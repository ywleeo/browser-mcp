"""Pure Douyin URL parsing and signed-response normalization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from browser_mcp.sites.models import (
    DouyinComment,
    DouyinCommentsRequest,
    DouyinCommentsResult,
    DouyinSearchItem,
    DouyinSearchRequest,
    DouyinSearchResult,
    DouyinVideoResult,
)


class DouyinParseError(ValueError):
    """Raised for unsupported Douyin URLs or incompatible response shapes."""


@dataclass(frozen=True, slots=True)
class DouyinAwemeIdentity:
    """Stable post id and canonical page kind parsed from a Douyin URL."""

    aweme_id: str
    page_kind: str


def parse_douyin_aweme_url(raw_url: str) -> DouyinAwemeIdentity:
    """Accept canonical Douyin video and image-note URLs without following redirects."""
    parsed = urlsplit(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"douyin.com", "www.douyin.com"}:
        raise DouyinParseError(f"not a supported Douyin host: {hostname or '(missing)'}")
    match = re.fullmatch(r"/(video|note)/(\d+)", parsed.path.rstrip("/"))
    if match is None:
        raise DouyinParseError("expected https://www.douyin.com/video/{id} or /note/{id}")
    return DouyinAwemeIdentity(aweme_id=match.group(2), page_kind=match.group(1))


def shape_douyin_search(raw: dict[str, Any], request: DouyinSearchRequest) -> DouyinSearchResult:
    """Normalize Douyin's concatenated signed search stream into stable post cards."""
    payloads = _response_objects(raw)
    seen: set[str] = set()
    items: list[DouyinSearchItem] = []
    has_more = False
    cursor: int | None = None

    for payload in payloads:
        has_more = has_more or _boolean(payload.get("has_more"))
        payload_cursor = _optional_integer(payload.get("cursor"))
        if payload_cursor is not None:
            cursor = payload_cursor
        records = payload.get("data")
        for record_value in cast(list[object], records) if isinstance(records, list) else []:
            record = _object(record_value)
            aweme = _object(record.get("aweme_info")) or _object(record.get("awemeInfo"))
            if not aweme:
                continue
            aweme_id = _string(aweme.get("aweme_id")) or _string(aweme.get("awemeId"))
            if not aweme_id or aweme_id in seen:
                continue
            seen.add(aweme_id)
            items.append(_search_item(aweme, len(items) + 1))
            if len(items) >= request.limit:
                return DouyinSearchResult(
                    keyword=request.keyword.strip(),
                    has_more=has_more or len(seen) > request.limit,
                    cursor=cursor,
                    items=tuple(items),
                )

    return DouyinSearchResult(
        keyword=request.keyword.strip(),
        has_more=has_more,
        cursor=cursor,
        items=tuple(items),
    )


def shape_douyin_video(
    raw: dict[str, Any],
    identity: DouyinAwemeIdentity,
) -> DouyinVideoResult:
    """Normalize one signed aweme-detail response and select usable media addresses."""
    aweme: dict[str, Any] = {}
    for payload in _response_objects(raw):
        candidate = (
            _object(payload.get("aweme_detail"))
            or _object(payload.get("aweme_info"))
            or _object(_object(payload.get("data")).get("aweme_detail"))
        )
        candidate_id = _string(candidate.get("aweme_id")) or _string(candidate.get("awemeId"))
        if candidate and (not candidate_id or candidate_id == identity.aweme_id):
            aweme = candidate
            break
    if not aweme:
        raise DouyinParseError("aweme was not found in the Douyin detail response")

    author = _object(aweme.get("author")) or _object(aweme.get("authorInfo"))
    stats = _object(aweme.get("statistics")) or _object(aweme.get("stats"))
    music = _object(aweme.get("music"))
    timestamp = _seconds_to_milliseconds(
        _optional_integer(aweme.get("create_time") or aweme.get("createTime"))
    )
    aweme_type = _aweme_type(aweme)
    return DouyinVideoResult(
        aweme_id=identity.aweme_id,
        aweme_type=aweme_type,
        url=_aweme_url(identity.aweme_id, aweme_type),
        description=_string(aweme.get("desc")),
        author=_string(author.get("nickname")),
        author_id=_string(author.get("uid")),
        sec_uid=_string(author.get("sec_uid")) or _string(author.get("secUid")),
        published_at=_format_milliseconds(timestamp),
        published_at_ms=timestamp,
        duration_ms=_duration_milliseconds(aweme),
        likes=_integer(stats.get("digg_count") or stats.get("diggCount")),
        comments=_integer(stats.get("comment_count") or stats.get("commentCount")),
        collects=_integer(stats.get("collect_count") or stats.get("collectCount")),
        shares=_integer(stats.get("share_count") or stats.get("shareCount")),
        cover_url=_cover_url(aweme),
        media_urls=_media_urls(aweme),
        music_title=_string(music.get("title")),
        music_author=_string(music.get("author")),
    )


