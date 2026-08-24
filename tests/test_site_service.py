"""Application tests for site adapter transport and immutable pagination."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from browser_mcp.application import BrowserService
from browser_mcp.config import AppSettings
from browser_mcp.models import BrowserFetchPayload
from browser_mcp.sites.auth import SiteLoginRequiredError
from browser_mcp.sites.media import MediaDownloader
from browser_mcp.sites.models import (
    BilibiliSearchRequest,
    BilibiliVideoRequest,
    DouyinCommentsRequest,
    DouyinDownloadRequest,
    DouyinSearchRequest,
    DouyinVideoRequest,
    SiteEngagementRequest,
    SitePageRequest,
    WebSearchRequest,
    XhsCommentsRequest,
    XhsDownloadRequest,
    XhsNoteRequest,
    XhsSearchRequest,
    XhsUserNotesRequest,
    XSearchRequest,
    ZhihuContentRequest,
    ZhihuInvitationsRequest,
    ZhihuSearchRequest,
)
from browser_mcp.sites.service import SiteService
from tests.helpers import FakeBridge, allow_public_url_policy


def _zhihu_payload() -> BrowserFetchPayload:
    """Build one rendered Zhihu answer payload for service tests."""
    state = {
        "initialState": {
            "entities": {
                "questions": {"123": {"title": "服务测试"}},
                "answers": {
                    "456": {
                        "author": {"name": "作者"},
                        "content": "<p>" + "内容" * 20 + "</p>",
                    }
                },
                "users": {},
            }
        }
    }
    html = f'<script id="js-initialData">{json.dumps(state)}</script>'
    return BrowserFetchPayload(
        final_url="https://www.zhihu.com/question/123/answer/456",
        html=html,
    )


@pytest.mark.asyncio
async def test_bilibili_tools_use_their_isolated_extension_namespace(tmp_path: Path) -> None:
    """Bilibili search and metadata should dispatch only through bilibili.fetch."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("bilibili.fetch", "search")] = {
        "code": 0,
        "data": {"numResults": 0, "numPages": 0, "result": []},
    }
    bridge.site_responses[("bilibili.fetch", "video")] = {
        "view": {
            "code": 0,
            "data": {
                "bvid": "BV1eaMH6gEDx",
                "aid": 116889867130412,
                "cid": 301,
                "title": "测试视频",
                "owner": {},
                "stat": {},
                "pages": [{"page": 1, "cid": 301, "part": "P1", "duration": 10}],
            },
        },
        "tags": {"code": 0, "data": []},
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    search = await service.bilibili_search(BilibiliSearchRequest(keyword="OpenAI"))
    video = await service.bilibili_video(
        BilibiliVideoRequest.model_validate({"url": "https://www.bilibili.com/video/BV1eaMH6gEDx/"})
    )

    assert search.items == ()
    assert video.cid == 301
    assert bridge.site_requests == [
        (
            "bilibili.fetch",
            "search",
            {"keyword": "OpenAI", "page": 1, "order": "totalrank"},
        ),
        (
            "bilibili.fetch",
            "video",
            {"videoId": "BV1eaMH6gEDx", "page": 1},
        ),
    ]


@pytest.mark.asyncio
async def test_site_service_routes_namespaced_search_requests(tmp_path: Path) -> None:
    """Zhihu and XHS searches should use only their isolated extension namespaces."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("zhihu.fetch", "search")] = {"data": [], "paging": {"is_end": True}}
    bridge.site_responses[("xhs.fetch", "search")] = {"data": {"items": [], "has_more": False}}
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    zhihu = await service.zhihu_search(ZhihuSearchRequest(keyword="MCP"))
    xhs = await service.xhs_search(XhsSearchRequest(keyword="MCP"))

    assert zhihu.items == ()
    assert xhs.items == ()
    assert bridge.site_requests[0][:2] == ("zhihu.fetch", "search")
    assert bridge.site_requests[1][:2] == ("xhs.fetch", "search")


@pytest.mark.asyncio
async def test_zhihu_content_is_snapshotted_and_pageable(tmp_path: Path) -> None:
    """Long normalized content should continue without a second network fetch."""
    bridge = FakeBridge(tmp_path / "extension", _zhihu_payload())
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    first = await service.zhihu_content(
        ZhihuContentRequest.model_validate(
            {
                "url": "https://www.zhihu.com/question/123/answer/456",
                "max_chars": 20,
            }
        )
    )
    second = await service.read_page(
        SitePageRequest(snapshot_id=first.snapshot_id, offset=first.next_offset or 0, max_chars=20)
    )

    assert first.complete is False
    assert second.range_start == first.range_end
    assert len(bridge.fetches) == 2


@pytest.mark.asyncio
async def test_xhs_note_passes_security_parameters_to_extension(tmp_path: Path) -> None:
    """The signed token should survive request validation and bridge dispatch."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("xhs.fetch", "note")] = {
        "note": {
            "noteDetailMap": {"n1": {"note": {"title": "笔记", "user": {}, "interactInfo": {}}}}
        }
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    request = XhsNoteRequest.model_validate(
        {"url": ("https://www.xiaohongshu.com/explore/n1?xsec_token=a%2Bb&xsec_source=pc_search")}
    )

    result = await service.xhs_note(request)

    assert result.title == "笔记"
    assert bridge.site_requests[0][2]["xsecToken"] == "a+b"


@pytest.mark.asyncio
async def test_xhs_comments_routes_stream_limit_and_security_parameters(tmp_path: Path) -> None:
    """Comment collection should use its isolated adapter and preserve signed URL data."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("xhs.fetch", "comments")] = {
        "expected_count": 0,
        "complete": True,
        "pages": [],
        "scrolls": 1,
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    request = XhsCommentsRequest.model_validate(
        {
            "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=a%2Bb&xsec_source=pc_search",
            "max_comments": 123,
        }
    )

    result = await service.xhs_comments(request)

    assert result.complete is True
    assert bridge.site_requests[0] == (
        "xhs.fetch",
        "comments",
        {
            "noteId": "n1",
            "xsecToken": "a+b",
            "xsecSource": "pc_search",
            "maxComments": 123,
            "budgetMs": 40_000,
            "sessionId": "",
        },
    )


@pytest.mark.asyncio
async def test_xhs_comments_resume_forwards_session_and_sizes_its_own_timeout(
    tmp_path: Path,
) -> None:
    """A budgeted call must outlive its own budget and carry the resume ticket unchanged."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("xhs.fetch", "comments")] = {
        "expected_count": 185,
        "complete": False,
        "budget_exhausted": True,
        "session_id": "session-1",
        "collected_total": 60,
        "pages": [],
        "scrolls": 40,
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    request = XhsCommentsRequest.model_validate(
        {
            "url": "https://www.xiaohongshu.com/explore/n1?xsec_token=t&xsec_source=pc_search",
            "session_id": "session-1",
            "time_budget_seconds": 90,
        }
    )

    result = await service.xhs_comments(request)

    assert result.complete is False
    assert result.budget_exhausted is True
    assert result.session_id == "session-1"
    assert result.collected_total == 60
    assert bridge.site_requests[0][2]["budgetMs"] == 90_000
    assert bridge.site_requests[0][2]["sessionId"] == "session-1"
    assert bridge.site_timeouts[0] == 105.0


@pytest.mark.asyncio
async def test_douyin_comments_forwards_budget_and_session(tmp_path: Path) -> None:
    """Douyin collection shares the budgeted, resumable contract with Xiaohongshu."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("douyin.fetch", "comments")] = {
        "complete": False,
        "budget_exhausted": True,
        "session_id": "session-2",
        "collected_total": 12,
        "pages": [],
        "scrolls": 80,
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    request = DouyinCommentsRequest.model_validate(
        {
            "url": "https://www.douyin.com/video/7478048831087725875",
            "session_id": "session-2",
        }
    )

    result = await service.douyin_comments(request)

    assert result.session_id == "session-2"
    assert result.budget_exhausted is True
    assert bridge.site_requests[0][2]["sessionId"] == "session-2"
    assert bridge.site_requests[0][2]["budgetMs"] == 40_000
    assert bridge.site_timeouts[0] == 55.0


@pytest.mark.asyncio
async def test_xhs_user_notes_defaults_to_logged_in_account_and_bounds_pages(
    tmp_path: Path,
) -> None:
    """The service should leave account discovery to Chrome and pass the page budget."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("xhs.fetch", "user_notes")] = {
        "user_id": "logged-in-user",
        "nickname": "作者",
        "complete": True,
        "pages_fetched": 1,
        "pages": [{"notes": [], "cursor": "", "has_more": False}],
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    result = await service.xhs_user_notes(XhsUserNotesRequest(max_pages=3))

    assert result.user_id == "logged-in-user"
    assert result.complete is True
    assert bridge.site_requests[0] == (
        "xhs.fetch",
        "user_notes",
        {"userId": "", "maxPages": 3},
    )


@pytest.mark.asyncio
async def test_douyin_tools_use_their_isolated_extension_namespace(tmp_path: Path) -> None:
    """Search, detail, and comments should dispatch only through douyin.fetch."""
    bridge = FakeBridge(tmp_path / "extension")
    aweme: dict[str, object] = {
        "aweme_id": "7478048831087725875",
        "desc": "测试作品",
        "author": {},
        "statistics": {},
    }
    bridge.site_responses[("douyin.fetch", "search")] = {
        "chunks": [{"data": [{"aweme_info": aweme}]}]
    }
    bridge.site_responses[("douyin.fetch", "video")] = {"aweme_detail": aweme}
    bridge.site_responses[("douyin.fetch", "comments")] = {
        "complete": True,
        "pages": [],
        "scrolls": 1,
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    url = "https://www.douyin.com/video/7478048831087725875"

    search = await service.douyin_search(DouyinSearchRequest(keyword="牵手 APP", limit=10))
    video = await service.douyin_video(DouyinVideoRequest.model_validate({"url": url}))
    comments = await service.douyin_comments(
        DouyinCommentsRequest.model_validate({"url": url, "max_comments": 123})
    )

    assert search.items[0].aweme_id == "7478048831087725875"
    assert video.description == "测试作品"
    assert comments.complete is True
    assert [request[:2] for request in bridge.site_requests] == [
        ("douyin.fetch", "search"),
        ("douyin.fetch", "video"),
        ("douyin.fetch", "comments"),
    ]
    assert bridge.site_requests[2][2]["maxComments"] == 123


@pytest.mark.asyncio
async def test_engagement_tools_set_desired_state_through_mutation_namespaces(
    tmp_path: Path,
) -> None:
    """Each platform should isolate mutations and preserve idempotent desired-state input."""
    bridge = FakeBridge(tmp_path / "extension")
    xhs_url = "https://www.xiaohongshu.com/explore/n1?xsec_token=a%2Bb&xsec_source=pc_search"
    douyin_url = "https://www.douyin.com/video/7478048831087725875"
    for action, enabled in (("like", True), ("collect", False)):
        bridge.site_responses[("xhs.mutate", action)] = {
            "platform": "xhs",
            "post_id": "n1",
            "action": action,
            "requested_state": enabled,
            "active": enabled,
            "changed": True,
            "url": xhs_url,
        }
        bridge.site_responses[("douyin.mutate", action)] = {
            "platform": "douyin",
            "post_id": "7478048831087725875",
            "action": action,
            "requested_state": enabled,
            "active": enabled,
            "changed": False,
            "url": douyin_url,
        }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    xhs_like = await service.xhs_like(
        SiteEngagementRequest.model_validate({"url": xhs_url, "enabled": True})
    )
    xhs_collect = await service.xhs_collect(
        SiteEngagementRequest.model_validate({"url": xhs_url, "enabled": False})
    )
    douyin_like = await service.douyin_like(
        SiteEngagementRequest.model_validate({"url": douyin_url, "enabled": True})
    )
    douyin_collect = await service.douyin_collect(
        SiteEngagementRequest.model_validate({"url": douyin_url, "enabled": False})
    )

    assert (xhs_like.active, xhs_collect.active) == (True, False)
    assert (douyin_like.changed, douyin_collect.changed) == (False, False)
    assert bridge.site_requests == [
        (
            "xhs.mutate",
            "like",
            {
                "noteId": "n1",
                "xsecToken": "a+b",
                "xsecSource": "pc_search",
                "enabled": True,
            },
        ),
        (
            "xhs.mutate",
            "collect",
            {
                "noteId": "n1",
                "xsecToken": "a+b",
                "xsecSource": "pc_search",
                "enabled": False,
            },
        ),
        (
            "douyin.mutate",
            "like",
            {"awemeId": "7478048831087725875", "pageKind": "video", "enabled": True},
        ),
        (
            "douyin.mutate",
            "collect",
            {"awemeId": "7478048831087725875", "pageKind": "video", "enabled": False},
        ),
    ]


@pytest.mark.asyncio
async def test_platform_downloads_resolve_media_through_site_detail(tmp_path: Path) -> None:
    """Download tools should reuse normalized XHS and Douyin detail adapters before writing."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("xhs.fetch", "note")] = {
        "note": {
            "noteDetailMap": {
                "n1": {
                    "note": {
                        "title": "图文",
                        "user": {},
                        "interactInfo": {},
                        "imageList": [{"urlDefault": "https://sns-img.xhscdn.com/image"}],
                    }
                }
            }
        }
    }
    bridge.site_responses[("douyin.fetch", "video")] = {
        "aweme_detail": {
            "aweme_id": "7478048831087725875",
            "desc": "视频",
            "author": {},
            "statistics": {},
            "video": {
                "play_addr": {"url_list": ["https://v.douyinvod.com/video"]},
            },
        }
    }

    def handle(request: httpx.Request) -> httpx.Response:
        """Return deterministic image or video bytes for approved test CDNs."""
        is_image = request.url.host.endswith("xhscdn.com")
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg" if is_image else "video/mp4"},
            content=b"image" if is_image else b"video",
            request=request,
        )

    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    downloader = MediaDownloader(
        tmp_path / "downloads",
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    service = SiteService(browser, media_downloader=downloader)

    xhs = await service.xhs_download(
        XhsDownloadRequest.model_validate(
            {
                "url": (
                    "https://www.xiaohongshu.com/explore/n1?xsec_token=token&xsec_source=pc_search"
                ),
                "media": "images",
            }
        )
    )
    douyin = await service.douyin_download(
        DouyinDownloadRequest.model_validate(
            {"url": "https://www.douyin.com/video/7478048831087725875", "media": "video"}
        )
    )

    assert Path(xhs.items[0].path).read_bytes() == b"image"
    assert Path(douyin.items[0].path).read_bytes() == b"video"
    assert [request[:2] for request in bridge.site_requests] == [
        ("xhs.fetch", "note"),
        ("douyin.fetch", "video"),
    ]


@pytest.mark.asyncio
async def test_zhihu_invitations_passes_day_boundary_and_page_limit(tmp_path: Path) -> None:
    """Invitation acquisition should stop against a China-local day boundary."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.site_responses[("zhihu.fetch", "invitations")] = {
        "pages": [],
        "complete": True,
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    result = await service.zhihu_invitations(
        ZhihuInvitationsRequest(day=date(2026, 8, 12), max_pages=3)
    )

    assert result.complete is True
    namespace, action, args = bridge.site_requests[0]
    assert (namespace, action) == ("zhihu.fetch", "invitations")
    assert args["maxPages"] == 3
    assert args["startTimestamp"] == 1_786_464_000


@pytest.mark.asyncio
async def test_rendered_search_services_build_safe_engine_and_social_urls(tmp_path: Path) -> None:
    """New search services should encode keywords and reuse the guarded Chrome fetch path."""
    bridge = FakeBridge(tmp_path / "extension")
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)
    bridge.payload = BrowserFetchPayload(
        final_url="https://www.google.com/search?q=MCP+browser",
        html='<a href="https://example.com"><h3>Example</h3></a>',
    )

    google = await service.google_search(WebSearchRequest(keyword="MCP browser"))

    assert google.items[0].url == "https://example.com"
    assert str(bridge.fetches[-1].url) == "https://www.google.com/search?q=MCP+browser"

    bridge.payload = BrowserFetchPayload(
        final_url="https://cn.bing.com/search?q=MCP+browser",
        html=(
            '<ol id="b_results"><li class="b_algo">'
            '<h2><a href="https://example.com">Example</a></h2>'
            "</li></ol>"
        ),
    )

    bing = await service.bing_search(WebSearchRequest(keyword="MCP browser"))

    assert bing.items[0].url == "https://example.com"
    assert str(bridge.fetches[-1].url) == "https://cn.bing.com/search?q=MCP+browser"

    bridge.payload = BrowserFetchPayload(
        final_url="https://www.sogou.com/web?query=MCP+browser",
        html=(
            '<div class="rb"><h3><a href="https://example.com">Example</a></h3>'
            '<div id="cacheresult_summary_0">Summary</div></div>'
        ),
    )

    sogou = await service.sogou_search(WebSearchRequest(keyword="MCP browser"))

    assert sogou.items[0].url == "https://example.com"
    assert str(bridge.fetches[-1].url) == "https://www.sogou.com/web?query=MCP+browser"

    bridge.payload = BrowserFetchPayload(
        final_url="https://x.com/search?q=MCP+browser&src=typed_query&f=live",
        html="""
        <article data-testid="tweet">
          <div data-testid="User-Name"><span>Alice</span><span>@alice</span></div>
          <a href="/alice/status/123"><time datetime="2026-08-12T00:00:00Z"></time></a>
          <div data-testid="tweetText">MCP post</div>
        </article>
        """,
    )

    x_result = await service.x_search(
        XSearchRequest.model_validate({"keyword": "MCP browser", "sort": "latest", "limit": 5})
    )

    assert x_result.items[0].post_id == "123"
    assert str(bridge.fetches[-1].url).endswith("q=MCP+browser&src=typed_query&f=live")


@pytest.mark.asyncio
async def test_logged_out_platform_is_blocked_before_target_request(tmp_path: Path) -> None:
    """A failed preflight must not dispatch the requested platform action."""
    bridge = FakeBridge(tmp_path / "extension")
    bridge.logged_in_sites["zhihu"] = False
    bridge.site_responses[("zhihu.fetch", "search")] = {
        "data": [],
        "paging": {"is_end": True},
    }
    browser = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    service = SiteService(browser)

    with pytest.raises(SiteLoginRequiredError, match="尚未登录知乎"):
        await service.zhihu_search(ZhihuSearchRequest(keyword="不应执行"))

    assert bridge.site_requests == []
    assert [str(request.url) for request in bridge.fetches] == ["https://www.zhihu.com/"]
