"""Pure Zhihu URL classification, SSR parsing, shaping, and rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from itertools import takewhile
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from browser_mcp.sites.models import (
    ZhihuInvitationItem,
    ZhihuInvitationsResult,
    ZhihuSearchItem,
    ZhihuSearchRequest,
    ZhihuSearchResult,
    ZhihuSearchType,
)


class ZhihuParseError(ValueError):
    """Raised when a supported Zhihu response no longer matches its contract."""


@dataclass(frozen=True, slots=True)
class ZhihuPageKind:
    """Numeric identity parsed from a supported Zhihu content URL."""

    kind: Literal["question", "answer", "article"]
    content_id: str
    question_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedZhihuDocument:
    """Normalized Zhihu content ready for immutable snapshot storage."""

    kind: str
    title: str
    content: str


class _InitialDataParser(HTMLParser):
    """Extract the JSON text inside Zhihu's js-initialData script."""

    def __init__(self) -> None:
        """Initialize script-selection state."""
        super().__init__(convert_charrefs=True)
        self._inside = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start collecting only the expected script element."""
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "js-initialData":
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        """Stop collecting when the selected script closes."""
        if tag == "script" and self._inside:
            self._inside = False

    def handle_data(self, data: str) -> None:
        """Retain raw JSON character data from the selected script."""
        if self._inside:
            self.chunks.append(data)


class _PlainTextParser(HTMLParser):
    """Convert Zhihu body HTML to paragraph-preserving plain text."""

    _BLOCKS = {"p", "br", "div", "li", "h1", "h2", "h3", "blockquote", "pre", "tr"}

    def __init__(self) -> None:
        """Initialize output chunks."""
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Translate block boundaries to line breaks."""
        del attrs
        if tag in self._BLOCKS and self.chunks and not self.chunks[-1].endswith("\n"):
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Terminate block elements with one line break."""
        if tag in self._BLOCKS and self.chunks and not self.chunks[-1].endswith("\n"):
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        """Retain text and decoded entities."""
        self.chunks.append(data)


def classify_zhihu_url(url: str) -> ZhihuPageKind:
    """Accept only question, answer, and article URLs on exact Zhihu domains."""
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"www.zhihu.com", "zhihu.com", "zhuanlan.zhihu.com"}:
        raise ZhihuParseError(f"not a supported Zhihu host: {hostname or '(missing)'}")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) == 2 and segments[0] == "question":
        return ZhihuPageKind("question", _numeric_id(segments[1]))
    if len(segments) == 4 and segments[0] == "question" and segments[2] == "answer":
        return ZhihuPageKind(
            "answer",
            _numeric_id(segments[3]),
            question_id=_numeric_id(segments[1]),
        )
    if len(segments) == 2 and segments[0] == "p":
        return ZhihuPageKind("article", _numeric_id(segments[1]))
    raise ZhihuParseError("expected /question/{id}, /question/{id}/answer/{id}, or /p/{id}")


def parse_zhihu_search(raw: dict[str, Any], request: ZhihuSearchRequest) -> ZhihuSearchResult:
    """Normalize Zhihu search_v3 JSON while dropping advertising and unknown records."""
    data = raw.get("data")
    records = cast(list[object], data) if isinstance(data, list) else []
    items: list[ZhihuSearchItem] = []
    for record in records:
        typed_record = _object(record)
        if not typed_record or typed_record.get("type") != "search_result":
            continue
        item = _search_item(typed_record, request.offset + len(items) + 1)
        if item is not None:
            items.append(item)
    paging = _object(raw.get("paging"))
    return ZhihuSearchResult(
        keyword=request.keyword.strip(),
        search_type=request.search_type,
        offset=request.offset,
        has_more=paging.get("is_end") is False,
        items=tuple(items),
    )


def parse_zhihu_content(html: str, page: ZhihuPageKind) -> ParsedZhihuDocument:
    """Parse one supported content type from Zhihu's SSR entity store."""
    initial = _extract_initial_data(html)
    entities = _object(_object(initial.get("initialState")).get("entities"))
    if not entities:
        raise ZhihuParseError("js-initialData is missing initialState.entities")
    if page.kind == "question":
        return _parse_question(entities, page.content_id)
    if page.kind == "answer" and page.question_id is not None:
        return _parse_answer(entities, page.question_id, page.content_id)
    return _parse_article(entities, page.content_id)