def shape_douyin_comments(
    raw: dict[str, Any],
    identity: DouyinAwemeIdentity,
    request: DouyinCommentsRequest,
) -> DouyinCommentsResult:
    """Normalize captured root and reply pages into one deduplicated comment thread."""
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def collect(
        raw_comment: object,
        *,
        root_id: str = "",
        parent_id: str = "",
        depth: int = 0,
    ) -> None:
        """Collect one comment and recursively include inline reply records."""
        comment = _object(raw_comment)
        comment_id = _string(comment.get("cid")) or _string(comment.get("comment_id"))
        if not comment_id:
            return
        reply_id = _string(comment.get("reply_id"))
        resolved_root = root_id or _string(comment.get("reply_comment_id")) or comment_id
        resolved_parent = parent_id or (reply_id if reply_id not in {"", "0"} else "")
        if resolved_root in {"", "0", comment_id}:
            resolved_root = comment_id
            resolved_parent = ""
            depth = 0
        user = _object(comment.get("user"))
        timestamp = _seconds_to_milliseconds(_optional_integer(comment.get("create_time")))
        normalized = {
            "comment_id": comment_id,
            "root_comment_id": resolved_root,
            "parent_comment_id": resolved_parent or None,
            "depth": depth,
            "user_id": _string(user.get("uid")),
            "author": _string(user.get("nickname")),
            "text": _string(comment.get("text")),
            "published_at": _format_milliseconds(timestamp),
            "published_at_ms": timestamp,
            "ip_location": _string(comment.get("ip_label")),
            "likes": _integer(comment.get("digg_count")),
            "reply_count": _integer(comment.get("reply_comment_total")),
            "reply_to": _string(comment.get("reply_to_user_name")),
        }
        if comment_id not in records:
            order.append(comment_id)
        records[comment_id] = normalized

        raw_replies = comment.get("reply_comment")
        replies = cast(list[object], raw_replies) if isinstance(raw_replies, list) else []
        for reply in replies:
            collect(reply, root_id=resolved_root, parent_id=comment_id, depth=depth + 1)

    total: int | None = None
    raw_pages = raw.get("pages")
    pages = cast(list[object], raw_pages) if isinstance(raw_pages, list) else []
    for raw_page in pages:
        page = _object(raw_page)
        payload = _object(page.get("payload")) or page
        page_total = _optional_integer(payload.get("total"))
        if page_total is not None:
            total = max(total or 0, page_total)
        raw_comments = payload.get("comments")
        comments = cast(list[object], raw_comments) if isinstance(raw_comments, list) else []
        page_kind = _string(page.get("kind"))
        root_id = _string(page.get("root_comment_id"))
        for comment in comments:
            collect(
                comment,
                root_id=root_id if page_kind == "reply" else "",
                depth=1 if page_kind == "reply" else 0,
            )

    selected = order[: request.max_comments]
    items = tuple(
        DouyinComment(index=index, **records[comment_id])
        for index, comment_id in enumerate(selected, start=1)
    )
    limit_reached = raw.get("limit_reached") is True or len(order) > request.max_comments
    complete = raw.get("complete") is True and not limit_reached
    return DouyinCommentsResult(
        aweme_id=identity.aweme_id,
        url=_aweme_url(identity.aweme_id, identity.page_kind),
        total=total,
        fetched=len(items),
        complete=complete,
        limit_reached=limit_reached,
        pages_fetched=len(pages),
        scrolls=_integer(raw.get("scrolls")),
        items=items,
    )


