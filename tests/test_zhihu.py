"""Deterministic tests for Zhihu URL, search, and SSR content parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from browser_mcp.sites.models import ZhihuSearchRequest, ZhihuSearchType
from browser_mcp.sites.zhihu import (
    ZhihuParseError,
    classify_zhihu_url,
    parse_zhihu_content,
    parse_zhihu_invitations,
    parse_zhihu_search,
)


def _ssr_html(entities: Mapping[str, object]) -> str:
    """Wrap entities in the exact Zhihu SSR script contract."""
    payload = {"initialState": {"entities": entities}}
    return f'<html><script id="js-initialData">{json.dumps(payload)}</script></html>'


def test_classify_supported_zhihu_urls_strictly() -> None:
    """Only exact numeric question, answer, and article routes should be accepted."""
    assert classify_zhihu_url("https://www.zhihu.com/question/123").kind == "question"
    answer = classify_zhihu_url("https://zhihu.com/question/123/answer/456?utm_source=x")
    assert (answer.kind, answer.question_id, answer.content_id) == ("answer", "123", "456")
    assert classify_zhihu_url("https://zhuanlan.zhihu.com/p/789").kind == "article"
    with pytest.raises(ZhihuParseError):
        classify_zhihu_url("https://evil.example/question/123")
    with pytest.raises(ZhihuParseError):
        classify_zhihu_url("https://www.zhihu.com/question/12abc")


def test_parse_zhihu_search_builds_canonical_content_urls() -> None:
    """Search records should omit ads and preserve distinct question and answer ids."""
    request = ZhihuSearchRequest(keyword="浏览器 MCP", search_type=ZhihuSearchType.CONTENT)
    raw: dict[str, Any] = {
        "data": [
            {
                "type": "search_result",
                "object": {
                    "type": "answer",
                    "id": 456,
                    "question": {"id": 123, "title": "<b>问题</b>"},
                    "author": {"name": "回答者"},
                    "excerpt": "<p>答案摘要</p>",
                    "voteup_count": 9,
                    "comment_count": 2,
                },
            },
            {
                "type": "search_result",
                "object": {
                    "type": "article",
                    "url": "https://api.zhihu.com/articles/789",
                    "title": "文章",
                    "author": {"name": "作者"},
                },
            },
            {"type": "ad", "object": {"type": "article", "id": 999}},
        ],
        "paging": {"is_end": False},
    }

    result = parse_zhihu_search(raw, request)

    assert result.has_more is True
    assert [item.url for item in result.items] == [
        "https://www.zhihu.com/question/123/answer/456",
        "https://zhuanlan.zhihu.com/p/789",
    ]
    assert result.items[0].title == "问题"
    assert result.items[0].excerpt == "答案摘要"


def test_parse_zhihu_question_renders_embedded_answers_by_votes() -> None:
    """Question output should retain detail and sort SSR answers by vote count."""
    html = _ssr_html(
        {
            "questions": {
                "123": {
                    "title": "测试问题",
                    "detail": "<p>问题详情</p>",
                    "answerCount": 3,
                    "followerCount": 8,
                }
            },
            "answers": {
                "1": {
                    "question": {"id": "123"},
                    "author": {"name": "甲"},
                    "content": "<p>低票答案</p>",
                    "voteupCount": 1,
                },
                "2": {
                    "question": "123",
                    "author": {"name": "乙"},
                    "content": "<p>高票答案</p>",
                    "voteupCount": 20,
                },
            },
            "users": {},
        }
    )

    document = parse_zhihu_content(html, classify_zhihu_url("https://zhihu.com/question/123"))

    assert document.kind == "question"
    assert document.title == "测试问题"
    assert document.content.index("高票答案") < document.content.index("低票答案")
    assert "另有 1 个回答未包含" in document.content


def test_parse_zhihu_answer_and_article() -> None:
    """Answer and article entities should render their canonical metadata and bodies."""
    entities = {
        "questions": {"123": {"title": "问题标题"}},
        "answers": {
            "456": {
                "author": {"name": "回答者"},
                "content": "<p>第一段</p><p>第二段</p>",
                "voteupCount": 10,
                "commentCount": 3,
                "createdTime": 1_700_000_000,
            }
        },
        "articles": {
            "789": {
                "title": "文章标题",
                "author": {"name": "作者"},
                "content": "<p>正文</p>",
                "voteupCount": 7,
                "commentCount": 1,
                "created": 1_700_000_000,
            }
        },
        "users": {},
    }
    html = _ssr_html(entities)

    answer = parse_zhihu_content(
        html,
        classify_zhihu_url("https://www.zhihu.com/question/123/answer/456"),
    )
    article = parse_zhihu_content(
        html,
        classify_zhihu_url("https://zhuanlan.zhihu.com/p/789"),
    )

    assert answer.title == "问题标题"
    assert "第一段\n第二段" in answer.content
    assert article.title == "文章标题"
    assert "作者：作者" in article.content


def test_parse_zhihu_invitations_filters_day_and_normalizes_sources() -> None:
    """Invitation pages should filter by China date and distinguish members from system."""
    china = ZoneInfo("Asia/Shanghai")

    def timestamp(day: int, hour: int) -> int:
        """Build one deterministic China-local Unix timestamp."""
        return int(datetime(2026, 8, day, hour, tzinfo=china).timestamp())

    raw: dict[str, Any] = {
        "complete": True,
        "pages": [
            {
                "data": [
                    {
                        "create_time": timestamp(12, 8),
                        "merge_count": 2,
                        "content": {
                            "verb": "邀请你回答问题",
                            "actors": [{"name": "邀请人"}],
                            "target": {
                                "text": "今天的问题",
                                "link": "https://www.zhihu.com/question/123?utm_source=notification",
                            },
                        },
                    },
                    {
                        "create_time": timestamp(12, 1),
                        "content": {
                            "verb": "邀请你回答",
                            "actors": [],
                            "target": {
                                "text": "系统邀请",
                                "link": "https://www.zhihu.com/question/456",
                            },
                        },
                    },
                    {
                        "create_time": timestamp(11, 23),
                        "content": {
                            "target": {
                                "text": "昨天的问题",
                                "link": "https://www.zhihu.com/question/789",
                            }
                        },
                    },
                ]
            }
        ],
    }

    result = parse_zhihu_invitations(raw, date(2026, 8, 12))

    assert result.complete is True
    assert result.pages_fetched == 1
    assert [item.question for item in result.items] == ["今天的问题", "系统邀请"]
    assert result.items[0].inviters == ("邀请人",)
    assert result.items[0].source == "member"
    assert result.items[0].merge_count == 2
    assert result.items[1].source == "system"
    assert result.items[0].url == "https://www.zhihu.com/question/123"
