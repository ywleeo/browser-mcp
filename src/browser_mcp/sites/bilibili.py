"""Pure Bilibili URL parsing and browser-response normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from browser_mcp.sites.models import (
    BilibiliSearchItem,
    BilibiliSearchRequest,
    BilibiliSearchResult,
    BilibiliVideoPart,
    BilibiliVideoResult,
)


class BilibiliParseError(ValueError):
    """Raised for unsupported Bilibili URLs or incompatible response shapes."""


@dataclass(frozen=True, slots=True)
class BilibiliVideoIdentity:
    """Stable BV/AV identity and selected multipart page parsed from a URL."""

    video_id: str
    bvid: str | None
    aid: int | None
    page: int


@dataclass(frozen=True, slots=True)
class BilibiliMediaTrack:
    """One selected Bilibili media track and its playback metadata."""

    kind: Literal["video", "audio"]
    url: str
    quality_id: int | None
    quality_label: str
    codec: str
    bandwidth: int


@dataclass(frozen=True, slots=True)
class BilibiliMediaStreams:
    """Best compatible video and audio tracks for one selected page."""

    bvid: str
    cid: int
    page: int
    video: BilibiliMediaTrack
    audio: BilibiliMediaTrack | None


def parse_bilibili_video_url(raw_url: str) -> BilibiliVideoIdentity:
    """Accept canonical Bilibili BV/AV video URLs and validate the optional p value."""
    parsed = urlsplit(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"bilibili.com", "www.bilibili.com"}:
        raise BilibiliParseError(f"not a supported Bilibili host: {hostname or '(missing)'}")
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av(\d+))", parsed.path.rstrip("/"))
    if match is None:
        raise BilibiliParseError("expected https://www.bilibili.com/video/BV... or /video/av...")
    query = parse_qs(parsed.query)
    raw_page = query.get("p", ["1"])[0]
    try:
        page = int(raw_page)
    except ValueError as error:
        raise BilibiliParseError("Bilibili multipart page p must be an integer") from error
    if page < 1 or page > 1_000:
        raise BilibiliParseError("Bilibili multipart page p must be between 1 and 1000")
    video_id = match.group(1)
    aid = int(match.group(2)) if match.group(2) else None
    return BilibiliVideoIdentity(
        video_id=video_id,
        bvid=video_id if video_id.startswith("BV") else None,
        aid=aid,
        page=page,
    )


def shape_bilibili_search(
    raw: dict[str, Any],
    request: BilibiliSearchRequest,
) -> BilibiliSearchResult:
    """Normalize one Bilibili video-search API page."""
    data = _api_data(raw, "Bilibili search")
    raw_items = data.get("result")
    items: list[BilibiliSearchItem] = []
    seen_bvids: set[str] = set()
    for raw_item in cast(list[object], raw_items) if isinstance(raw_items, list) else []:
        item = _object(raw_item)
        bvid = _string(item.get("bvid"))
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or bvid in seen_bvids:
            continue
        seen_bvids.add(bvid)
        published_ms = _seconds_to_milliseconds(_optional_integer(item.get("pubdate")))
        items.append(
            BilibiliSearchItem(
                index=len(items) + 1,
                bvid=bvid,
                aid=_integer(item.get("aid") or item.get("id")),
                url=f"https://www.bilibili.com/video/{bvid}/",
                title=_strip_markup(_string(item.get("title"))),
                description=_string(item.get("description") or item.get("desc")),
                author=_string(item.get("author")),
                author_id=_integer(item.get("mid")),
                category=_string(item.get("typename")),
                duration=_string(item.get("duration")),
                published_at=_format_milliseconds(published_ms),
                published_at_ms=published_ms,
                views=_integer(item.get("play")),
                danmaku=_integer(item.get("danmaku") or item.get("video_review")),
                favorites=_integer(item.get("favorites")),
                comments=_integer(item.get("review")),
                likes=_integer(item.get("like")),
                cover_url=_absolute_bilibili_url(_string(item.get("pic"))),
                tags=tuple(
                    tag.strip() for tag in _string(item.get("tag")).split(",") if tag.strip()
                ),
            )
        )
    total = _integer(data.get("numResults"))
    total_pages = _integer(data.get("numPages"))
    return BilibiliSearchResult(
        keyword=request.keyword.strip(),
        page=request.page,
        order=request.order,
        total=total,
        total_pages=total_pages,
        has_more=request.page < total_pages,
        items=tuple(items),
    )


def shape_bilibili_video(
    raw: dict[str, Any],
    identity: BilibiliVideoIdentity,
) -> BilibiliVideoResult:
    """Normalize Bilibili view and tag API responses for one selected page."""
    view = _api_data(_object(raw.get("view")), "Bilibili video metadata")
    bvid = _string(view.get("bvid"))
    aid = _integer(view.get("aid"))
    _validate_identity(identity, bvid, aid)
    pages = _video_parts(view, bvid)
    if identity.page > len(pages):
        raise BilibiliParseError(
            f"Bilibili video has {len(pages)} part(s); requested p={identity.page}"
        )
    selected = pages[identity.page - 1]
    owner = _object(view.get("owner"))
    stat = _object(view.get("stat"))
    published_ms = _seconds_to_milliseconds(_optional_integer(view.get("pubdate")))
    return BilibiliVideoResult(
        bvid=bvid,
        aid=aid,
        cid=selected.cid,
        page=identity.page,
        url=selected.url,
        title=_string(view.get("title")),
        description=_string(view.get("desc")),
        category=_string(view.get("tname")),
        cover_url=_absolute_bilibili_url(_string(view.get("pic"))),
        author=_string(owner.get("name")),
        author_id=_integer(owner.get("mid")),
        published_at=_format_milliseconds(published_ms),
        published_at_ms=published_ms,
        duration_seconds=_integer(view.get("duration")),
        views=_integer(stat.get("view")),
        danmaku=_integer(stat.get("danmaku")),
        comments=_integer(stat.get("reply")),
        favorites=_integer(stat.get("favorite")),
        coins=_integer(stat.get("coin")),
        shares=_integer(stat.get("share")),
        likes=_integer(stat.get("like")),
        tags=_tag_names(_object(raw.get("tags"))),
        parts=pages,
    )


def select_bilibili_media_streams(
    raw: dict[str, Any],
    identity: BilibiliVideoIdentity,
) -> BilibiliMediaStreams:
    """Select the highest-quality broadly compatible DASH tracks from page playinfo."""
    view = _api_data(_object(raw.get("view")), "Bilibili video metadata")
    bvid = _string(view.get("bvid"))
    aid = _integer(view.get("aid"))
    _validate_identity(identity, bvid, aid)
    pages = _video_parts(view, bvid)
    if identity.page > len(pages):
        raise BilibiliParseError(
            f"Bilibili video has {len(pages)} part(s); requested p={identity.page}"
        )
    selected = pages[identity.page - 1]
    playinfo = _api_data(_object(raw.get("playinfo")), "Bilibili playinfo")
    dash = _object(playinfo.get("dash"))
    quality_labels = _quality_labels(playinfo)
    video = _select_video_track(dash, quality_labels)
    if video is None:
        video = _select_progressive_track(playinfo, quality_labels)
    if video is None:
        raise BilibiliParseError("Bilibili playinfo contains no downloadable video stream")
    audio = _select_audio_track(dash)
    return BilibiliMediaStreams(
        bvid=bvid,
        cid=selected.cid,
        page=identity.page,
        video=video,
        audio=audio,
    )


def _api_data(raw: dict[str, Any], label: str) -> dict[str, Any]:
    """Require one successful Bilibili API envelope and return its object data."""
    code = raw.get("code")
    if code != 0:
        message = _string(raw.get("message")) or "unknown API error"
        raise BilibiliParseError(f"{label} returned code {code}: {message}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise BilibiliParseError(f"{label} returned no object data")
    return cast(dict[str, Any], data)


def _validate_identity(identity: BilibiliVideoIdentity, bvid: str, aid: int) -> None:
    """Fail closed when view metadata resolves to a different requested video."""
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or aid <= 0:
        raise BilibiliParseError("Bilibili metadata did not expose a valid BV/AV identity")
    if identity.bvid is not None and bvid != identity.bvid:
        raise BilibiliParseError("Bilibili metadata returned a different BV identity")
    if identity.aid is not None and aid != identity.aid:
        raise BilibiliParseError("Bilibili metadata returned a different AV identity")


def _video_parts(view: dict[str, Any], bvid: str) -> tuple[BilibiliVideoPart, ...]:
    """Normalize multipart entries while preserving their one-based page order."""
    raw_pages = view.get("pages")
    parts: list[BilibiliVideoPart] = []
    for fallback_index, raw_page in enumerate(
        cast(list[object], raw_pages) if isinstance(raw_pages, list) else [],
        start=1,
    ):
        page = _object(raw_page)
        index = _integer(page.get("page")) or fallback_index
        cid = _integer(page.get("cid"))
        if cid <= 0:
            continue
        suffix = f"?p={index}" if index > 1 else "/"
        parts.append(
            BilibiliVideoPart(
                index=index,
                cid=cid,
                title=_string(page.get("part")) or f"P{index}",
                duration_seconds=_integer(page.get("duration")),
                url=f"https://www.bilibili.com/video/{bvid}{suffix}",
            )
        )
    if not parts:
        cid = _integer(view.get("cid"))
        if cid > 0:
            parts.append(
                BilibiliVideoPart(
                    index=1,
                    cid=cid,
                    title=_string(view.get("title")) or "P1",
                    duration_seconds=_integer(view.get("duration")),
                    url=f"https://www.bilibili.com/video/{bvid}/",
                )
            )
    if not parts:
        raise BilibiliParseError("Bilibili metadata contains no playable parts")
    return tuple(parts)


def _tag_names(raw: dict[str, Any]) -> tuple[str, ...]:
    """Return unique tag names from the optional tag API response."""
    if raw.get("code") != 0 or not isinstance(raw.get("data"), list):
        return ()
    names: list[str] = []
    for value in cast(list[object], raw["data"]):
        name = _string(_object(value).get("tag_name")).strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _quality_labels(playinfo: dict[str, Any]) -> dict[int, str]:
    """Map Bilibili quality ids to the descriptions supplied by the current account."""
    quality_ids = playinfo.get("accept_quality")
    descriptions = playinfo.get("accept_description")
    if not isinstance(quality_ids, list) or not isinstance(descriptions, list):
        return {}
    typed_ids = cast(list[object], quality_ids)
    typed_descriptions = cast(list[object], descriptions)
    return {
        _integer(quality): _string(description)
        for quality, description in zip(typed_ids, typed_descriptions, strict=False)
        if _integer(quality) > 0
    }


def _select_video_track(
    dash: dict[str, Any],
    quality_labels: dict[int, str],
) -> BilibiliMediaTrack | None:
    """Prefer the highest quality, then AVC compatibility, then bandwidth."""
    raw_tracks = dash.get("video")
    track_values = cast(list[object], raw_tracks) if isinstance(raw_tracks, list) else []
    tracks = [track for value in track_values if (track := _object(value)) and _track_url(track)]
    if not tracks:
        return None
    highest_quality = max(_integer(track.get("id")) for track in tracks)
    candidates = [track for track in tracks if _integer(track.get("id")) == highest_quality]
    selected = max(
        candidates,
        key=lambda track: (
            _codec_preference(_string(track.get("codecs"))),
            _integer(track.get("bandwidth")),
        ),
    )
    codec = _string(selected.get("codecs"))
    return BilibiliMediaTrack(
        kind="video",
        url=_track_url(selected),
        quality_id=highest_quality,
        quality_label=quality_labels.get(highest_quality, str(highest_quality)),
        codec=codec,
        bandwidth=_integer(selected.get("bandwidth")),
    )


def _select_audio_track(dash: dict[str, Any]) -> BilibiliMediaTrack | None:
    """Select the highest-bandwidth regular audio track for broad mux compatibility."""
    raw_tracks = dash.get("audio")
    track_values = cast(list[object], raw_tracks) if isinstance(raw_tracks, list) else []
    tracks = [track for value in track_values if (track := _object(value)) and _track_url(track)]
    if not tracks:
        return None
    selected = max(tracks, key=lambda track: _integer(track.get("bandwidth")))
    return BilibiliMediaTrack(
        kind="audio",
        url=_track_url(selected),
        quality_id=_optional_integer(selected.get("id")),
        quality_label="audio",
        codec=_string(selected.get("codecs")) or "mp4a",
        bandwidth=_integer(selected.get("bandwidth")),
    )


def _select_progressive_track(
    playinfo: dict[str, Any],
    quality_labels: dict[int, str],
) -> BilibiliMediaTrack | None:
    """Fall back to one combined progressive stream when DASH is unavailable."""
    raw_tracks = playinfo.get("durl")
    tracks = cast(list[object], raw_tracks) if isinstance(raw_tracks, list) else []
    first = _object(tracks[0]) if tracks else {}
    url = _string(first.get("url"))
    if not url:
        return None
    quality_id = _optional_integer(playinfo.get("quality"))
    return BilibiliMediaTrack(
        kind="video",
        url=url,
        quality_id=quality_id,
        quality_label=quality_labels.get(quality_id or 0, str(quality_id or "progressive")),
        codec="progressive",
        bandwidth=0,
    )


def _track_url(track: dict[str, Any]) -> str:
    """Read a primary or backup DASH URL across Bilibili field variants."""
    primary = _string(track.get("baseUrl") or track.get("base_url"))
    if primary:
        return primary
    raw_backups = track.get("backupUrl") or track.get("backup_url")
    backups = cast(list[object], raw_backups) if isinstance(raw_backups, list) else []
    return next((_string(value) for value in backups if _string(value)), "")


def _codec_preference(codec: str) -> int:
    """Rank AVC above HEVC and AV1 for the broadest local playback compatibility."""
    lowered = codec.lower()
    if lowered.startswith(("avc", "avc1")):
        return 3
    if lowered.startswith(("hev", "hvc")):
        return 2
    if lowered.startswith("av01"):
        return 1
    return 0


def _strip_markup(value: str) -> str:
    """Remove search-highlight markup and decode HTML entities from one title."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())


def _absolute_bilibili_url(value: str) -> str:
    """Normalize Bilibili asset URLs to HTTPS."""
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://"):
        return f"https://{value.removeprefix('http://')}"
    return value


def _seconds_to_milliseconds(value: int | None) -> int | None:
    """Convert a positive Unix-seconds value to milliseconds."""
    return value * 1000 if value is not None and value > 0 else None


def _format_milliseconds(value: int | None) -> str:
    """Format one Unix timestamp in the project's China-time display convention."""
    if value is None:
        return ""
    instant = datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    return instant.isoformat(timespec="seconds")


def _object(value: object) -> dict[str, Any]:
    """Return JSON objects or an empty mapping for absent variants."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    """Return JSON strings without coercing nested values."""
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int:
    """Return an integer-like scalar or zero for invalid values."""
    parsed = _optional_integer(value)
    return parsed if parsed is not None else 0


def _optional_integer(value: object) -> int | None:
    """Parse an integer-like scalar without treating booleans as counts."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None