def _search_item(aweme: dict[str, Any], index: int) -> DouyinSearchItem:
    """Build one normalized search item from an aweme object."""
    author = _object(aweme.get("author")) or _object(aweme.get("authorInfo"))
    stats = _object(aweme.get("statistics")) or _object(aweme.get("stats"))
    aweme_id = _string(aweme.get("aweme_id")) or _string(aweme.get("awemeId"))
    timestamp = _seconds_to_milliseconds(
        _optional_integer(aweme.get("create_time") or aweme.get("createTime"))
    )
    aweme_type = _aweme_type(aweme)
    return DouyinSearchItem(
        index=index,
        aweme_id=aweme_id,
        aweme_type=aweme_type,
        url=_aweme_url(aweme_id, aweme_type),
        description=_string(aweme.get("desc")),
        author=_string(author.get("nickname")),
        author_id=_string(author.get("uid")),
        sec_uid=_string(author.get("sec_uid")) or _string(author.get("secUid")),
        published_at=_format_milliseconds(timestamp),
        published_at_ms=timestamp,
        duration_ms=_duration_milliseconds(aweme),
        likes=_integer(stats.get("digg_count") or stats.get("diggCount")),
        comments=_integer(stats.get("comment_count") or stats.get("commentCount")),
        collects=_integer(stats.get("collect_count") or stats.get("collectCount")),
        shares=_integer(stats.get("share_count") or stats.get("shareCount")),
        cover_url=_cover_url(aweme),
    )


