"""Tests for Bilibili URL, metadata, search, and media normalization."""

from __future__ import annotations

import pytest

from browser_mcp.sites.bilibili import (
    BilibiliParseError,
    parse_bilibili_video_url,
    select_bilibili_media_streams,
    shape_bilibili_search,
    shape_bilibili_video,
)
from browser_mcp.sites.models import BilibiliSearchRequest


def _view_envelope() -> dict[str, object]:
    """Build one deterministic multipart Bilibili view response."""
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "bvid": "BV1eaMH6gEDx",
            "aid": 116889867130412,
            "cid": 301,
            "title": "测试视频",
            "desc": "内容简介",
            "tname": "软件应用",
            "pic": "//i1.hdslb.com/cover.jpg",
            "pubdate": 1_700_000_000,
            "duration": 90,
            "owner": {"name": "作者", "mid": 42},
            "stat": {
                "view": 100,
                "danmaku": 20,
                "reply": 10,
                "favorite": 8,
                "coin": 7,
                "share": 6,
                "like": 9,
            },
            "pages": [
                {"page": 1, "cid": 301, "part": "第一集", "duration": 40},
                {"page": 2, "cid": 302, "part": "第二集", "duration": 50},
            ],
        },
    }


def test_parse_bilibili_video_url_supports_bv_av_and_multipart_pages() -> None:
    """Canonical BV and AV identities should retain one validated selected page."""
    bv = parse_bilibili_video_url("https://www.bilibili.com/video/BV1eaMH6gEDx/?p=2")
    av = parse_bilibili_video_url("https://bilibili.com/video/av116889867130412")

    assert (bv.bvid, bv.aid, bv.page) == ("BV1eaMH6gEDx", None, 2)
    assert (av.bvid, av.aid, av.page) == (None, 116889867130412, 1)
    with pytest.raises(BilibiliParseError, match="supported Bilibili host"):
        parse_bilibili_video_url("https://example.com/video/BV1eaMH6gEDx")
    with pytest.raises(BilibiliParseError, match="p must be between"):
        parse_bilibili_video_url("https://www.bilibili.com/video/BV1eaMH6gEDx?p=0")


def test_shape_bilibili_search_normalizes_highlights_urls_and_statistics() -> None:
    """Search API markup and protocol-relative assets should not leak into results."""
    request = BilibiliSearchRequest(keyword="OpenAI")
    raw = {
        "code": 0,
        "message": "OK",
        "data": {
            "numResults": 21,
            "numPages": 2,
            "result": [
                {
                    "bvid": "BV1eaMH6gEDx",
                    "aid": 116889867130412,
                    "title": '<em class="keyword">OpenAI</em> 测试',
                    "description": "简介",
                    "author": "作者",
                    "mid": 42,
                    "typename": "科技",
                    "duration": "10:43",
                    "pubdate": 1_700_000_000,
                    "play": 100,
                    "danmaku": 20,
                    "favorites": 8,
                    "review": 10,
                    "like": 9,
                    "pic": "//i1.hdslb.com/cover.jpg",
                    "tag": "AI,OpenAI",
                },
                {
                    "bvid": "BV1eaMH6gEDx",
                    "aid": 116889867130412,
                    "title": "重复卡片",
                },
            ],
        },
    }

    result = shape_bilibili_search(raw, request)

    assert result.has_more is True
    assert result.items[0].title == "OpenAI 测试"
    assert result.items[0].cover_url == "https://i1.hdslb.com/cover.jpg"
    assert result.items[0].tags == ("AI", "OpenAI")
    assert len(result.items) == 1


def test_shape_bilibili_search_upgrades_legacy_http_asset_urls() -> None:
    """Bilibili legacy HTTP cover links should be safe HTTPS metadata links."""
    request = BilibiliSearchRequest(keyword="OpenAI")
    raw = {
        "code": 0,
        "data": {
            "numResults": 1,
            "numPages": 1,
            "result": [
                {
                    "bvid": "BV1eaMH6gEDx",
                    "aid": 1,
                    "title": "测试",
                    "pic": "http://i1.hdslb.com/cover.jpg",
                }
            ],
        },
    }

    result = shape_bilibili_search(raw, request)

    assert result.items[0].cover_url == "https://i1.hdslb.com/cover.jpg"


def test_shape_bilibili_video_selects_requested_part_and_tags() -> None:
    """Metadata should expose the selected CID while retaining every multipart entry."""
    identity = parse_bilibili_video_url("https://www.bilibili.com/video/BV1eaMH6gEDx?p=2")
    raw = {
        "view": _view_envelope(),
        "tags": {
            "code": 0,
            "data": [{"tag_name": "AI"}, {"tag_name": "教程"}],
        },
    }

    result = shape_bilibili_video(raw, identity)

    assert (result.bvid, result.cid, result.page) == ("BV1eaMH6gEDx", 302, 2)
    assert result.url.endswith("?p=2")
    assert result.tags == ("AI", "教程")
    assert [part.cid for part in result.parts] == [301, 302]


def test_select_bilibili_media_prefers_best_quality_avc_and_best_audio() -> None:
    """Track selection should maximize quality while preferring compatible AVC video."""
    identity = parse_bilibili_video_url("https://www.bilibili.com/video/BV1eaMH6gEDx")
    raw = {
        "view": _view_envelope(),
        "playinfo": {
            "code": 0,
            "data": {
                "accept_quality": [80, 64],
                "accept_description": ["1080P", "720P"],
                "dash": {
                    "video": [
                        {
                            "id": 80,
                            "codecs": "hev1.1.6.L120",
                            "bandwidth": 900,
                            "baseUrl": "https://v.bilivideo.com/hevc.m4s",
                        },
                        {
                            "id": 80,
                            "codecs": "avc1.640028",
                            "bandwidth": 800,
                            "baseUrl": "https://v.bilivideo.com/avc.m4s",
                        },
                        {
                            "id": 64,
                            "codecs": "avc1.64001f",
                            "bandwidth": 700,
                            "baseUrl": "https://v.bilivideo.com/720.m4s",
                        },
                    ],
                    "audio": [
                        {
                            "id": 30216,
                            "codecs": "mp4a.40.2",
                            "bandwidth": 64,
                            "baseUrl": "https://a.bilivideo.com/low.m4s",
                        },
                        {
                            "id": 30280,
                            "codecs": "mp4a.40.2",
                            "bandwidth": 128,
                            "baseUrl": "https://a.bilivideo.com/high.m4s",
                        },
                    ],
                },
            },
        },
    }

    streams = select_bilibili_media_streams(raw, identity)

    assert streams.video.url.endswith("avc.m4s")
    assert streams.video.quality_label == "1080P"
    assert streams.audio is not None
    assert streams.audio.url.endswith("high.m4s")


def test_select_bilibili_media_accepts_backup_track_urls() -> None:
    """A backup URL should remain usable when Bilibili omits the primary DASH URL."""
    identity = parse_bilibili_video_url("https://www.bilibili.com/video/BV1eaMH6gEDx/")
    raw = {
        "view": _view_envelope(),
        "playinfo": {
            "code": 0,
            "data": {
                "accept_quality": [80],
                "accept_description": ["高清 1080P"],
                "dash": {
                    "video": [
                        {
                            "id": 80,
                            "backup_url": ["https://video.bilivideo.com/backup.m4s"],
                            "codecs": "avc1.640032",
                            "bandwidth": 1_000_000,
                        }
                    ],
                    "audio": [],
                },
            },
        },
    }

    streams = select_bilibili_media_streams(raw, identity)

    assert streams.video.url == "https://video.bilivideo.com/backup.m4s"