def parse_zhihu_invitations(raw: dict[str, Any], requested_day: date) -> ZhihuInvitationsResult:
    """Normalize paged invitation notifications for one China-calendar day."""
    raw_pages = raw.get("pages")
    pages = cast(list[object], raw_pages) if isinstance(raw_pages, list) else []
    items: list[ZhihuInvitationItem] = []
    seen_urls: set[str] = set()
    china = ZoneInfo("Asia/Shanghai")
    for raw_page in pages:
        records_value = _object(raw_page).get("data")
        records = cast(list[object], records_value) if isinstance(records_value, list) else []
        for raw_record in records:
            record = _object(raw_record)
            timestamp = _integer(record.get("create_time"))
            if timestamp <= 0:
                continue
            invited_at = datetime.fromtimestamp(timestamp, UTC).astimezone(china)
            if invited_at.date() != requested_day:
                continue
            content = _object(record.get("content"))
            target = _object(content.get("target"))
            url = _canonical_invitation_url(_string(target.get("link")))
            question = _plain_text(_string(target.get("text")), inline=True)
            if not url or not question or url in seen_urls:
                continue
            actors_value = content.get("actors")
            actors = cast(list[object], actors_value) if isinstance(actors_value, list) else []
            inviters = tuple(
                name for actor in actors if (name := _string(_object(actor).get("name")))
            )
            seen_urls.add(url)
            items.append(
                ZhihuInvitationItem(
                    invited_at=invited_at.isoformat(timespec="minutes"),
                    verb=_plain_text(_string(content.get("verb")), inline=True),
                    inviters=inviters,
                    source="member" if inviters else "system",
                    question=question,
                    url=url,
                    merge_count=max(1, _integer(record.get("merge_count"))),
                )
            )
    items.sort(key=lambda item: item.invited_at, reverse=True)
    return ZhihuInvitationsResult(
        day=requested_day,
        complete=raw.get("complete") is True,
        pages_fetched=len(pages),
        items=tuple(items),
    )


def _search_item(record: dict[str, Any], index: int) -> ZhihuSearchItem | None:
    """Normalize one search record and reconstruct its canonical web URL."""
    item = _object(record.get("object"))
    kind = _string(item.get("type"))
    if kind not in {"answer", "article", "question"}:
        return None
    question = _object(item.get("question"))
    title = _plain_text(_string(item.get("title")) or _string(question.get("title")), inline=True)
    author = _string(_object(item.get("author")).get("name"))
    excerpt = _plain_text(_string(item.get("excerpt")) or _string(item.get("content")), inline=True)
    return ZhihuSearchItem(
        index=index,
        kind=kind,
        title=title,
        author=author,
        voteup=_integer(item.get("voteup_count")),
        comments=_integer(item.get("comment_count")),
        excerpt=_truncate(excerpt, 240),
        url=_search_result_url(item, question, kind),
    )


def _search_result_url(item: dict[str, Any], question: dict[str, Any], kind: str) -> str:
    """Map API entity URLs and ids to canonical browser URLs."""
    item_id = _entity_id(item, {"articles/", "answers/", "questions/"})
    if kind == "article" and item_id:
        return f"https://zhuanlan.zhihu.com/p/{item_id}"
    if kind == "question" and item_id:
        return f"https://www.zhihu.com/question/{item_id}"
    question_id = _entity_id(question, {"questions/"})
    if kind == "answer" and item_id and question_id:
        return f"https://www.zhihu.com/question/{question_id}/answer/{item_id}"
    return ""


def _canonical_invitation_url(url: str) -> str:
    """Accept only supported Zhihu question links from notification payloads."""
    try:
        page = classify_zhihu_url(url)
    except ZhihuParseError:
        return ""
    if page.kind != "question":
        return ""
    return f"https://www.zhihu.com/question/{page.content_id}"


def _parse_question(entities: dict[str, Any], question_id: str) -> ParsedZhihuDocument:
    """Render question metadata and every SSR-embedded answer sorted by votes."""
    question = _object(_object(entities.get("questions")).get(question_id))
    if not question:
        raise ZhihuParseError(f"question entity {question_id} is missing")
    title = _plain_text(_string(question.get("title")), inline=True)
    detail = _plain_text(_string(question.get("detail")))
    answer_count = _integer(question.get("answerCount"))
    follower_count = _integer(question.get("followerCount"))
    users = _object(entities.get("users"))
    answers: list[tuple[int, str]] = []
    for answer_id, raw_answer in _object(entities.get("answers")).items():
        answer = _object(raw_answer)
        if _question_id(answer.get("question")) != question_id:
            continue
        answers.append(
            (_integer(answer.get("voteupCount")), _render_answer(answer_id, answer, users))
        )
    answers.sort(key=lambda value: value[0], reverse=True)
    lines = [
        f"知乎问题：{title}",
        f"URL: https://www.zhihu.com/question/{question_id}",
        f"回答数：{answer_count} · 关注者：{follower_count}",
    ]
    if detail:
        lines.extend(("", detail))
    lines.extend(("", f"SSR 内含回答：{len(answers)}"))
    for index, (_votes, rendered) in enumerate(answers, start=1):
        lines.extend(("", f"## 回答 {index}", rendered))
    if len(answers) < answer_count:
        lines.extend(("", f"另有 {answer_count - len(answers)} 个回答未包含在 SSR 页面中。"))
    return ParsedZhihuDocument("question", title, "\n".join(lines).strip())


