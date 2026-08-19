"""Deterministic tests for Xiaohongshu URL and response normalization."""

from __future__ import annotations

import pytest

from browser_mcp.sites.models import (
    XhsCommentsRequest,
    XhsSearchRequest,
    XhsSort,
    XhsUserNotesRequest,
)
from browser_mcp.sites.xhs import (
    XhsParseError,
    parse_xhs_note_url,
    shape_xhs_comments,
    shape_xhs_note,
    shape_xhs_search,
    shape_xhs_user_notes,
)


def test_parse_xhs_note_url_preserves_signed_parameters() -> None:
    """Explore URLs should retain the token needed for later authenticated reads."""
    identity = parse_xhs_note_url(
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=a%2Bb&xsec_source=pc_search"
    )

    assert identity.note_id == "abc123"
    assert identity.xsec_token == "a+b"
    assert identity.xsec_source == "pc_search"
    with pytest.raises(XhsParseError):
        parse_xhs_note_url("https://example.com/explore/abc123")


def test_shape_xhs_search_returns_stable_note_cards() -> None:
    """Signed search responses should become canonical note URLs and display metadata."""
    request = XhsSearchRequest(keyword="露营", page=2, sort=XhsSort.LATEST)
    raw = {
        "data": {
            "has_more": True,
            "items": [
                {
                    "id": "note-1",
                    "xsec_token": "token+/=",
                    "note_card": {
                        "display_title": "周末露营",
                        "type": "video",
                        "user": {"nickname": "小红"},
                        "cover": {"url_default": "https://img.example/cover.jpg"},
                        "interact_info": {
                            "liked_count": "1万+",
                            "collected_count": "88",
                            "comment_count": 7,
                        },
                    },
                }
            ],
        }
    }

    result = shape_xhs_search(raw, request)

    assert result.has_more is True
    assert result.items[0].index == 21
    assert result.items[0].likes == "1万+"
    assert "xsec_token=token%2B%2F%3D" in result.items[0].url


def test_shape_xhs_note_extracts_images_video_and_statistics() -> None:
    """SSR note state should expose best media URLs and normalized counters."""
    identity = parse_xhs_note_url(
        "https://www.xiaohongshu.com/explore/n1?xsec_token=token&xsec_source=pc_search"
    )
    raw = {
        "note": {
            "noteDetailMap": {
                "n1": {
                    "note": {
                        "type": "video",
                        "title": "视频笔记",
                        "desc": "正文",
                        "time": 1_700_000_000_000,
                        "user": {"nickname": "作者"},
                        "interactInfo": {
                            "likedCount": "100",
                            "collectedCount": 20,
                            "commentCount": "3",
                        },
                        "imageList": [
                            {"urlDefault": "https://img.example/1.jpg"},
                            {"url": "https://img.example/2.jpg"},
                        ],
                        "video": {
                            "media": {
                                "stream": {
                                    "EF4": [
                                        {"masterUrl": "https://video.example/fallback.mp4"},
                                        {
                                            "defaultStream": 1,
                                            "master_url": "https://video.example/master.mp4",
                                        },
                                    ]
                                }
                            }
                        },
                    }
                }
            }
        }
    }

    result = shape_xhs_note(raw, identity)

    assert result.title == "视频笔记"
    assert result.author == "作者"
    assert [image.url for image in result.images] == [
        "https://img.example/1.jpg",
        "https://img.example/2.jpg",
    ]
    assert result.video_url == "https://video.example/master.mp4"


def test_shape_xhs_user_notes_merges_pages_and_deduplicates_notes() -> None:
    """SSR camel-case cards and API snake-case pages should form one stable list."""
    request = XhsUserNotesRequest(user_id="user-1", max_pages=3)
    raw = {
        "user_id": "user-1",
        "nickname": "作者",
        "red_id": "red-1",
        "complete": True,
        "pages_fetched": 2,
        "pages": [
            {
                "cursor": "cursor-1",
                "has_more": True,
                "notes": [
                    {
                        "id": "note-1",
                        "xsecToken": "token-1",
                        "noteCard": {
                            "noteId": "note-1",
                            "displayTitle": "置顶首篇",
                            "time": 1_700_000_000_000,
                            "type": "normal",
                            "user": {"nickname": "作者"},
                            "cover": {"urlDefault": "https://img.example/1.jpg"},
                            "interactInfo": {"likedCount": "100", "sticky": True},
                        },
                    }
                ],
            },
            {
                "cursor": "",
                "has_more": False,
                "notes": [
                    {
                        "note_id": "note-1",
                        "display_title": "重复项",
                    },
                    {
                        "note_id": "note-2",
                        "xsec_token": "token-2",
                        "display_title": "第二篇",
                        "time": "1700086400000",
                        "type": "video",
                        "user": {"nick_name": "作者"},
                        "cover": {
                            "info_list": [
                                {
                                    "image_scene": "WB_DFT",
                                    "url": "https://img.example/2.jpg",
                                }
                            ]
                        },
                        "interact_info": {"liked_count": "23", "sticky": False},
                    },
                ],
            },
        ],
    }

    result = shape_xhs_user_notes(raw, request)

    assert result.complete is True
    assert result.pages_fetched == 2
    assert result.has_more is False
    assert [item.note_id for item in result.items] == ["note-1", "note-2"]
    assert result.items[0].is_sticky is True
    assert result.items[1].cover == "https://img.example/2.jpg"
    assert result.items[1].published_at == "2023-11-16"
    assert "xsec_source=pc_user" in result.items[1].url


def test_shape_xhs_comments_flattens_replies_and_deduplicates_pages() -> None:
    """Root pages and expanded reply pages should form one stable flat thread."""
    request = XhsCommentsRequest.model_validate(
        {
            "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=token&xsec_source=pc_search",
            "max_comments": 20,
        }
    )
    identity = parse_xhs_note_url(str(request.url))
    reply = {
        "id": "c2",
        "content": "首条回复",
        "create_time": 1_700_000_001,
        "user_info": {"user_id": "u2", "nickname": "回复者"},
        "target_comment": {"id": "c1", "user_info": {"nickname": "作者"}},
    }
    raw = {
        "expected_count": 3,
        "complete": True,
        "scrolls": 4,
        "pages": [
            {
                "kind": "root",
                "payload": {
                    "data": {
                        "has_more": False,
                        "comments": [
                            {
                                "id": "c1",
                                "content": "顶层评论",
                                "create_time": 1_700_000_000,
                                "ip_location": "上海",
                                "liked_count": "12",
                                "sub_comment_count": 2,
                                "user_info": {"user_id": "u1", "nickname": "作者"},
                                "sub_comments": [reply],
                            }
                        ],
                    }
                },
            },
            {
                "kind": "sub",
                "root_comment_id": "c1",
                "payload": {
                    "data": {
                        "comments": [
                            reply,
                            {
                                "id": "c3",
                                "content": "第二条回复",
                                "create_time": 1_700_000_002_000,
                                "user_info": {"user_id": "u3", "nickname": "另一人"},
                                "target_comment": {
                                    "id": "c2",
                                    "user_info": {"nickname": "回复者"},
                                },
                            },
                        ]
                    }
                },
            },
        ],
    }

    result = shape_xhs_comments(raw, identity, request)

    assert result.complete is True
    assert result.fetched == 3
    assert [comment.comment_id for comment in result.items] == ["c1", "c2", "c3"]
    assert result.items[1].root_comment_id == "c1"
    assert result.items[2].parent_comment_id == "c2"
    assert result.items[2].reply_to == "回复者"
    assert result.items[1].published_at_ms == 1_700_000_001_000
