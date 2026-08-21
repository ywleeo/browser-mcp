"""Deterministic tests for Douyin URL and signed-response normalization."""

from __future__ import annotations

import json

import pytest

from browser_mcp.sites.douyin import (
    DouyinParseError,
    parse_douyin_aweme_url,
    shape_douyin_comments,
    shape_douyin_search,
    shape_douyin_video,
)
from browser_mcp.sites.models import DouyinCommentsRequest, DouyinSearchRequest


def _aweme(aweme_id: str = "7478048831087725875") -> dict[str, object]:
    """Build one representative Douyin video payload shared by parser tests."""
    return {
        "aweme_id": aweme_id,
        "desc": "牵手 APP 体验",
        "create_time": 1_741_118_946,
        "author": {
            "uid": "author-1",
            "sec_uid": "sec-author-1",
            "nickname": "测试作者",
        },
        "statistics": {
            "digg_count": 265,
            "comment_count": 22,
            "collect_count": 131,
            "share_count": 49,
        },
        "video": {
            "duration": 256_000,
            "cover": {"url_list": ["https://img.example/cover.jpg"]},
            "play_addr": {"url_list": ["https://video.example/play.mp4"]},
        },
        "music": {"title": "原声", "author": "测试作者"},
    }


def test_parse_douyin_aweme_url_accepts_video_and_note_pages() -> None:
    """Only canonical numeric post identities should cross the site boundary."""
    video = parse_douyin_aweme_url("https://www.douyin.com/video/7478048831087725875")
    note = parse_douyin_aweme_url("https://douyin.com/note/123456/")

    assert video.aweme_id == "7478048831087725875"
    assert video.page_kind == "video"
    assert note.page_kind == "note"
    with pytest.raises(DouyinParseError):
        parse_douyin_aweme_url("https://example.com/video/123")


def test_shape_douyin_search_decodes_concatenated_stream_and_deduplicates() -> None:
    """Streaming response objects should produce stable canonical search cards."""
    payload = {"status_code": 0, "data": [{"type": 1, "aweme_info": _aweme()}]}
    body = json.dumps({"ack": 0}) + json.dumps(payload) + json.dumps(payload)

    result = shape_douyin_search(
        {"body": body},
        DouyinSearchRequest(keyword="牵手 APP", limit=20),
    )

    assert len(result.items) == 1
    assert result.items[0].author == "测试作者"
    assert result.items[0].comments == 22
    assert result.items[0].url.endswith("/video/7478048831087725875")


def test_shape_douyin_video_extracts_statistics_media_and_music() -> None:
    """Aweme detail responses should expose useful metadata without raw response noise."""
    identity = parse_douyin_aweme_url("https://www.douyin.com/video/7478048831087725875")

    result = shape_douyin_video({"aweme_detail": _aweme()}, identity)

    assert result.description == "牵手 APP 体验"
    assert result.likes == 265
    assert result.media_urls == ("https://video.example/play.mp4",)
    assert result.music_title == "原声"


def test_shape_douyin_note_accepts_react_flight_camel_case_media() -> None:
    """Image-note SSR data should normalize without depending on the detail API shape."""
    identity = parse_douyin_aweme_url("https://www.douyin.com/note/7665572565292846346")
    raw = {
        "aweme_detail": {
            "awemeId": "7665572565292846346",
            "awemeType": 68,
            "desc": "旅行图文",
            "createTime": 1_784_780_197,
            "authorInfo": {"uid": "author-2", "secUid": "sec-2", "nickname": "作者"},
            "stats": {
                "diggCount": 39,
                "commentCount": 8,
                "collectCount": 38,
                "shareCount": 13,
            },
            "images": [
                {
                    "urlList": [
                        "https://p3-pc-sign.douyinpic.com/image-1.webp",
                        "https://p9-pc-sign.douyinpic.com/image-1.webp",
                    ]
                },
                {"urlList": ["https://p3-pc-sign.douyinpic.com/image-2.webp"]},
            ],
            "video": {
                "duration": 0,
                "coverUrlList": ["https://p3-pc-sign.douyinpic.com/cover.webp"],
            },
            "music": {"title": "配乐", "author": "音乐人"},
        }
    }

    result = shape_douyin_video(raw, identity)

    assert result.aweme_type == "note"
    assert result.author_id == "author-2"
    assert result.sec_uid == "sec-2"
    assert result.likes == 39
    assert result.cover_url.endswith("image-1.webp")
    assert result.media_urls == (
        "https://p3-pc-sign.douyinpic.com/image-1.webp",
        "https://p3-pc-sign.douyinpic.com/image-2.webp",
    )


def test_shape_douyin_comments_flattens_replies_and_honors_limit() -> None:
    """Root comments and inline replies should retain thread identities and completeness."""
    identity = parse_douyin_aweme_url("https://www.douyin.com/video/7478048831087725875")
    raw = {
        "complete": True,
        "scrolls": 3,
        "pages": [
            {
                "kind": "root",
                "payload": {
                    "total": 2,
                    "comments": [
                        {
                            "cid": "c1",
                            "text": "主评论",
                            "create_time": 1_700_000_000,
                            "digg_count": 5,
                            "reply_comment_total": 1,
                            "user": {"uid": "u1", "nickname": "甲"},
                            "reply_comment": [
                                {
                                    "cid": "c2",
                                    "reply_comment_id": "c1",
                                    "reply_id": "c1",
                                    "text": "回复",
                                    "user": {"uid": "u2", "nickname": "乙"},
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }

    result = shape_douyin_comments(
        raw,
        identity,
        DouyinCommentsRequest.model_validate(
            {"url": "https://www.douyin.com/video/7478048831087725875", "max_comments": 2}
        ),
    )

    assert result.complete is True
    assert result.fetched == 2
    assert result.items[1].root_comment_id == "c1"
    assert result.items[1].parent_comment_id == "c1"
    assert result.items[1].depth == 1
    assert result.session_id is None
    assert result.budget_exhausted is False


def test_shape_douyin_comments_keeps_resume_ticket_for_a_budget_stop() -> None:
    """A budget stop returns the comments collected so far plus a resumable session."""
    identity = parse_douyin_aweme_url("https://www.douyin.com/video/7478048831087725875")
    raw = {
        "complete": False,
        "budget_exhausted": True,
        "session_id": "session-2",
        "collected_total": 31,
        "scrolls": 96,
        "pages": [
            {
                "kind": "root",
                "payload": {
                    "total": 240,
                    "comments": [
                        {
                            "cid": "c1",
                            "text": "根评论",
                            "create_time": 1_700_000_000,
                            "user": {"uid": "u1", "nickname": "作者"},
                        }
                    ],
                },
            }
        ],
    }

    result = shape_douyin_comments(
        raw,
        identity,
        DouyinCommentsRequest.model_validate(
            {"url": "https://www.douyin.com/video/7478048831087725875"}
        ),
    )

    assert result.complete is False
    assert result.budget_exhausted is True
    assert result.session_id == "session-2"
    assert result.collected_total == 31
    assert result.total == 240
    assert result.fetched == 1