def _parse_answer(
    entities: dict[str, Any], question_id: str, answer_id: str
) -> ParsedZhihuDocument:
    """Render one answer with question title, author, stats, date, and body."""
    answer = _object(_object(entities.get("answers")).get(answer_id))
    if not answer:
        raise ZhihuParseError(f"answer entity {answer_id} is missing")
    question = _object(_object(entities.get("questions")).get(question_id))
    title = _plain_text(_string(question.get("title")), inline=True)
    rendered = _render_answer(answer_id, answer, _object(entities.get("users")))
    content = (
        f"知乎回答：{title}\n"
        f"URL: https://www.zhihu.com/question/{question_id}/answer/{answer_id}\n\n{rendered}"
    )
    return ParsedZhihuDocument("answer", title, content)


def _parse_article(entities: dict[str, Any], article_id: str) -> ParsedZhihuDocument:
    """Render one Zhihu article from the SSR article entity."""
    article = _object(_object(entities.get("articles")).get(article_id))
    if not article:
        raise ZhihuParseError(f"article entity {article_id} is missing")
    title = _plain_text(_string(article.get("title")), inline=True)
    author = _author_name(article.get("author"), _object(entities.get("users")))
    body = _plain_text(_string(article.get("content")))
    content = (
        f"知乎文章：{title}\nURL: https://zhuanlan.zhihu.com/p/{article_id}\n"
        f"作者：{author} · 赞同：{_integer(article.get('voteupCount'))} · "
        f"评论：{_integer(article.get('commentCount'))} · "
        f"发布：{_format_timestamp(_integer(article.get('created')))}\n\n{body}"
    )
    return ParsedZhihuDocument("article", title, content)


def _render_answer(answer_id: str, answer: dict[str, Any], users: dict[str, Any]) -> str:
    """Render one normalized answer block."""
    author = _author_name(answer.get("author"), users)
    body = _plain_text(_string(answer.get("content")))
    return (
        f"作者：{author} · 赞同：{_integer(answer.get('voteupCount'))} · "
        f"评论：{_integer(answer.get('commentCount'))} · "
        f"发布：{_format_timestamp(_integer(answer.get('createdTime')))}\n"
        f"回答 ID：{answer_id}\n\n{body}"
    )


def _extract_initial_data(html: str) -> dict[str, Any]:
    """Extract and decode Zhihu's embedded initial state."""
    parser = _InitialDataParser()
    parser.feed(html)
    raw = "".join(parser.chunks).strip()
    if not raw:
        raise ZhihuParseError("js-initialData script was not found")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ZhihuParseError(f"invalid js-initialData JSON: {error}") from error
    return _object(value)


def _plain_text(html: str, *, inline: bool = False) -> str:
    """Strip HTML while retaining paragraphs for bodies or collapsing titles inline."""
    parser = _PlainTextParser()
    parser.feed(html)
    text = "".join(parser.chunks)
    if inline:
        return " ".join(text.split())
    lines = [" ".join(line.split()) for line in text.splitlines()]
    output: list[str] = []
    for line in lines:
        if line or (output and output[-1]):
            output.append(line)
    return "\n".join(output).strip()


def _author_name(author_value: object, users: dict[str, Any]) -> str:
    """Resolve inline authors or normalized user references."""
    author = _object(author_value)
    direct = _string(author.get("name"))
    if direct:
        return direct
    key = _string(author_value) or _string(author.get("urlToken")) or _string(author.get("id"))
    return _string(_object(users.get(key)).get("name")) if key else ""


def _entity_id(entity: dict[str, Any], markers: set[str]) -> str:
    """Read a numeric id directly or from one of the known API URL markers."""
    direct = _string(entity.get("id")) or str(_integer(entity.get("id")) or "")
    if direct:
        return direct
    url = _string(entity.get("url"))
    for marker in markers:
        if marker in url:
            tail = url.split(marker, 1)[1]
            digits = "".join(takewhile(str.isdigit, tail))
            if digits:
                return digits
    return ""


def _question_id(value: object) -> str:
    """Read a question reference stored as an id or normalized object."""
    if isinstance(value, (str, int)):
        return str(value)
    question = _object(value)
    return _string(question.get("id")) or str(_integer(question.get("id")) or "")


def _numeric_id(value: str) -> str:
    """Require a numeric leading URL identifier."""
    if not value.isdigit():
        raise ZhihuParseError(f"expected a numeric id, got {value!r}")
    return value


def _format_timestamp(value: int) -> str:
    """Render Unix seconds in the China timezone."""
    if value <= 0:
        return "未知"
    return (
        datetime.fromtimestamp(value, UTC).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    )


def _truncate(value: str, limit: int) -> str:
    """Bound a search excerpt without splitting Unicode."""
    return value if len(value) <= limit else value[:limit] + "…"


def _object(value: object) -> dict[str, Any]:
    """Return a JSON object or an empty mapping for absent variants."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    """Normalize JSON string and numeric identifiers."""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return ""


def _integer(value: object) -> int:
    """Normalize JSON integer and decimal-string counters."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def map_zhihu_search_type(search_type: ZhihuSearchType) -> str:
    """Map public tool filters to Zhihu search_v3's `t` parameter."""
    return "general" if search_type is ZhihuSearchType.CONTENT else search_type.value
