"""Pure Xiaohongshu URL parsing and response shaping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from browser_mcp.sites.models import (
    XhsComment,
    XhsCommentsRequest,
    XhsCommentsResult,
    XhsImage,
    XhsNoteResult,
    XhsSearchItem,
    XhsSearchRequest,
    XhsSearchResult,
    XhsUserNoteItem,
    XhsUserNotesRequest,
    XhsUserNotesResult,
)


class XhsParseError(ValueError):
    """Raised for unsupported XHS URLs or incompatible upstream response shapes."""


@dataclass(frozen=True, slots=True)
class XhsNoteIdentity:
    """Note id and security parameters carried by a canonical XHS URL."""

    note_id: str
    xsec_token: str
    xsec_source: str


def parse_xhs_note_url(raw_url: str) -> XhsNoteIdentity:
    """Accept an XHS explore URL and preserve its signed access parameters."""
    parsed = urlsplit(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        raise XhsParseError(f"not a supported Xiaohongshu host: {hostname or '(missing)'}")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2 or segments[0] != "explore" or not segments[1]:
        raise XhsParseError("expected https://www.xiaohongshu.com/explore/{note_id}")
    query = parse_qs(parsed.query)
    return XhsNoteIdentity(
        note_id=segments[1],
        xsec_token=_first_query(query, "xsec_token"),
        xsec_source=_first_query(query, "xsec_source") or "pc_search",
    )


def shape_xhs_search(raw: dict[str, Any], request: XhsSearchRequest) -> XhsSearchResult:
    """Normalize signed XHS search JSON into stable note cards."""
    data = _object(raw.get("data"))
    raw_items = data.get("items") if isinstance(data.get("items"), list) else raw.get("items")
    records = cast(list[object], raw_items) if isinstance(raw_items, list) else []
    items: list[XhsSearchItem] = []
    for index, raw_item in enumerate(records):
        item = _object(raw_item)
        note = _object(item.get("note_card")) or item
        note_id = _string(item.get("id")) or _string(note.get("id"))
        if not note_id:
            continue
        token = _string(item.get("xsec_token")) or _string(note.get("xsec_token"))
        user = _object(note.get("user"))
        stats = _object(note.get("interact_info"))
        cover = _object(note.get("cover"))
        cover_url = _string(cover.get("url_default")) or _string(cover.get("url"))
        if not cover_url:
            image_list = note.get("image_list")
            if isinstance(image_list, list) and image_list:
                first_image = cast(list[object], image_list)[0]
                cover_url = _string(_object(first_image).get("url"))
        items.append(
            XhsSearchItem(
                index=(request.page - 1) * 20 + index + 1,
                note_id=note_id,
                xsec_token=token,
                url=_build_note_url(note_id, token, "pc_search"),
                title=_string(note.get("display_title")) or _string(note.get("title")),
                author=_string(user.get("nickname")),
                cover=cover_url,
                note_type=_string(note.get("type")) or "normal",
                likes=_counter(stats.get("liked_count")),
                collects=_counter(stats.get("collected_count")),
                comments=_counter(stats.get("comment_count")),
            )
        )
    return XhsSearchResult(
        keyword=request.keyword.strip(),
        page=request.page,
        sort=request.sort,
        has_more=data.get("has_more") is True,
        items=tuple(items),
    )


def shape_xhs_user_notes(raw: dict[str, Any], request: XhsUserNotesRequest) -> XhsUserNotesResult:
    """Merge SSR and signed pagination pages into one stable published-note list."""
    raw_pages = raw.get("pages")
    pages = cast(list[object], raw_pages) if isinstance(raw_pages, list) else []
    items: list[XhsUserNoteItem] = []
    seen_note_ids: set[str] = set()
    final_cursor = ""
    has_more = False

    for raw_page in pages:
        page = _object(raw_page)
        final_cursor = _string(page.get("cursor")) or final_cursor
        has_more = page.get("has_more") is True or page.get("hasMore") is True
        raw_notes = page.get("notes")
        notes = cast(list[object], raw_notes) if isinstance(raw_notes, list) else []
        for raw_note in notes:
            wrapper = _object(raw_note)
            note = _object(wrapper.get("noteCard")) or wrapper
            note_id = (
                _string(note.get("note_id"))
                or _string(note.get("noteId"))
                or _string(wrapper.get("id"))
            )
            if not note_id or note_id in seen_note_ids:
                continue
            seen_note_ids.add(note_id)
            token = (
                _string(note.get("xsec_token"))
                or _string(note.get("xsecToken"))
                or _string(wrapper.get("xsecToken"))
            )
            user = _object(note.get("user"))
            stats = _object(note.get("interact_info")) or _object(note.get("interactInfo"))
            cover = _object(note.get("cover"))
            timestamp = _optional_integer(note.get("time"))
            items.append(
                XhsUserNoteItem(
                    index=len(items) + 1,
                    note_id=note_id,
                    xsec_token=token,
                    url=_build_note_url(note_id, token, "pc_user"),
                    title=_string(note.get("display_title"))
                    or _string(note.get("displayTitle"))
                    or _string(note.get("title")),
                    author=_string(user.get("nickname"))
                    or _string(user.get("nick_name"))
                    or _string(user.get("nickName")),
                    cover=_cover_url(cover),
                    note_type=_string(note.get("type")) or "normal",
                    published_at=_format_milliseconds(timestamp),
                    published_at_ms=timestamp,
                    likes=_counter(stats.get("liked_count") or stats.get("likedCount")),
                    is_sticky=stats.get("sticky") is True,
                )
            )

    pages_fetched = _optional_integer(raw.get("pages_fetched")) or len(pages)
    complete = raw.get("complete") is True or (bool(pages) and not has_more)
    return XhsUserNotesResult(
        user_id=_string(raw.get("user_id")) or request.user_id or "",
        nickname=_string(raw.get("nickname")),
        red_id=_string(raw.get("red_id")),
        complete=complete,
        pages_fetched=pages_fetched,
        has_more=has_more,
        cursor=final_cursor,
        items=tuple(items),
    )


def shape_xhs_note(raw: dict[str, Any], identity: XhsNoteIdentity) -> XhsNoteResult:
    """Normalize XHS noteDetailMap SSR state, including best image and video URLs."""
    note_map = _object(_object(raw.get("note")).get("noteDetailMap"))
    note = _object(_object(note_map.get(identity.note_id)).get("note"))
    if not note:
        raise XhsParseError("note was not found in window.__INITIAL_STATE__.note.noteDetailMap")
    user = _object(note.get("user"))
    stats = _object(note.get("interactInfo"))
    images: list[XhsImage] = []
    image_list = note.get("imageList")
    for raw_image in cast(list[object], image_list) if isinstance(image_list, list) else []:
        image = _object(raw_image)
        url = (
            _string(image.get("urlDefault"))
            or _string(image.get("url"))
            or _string(image.get("urlPre"))
        )
        if url:
            images.append(XhsImage(url=url))
    timestamp = _optional_integer(note.get("time"))
    return XhsNoteResult(
        note_id=identity.note_id,
        note_type=_string(note.get("type")) or "normal",
        url=_build_note_url(identity.note_id, identity.xsec_token, identity.xsec_source),
        title=_string(note.get("title")),
        description=_string(note.get("desc")),
        author=_string(user.get("nickname")),
        published_at=_format_milliseconds(timestamp),
        published_at_ms=timestamp,
        likes=_counter(stats.get("likedCount")),
        collects=_counter(stats.get("collectedCount")),
        comments=_counter(stats.get("commentCount")),
        images=tuple(images),
        video_url=_video_url(note),
    )


def shape_xhs_comments(
    raw: dict[str, Any],
    identity: XhsNoteIdentity,
    request: XhsCommentsRequest,
) -> XhsCommentsResult:
    """Normalize captured comment-page responses into a deduplicated flat thread."""
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def collect(
        raw_comment: object,
        *,
        root_id: str = "",
        parent_id: str = "",
        depth: int = 0,
    ) -> None:
        """Collect one comment and its inline replies while retaining first-seen order."""
        comment = _object(raw_comment)
        comment_id = _string(comment.get("id")) or _string(comment.get("comment_id"))
        if not comment_id:
            return
        resolved_root = root_id or _string(comment.get("root_comment_id")) or comment_id
        target = _object(comment.get("target_comment")) or _object(comment.get("targetComment"))
        resolved_parent = (
            _string(comment.get("parent_comment_id"))
            or _string(target.get("id"))
            or parent_id
        )
        if resolved_root == comment_id:
            resolved_parent = ""
            depth = 0
        user = _object(comment.get("user_info")) or _object(comment.get("userInfo"))
        reply_user = _object(target.get("user_info")) or _object(target.get("userInfo"))
        timestamp = _timestamp_milliseconds(
            _optional_integer(comment.get("create_time") or comment.get("createTime"))
        )
        normalized = {
            "comment_id": comment_id,
            "root_comment_id": resolved_root,
            "parent_comment_id": resolved_parent or None,
            "depth": depth,
            "user_id": _string(user.get("user_id")) or _string(user.get("userId")),
            "author": _string(user.get("nickname")) or _string(user.get("nick_name")),
            "text": _string(comment.get("content")) or _string(comment.get("text")),
            "published_at": _format_milliseconds(timestamp),
            "published_at_ms": timestamp,
            "ip_location": _string(comment.get("ip_location"))
            or _string(comment.get("ipLocation")),
            "likes": _counter(comment.get("like_count") or comment.get("liked_count")),
            "reply_count": _integer(
                comment.get("sub_comment_count") or comment.get("subCommentCount")
            ),
            "reply_to": _string(reply_user.get("nickname")) or _string(reply_user.get("nick_name")),
        }
        existing = records.get(comment_id)
        if existing is None:
            records[comment_id] = normalized
            order.append(comment_id)
        else:
            records[comment_id] = {
                key: value if value not in {"", None, 0} else existing.get(key, value)
                for key, value in normalized.items()
            }

        raw_replies = comment.get("sub_comments") or comment.get("subComments")
        replies = cast(list[object], raw_replies) if isinstance(raw_replies, list) else []
        for reply in replies:
            collect(
                reply,
                root_id=resolved_root,
                parent_id=comment_id,
                depth=depth + 1,
            )

    raw_pages = raw.get("pages")
    pages = cast(list[object], raw_pages) if isinstance(raw_pages, list) else []
    for raw_page in pages:
        page = _object(raw_page)
        payload = _object(page.get("payload"))
        data = _object(payload.get("data")) or payload
        raw_comments = data.get("comments") or data.get("comment_list")
        comments = cast(list[object], raw_comments) if isinstance(raw_comments, list) else []
        page_root_id = _string(page.get("root_comment_id"))
        is_sub_page = _string(page.get("kind")) == "sub"
        for raw_comment in comments:
            collect(
                raw_comment,
                root_id=page_root_id if is_sub_page else "",
                parent_id=page_root_id if is_sub_page else "",
                depth=1 if is_sub_page else 0,
            )

    limit_reached = raw.get("limit_reached") is True or len(order) > request.max_comments
    selected_ids = order[: request.max_comments]
    items = tuple(
        XhsComment(index=index, **records[comment_id])
        for index, comment_id in enumerate(selected_ids, start=1)
    )
    total = _optional_integer(raw.get("expected_count"))
    return XhsCommentsResult(
        note_id=identity.note_id,
        url=_build_note_url(identity.note_id, identity.xsec_token, identity.xsec_source),
        total=total if total is not None and total >= 0 else None,
        fetched=len(items),
        complete=raw.get("complete") is True and not limit_reached,
        limit_reached=limit_reached,
        pages_fetched=len(pages),
        scrolls=_optional_integer(raw.get("scrolls")) or 0,
        items=items,
    )


def _video_url(note: dict[str, Any]) -> str | None:
    """Select a master URL across both codec-named and opaque XHS stream groups."""
    stream = _object(_object(_object(note.get("video")).get("media")).get("stream"))
    preferred_groups = ("h264", "h265", "h266", "av1")
    group_names = (*preferred_groups, *(name for name in stream if name not in preferred_groups))
    for group_name in group_names:
        entries = stream.get(group_name)
        if not isinstance(entries, list):
            continue
        normalized_entries = [_object(entry) for entry in cast(list[object], entries)]
        normalized_entries.sort(key=lambda entry: entry.get("defaultStream") != 1)
        for entry in normalized_entries:
            url = _string(entry.get("masterUrl")) or _string(entry.get("master_url"))
            if url:
                return url
    return None


def _cover_url(cover: dict[str, Any]) -> str:
    """Select the best available cover URL across snake- and camel-case responses."""
    direct = (
        _string(cover.get("url_default"))
        or _string(cover.get("urlDefault"))
        or _string(cover.get("url"))
        or _string(cover.get("url_pre"))
        or _string(cover.get("urlPre"))
    )
    if direct:
        return direct
    info_list = cover.get("info_list") or cover.get("infoList")
    for raw_image in cast(list[object], info_list) if isinstance(info_list, list) else []:
        image = _object(raw_image)
        if _string(image.get("image_scene")) in {"WB_DFT", "CRD_WM_JPG"} or _string(
            image.get("imageScene")
        ) in {"WB_DFT", "CRD_WM_JPG"}:
            url = _string(image.get("url"))
            if url:
                return url
    return ""


def _build_note_url(note_id: str, token: str, source: str) -> str:
    """Build the canonical shareable note URL without dropping its xsec token."""
    base = f"https://www.xiaohongshu.com/explore/{quote(note_id, safe='')}"
    if not token:
        return base
    return f"{base}?{urlencode({'xsec_token': token, 'xsec_source': source or 'pc_search'})}"


def _first_query(query: dict[str, list[str]], key: str) -> str:
    """Read the first non-empty query value."""
    values = query.get(key, [])
    return values[0] if values else ""


def _format_milliseconds(value: int | None) -> str:
    """Format a millisecond Unix timestamp in the China timezone."""
    if value is None or value <= 0:
        return ""
    return (
        datetime.fromtimestamp(value / 1000, UTC)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )


def _counter(value: object) -> str:
    """Preserve XHS counters that may be numeric strings such as `1万+`."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _optional_integer(value: object) -> int | None:
    """Normalize an optional integer timestamp."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _integer(value: object) -> int:
    """Normalize an integer-like JSON value while rejecting booleans."""
    return _optional_integer(value) or 0


def _timestamp_milliseconds(value: int | None) -> int | None:
    """Normalize XHS comment timestamps that may use seconds or milliseconds."""
    if value is None or value <= 0:
        return None
    return value * 1000 if value < 10_000_000_000 else value


def _string(value: object) -> str:
    """Return JSON string values without coercing nested objects."""
    return value if isinstance(value, str) else ""


def _object(value: object) -> dict[str, Any]:
    """Return JSON objects or an empty mapping for absent variants."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}