def _response_objects(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return JSON objects from direct fixtures or a captured streaming response body."""
    chunks = raw.get("chunks")
    if isinstance(chunks, list):
        return [_object(chunk) for chunk in cast(list[object], chunks) if _object(chunk)]
    body = raw.get("body")
    if not isinstance(body, str):
        return [raw]
    candidates = [body]
    decoded = _decode_http_chunks(body)
    if decoded != body:
        candidates.insert(0, decoded)
    for candidate in candidates:
        objects = _scan_json_objects(candidate)
        if objects:
            return objects
    raise DouyinParseError("Douyin response did not contain a complete JSON object")


def _decode_http_chunks(raw: str) -> str:
    """Decode optional HTTP chunk framing observed on Douyin streaming search bodies."""
    data = raw.encode("utf-8")
    cursor = 0
    chunks: list[bytes] = []
    while cursor < len(data):
        line_end = data.find(b"\r\n", cursor)
        if line_end < 0:
            return raw
        size_token = data[cursor:line_end].split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9A-Fa-f]+", size_token):
            return raw
        size = int(size_token, 16)
        cursor = line_end + 2
        if size == 0:
            break
        end = cursor + size
        if end > len(data):
            return raw
        chunks.append(data[cursor:end])
        cursor = end
        if data[cursor : cursor + 2] == b"\r\n":
            cursor += 2
    try:
        return b"".join(chunks).decode("utf-8") if chunks else raw
    except UnicodeDecodeError:
        return raw


def _scan_json_objects(raw: str) -> list[dict[str, Any]]:
    """Decode concatenated JSON objects while tolerating streaming separators."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(raw):
        start = raw.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(raw, start)
        except JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            objects.append(cast(dict[str, Any], value))
        cursor = max(end, start + 1)
    return objects


def _aweme_type(aweme: dict[str, Any]) -> str:
    """Classify a post by available image records instead of unstable numeric type ids."""
    images = aweme.get("images")
    has_images = isinstance(images, list) and len(cast(list[object], images)) > 0
    return "note" if has_images else "video"


def _aweme_url(aweme_id: str, aweme_type: str) -> str:
    """Build one canonical Douyin page URL."""
    page_kind = "note" if aweme_type == "note" else "video"
    return f"https://www.douyin.com/{page_kind}/{aweme_id}"


def _duration_milliseconds(aweme: dict[str, Any]) -> int | None:
    """Return duration from either the video or top-level aweme record."""
    video = _object(aweme.get("video"))
    return _optional_integer(video.get("duration")) or _optional_integer(aweme.get("duration"))


def _cover_url(aweme: dict[str, Any]) -> str:
    """Select the first stable cover URL for videos and image posts."""
    images = aweme.get("images")
    if isinstance(images, list) and images:
        return _address_url(cast(list[object], images)[0])
    video = _object(aweme.get("video"))
    return (
        _address_url(video.get("cover"))
        or _address_url(video.get("coverUrlList"))
        or _address_url(video.get("origin_cover"))
        or _address_url(video.get("originCover"))
        or _address_url(video.get("originCoverUrlList"))
    )


def _media_urls(aweme: dict[str, Any]) -> tuple[str, ...]:
    """Select image originals or the primary playable video address."""
    images = aweme.get("images")
    if isinstance(images, list) and images:
        urls = [_address_url(image) for image in cast(list[object], images)]
        return tuple(url for url in urls if url)
    video = _object(aweme.get("video"))
    play_url = _address_url(video.get("play_addr")) or _address_url(video.get("playAddr"))
    if play_url:
        return (play_url,)
    bit_rates = video.get("bit_rate") or video.get("bitRateList")
    records = cast(list[object], bit_rates) if isinstance(bit_rates, list) else []
    ranked = sorted(
        records,
        key=lambda item: _integer(
            _object(item).get("bit_rate") or _object(item).get("bitRate")
        ),
        reverse=True,
    )
    for record in ranked:
        record_data = _object(record)
        url = _address_url(record_data.get("play_addr")) or _address_url(
            record_data.get("playAddr")
        )
        if url:
            return (url,)
    return ()


def _address_url(value: object) -> str:
    """Return the first URL across API and React Flight media address shapes."""
    direct = _string(value)
    if direct:
        return direct
    if isinstance(value, list):
        for item in cast(list[object], value):
            url = _address_url(item)
            if url:
                return url
        return ""
    mapping = _object(value)
    direct = _string(mapping.get("src")) or _string(mapping.get("url"))
    if direct:
        return direct
    raw_urls = mapping.get("url_list") or mapping.get("urlList")
    urls = cast(list[object], raw_urls) if isinstance(raw_urls, list) else []
    for raw_url in urls:
        url = _address_url(raw_url)
        if url:
            return url
    return ""


def _object(value: object) -> dict[str, Any]:
    """Return JSON objects or an empty mapping for absent response variants."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    """Return string-like identifiers without exposing nested values."""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return ""


def _integer(value: object) -> int:
    """Parse non-negative Douyin counters while mapping absent values to zero."""
    parsed = _optional_integer(value)
    return max(0, parsed or 0)


def _optional_integer(value: object) -> int | None:
    """Parse an integer without accepting booleans or lossy floating-point values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _boolean(value: object) -> bool:
    """Interpret Douyin's integer and boolean pagination flags."""
    return value is True or value == 1 or value == "1"


def _seconds_to_milliseconds(value: int | None) -> int | None:
    """Normalize a Unix-seconds timestamp to milliseconds."""
    return value * 1000 if value is not None else None


def _format_milliseconds(value: int | None) -> str:
    """Format a Unix-milliseconds timestamp in the China calendar."""
    if value is None:
        return ""
    moment = datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    return moment.isoformat(timespec="seconds")
