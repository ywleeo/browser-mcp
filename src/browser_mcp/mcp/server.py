"""MCP server factory and tool registration."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from browser_mcp import __version__
from browser_mcp.application import BrowserService
from browser_mcp.config import AppSettings
from browser_mcp.models import (
    BrowserClickCoordinateSpace,
    BrowserClickRequest,
    BrowserPressKey,
    BrowserPressRequest,
    BrowserReadRequest,
    BrowserReadResult,
    BrowserScrollDirection,
    BrowserScrollRequest,
    BrowserSelectRequest,
    BrowserSnapshotRequest,
    BrowserStatus,
    BrowserTypeRequest,
    BrowserVisualResult,
    ExtractMode,
    SnapshotPageRequest,
)
from browser_mcp.sites import SiteService
from browser_mcp.sites.media import MediaDownloader, ProgressCallback
from browser_mcp.sites.models import (
    BilibiliDownloadRequest,
    BilibiliDownloadResult,
    BilibiliSearchOrder,
    BilibiliSearchRequest,
    BilibiliSearchResult,
    BilibiliVideoRequest,
    BilibiliVideoResult,
    DouyinCommentsRequest,
    DouyinCommentsResult,
    DouyinDownloadRequest,
    DouyinSearchRequest,
    DouyinSearchResult,
    DouyinVideoRequest,
    DouyinVideoResult,
    MediaDownloadResult,
    MediaSelection,
    RedditPostRequest,
    RedditPostResult,
    RedditSearchRequest,
    RedditSearchResult,
    RedditSearchSort,
    SiteDocumentResult,
    SiteEngagementRequest,
    SiteEngagementResult,
    SiteLoginStatus,
    SitePageRequest,
    SitePlatform,
    WebSearchRequest,
    WebSearchResult,
    XhsCommentsRequest,
    XhsCommentsResult,
    XhsDownloadRequest,
    XhsNoteRequest,
    XhsNoteResult,
    XhsSearchRequest,
    XhsSearchResult,
    XhsSort,
    XhsUserNotesRequest,
    XhsUserNotesResult,
    XPostRequest,
    XPostResult,
    XSearchRequest,
    XSearchResult,
    XSearchSort,
    ZhihuContentRequest,
    ZhihuInvitationsRequest,
    ZhihuInvitationsResult,
    ZhihuSearchRequest,
    ZhihuSearchResult,
    ZhihuSearchType,
)


def _visual_tool_result(result: BrowserVisualResult) -> CallToolResult:
    """Expose one browser state as both native MCP image content and structured JSON."""
    state = result.state
    return CallToolResult(
        content=[
            ImageContent(data=result.screenshot_data, mime_type=state.screenshot_mime_type),
            TextContent(text=state.model_dump_json(indent=2)),
        ],
        structured_content=state.model_dump(mode="json"),
    )


def create_server(
    settings: AppSettings | None = None,
    service: BrowserService | None = None,
    site_service: SiteService | None = None,
) -> MCPServer[None]:
    """Create an isolated MCP server with generic and website-specific read tools."""
    resolved_settings = settings or AppSettings.from_env()
    browser_service = service or BrowserService(resolved_settings)
    websites = site_service or SiteService(
        browser_service,
        media_downloader=MediaDownloader(resolved_settings.data_dir / "downloads"),
    )

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        """Tie bridge listener cleanup to the MCP transport lifecycle."""
        await browser_service.start()
        try:
            yield None
        finally:
            await browser_service.close()

    def _progress(ctx: Context) -> ProgressCallback:
        """Build a download-progress reporter bound to one MCP request context.

        `report_progress` is a no-op for callers that did not negotiate a progress
        token, so plain tool calls are unaffected. The reporter is best-effort:
        the downloader never propagates a reporting failure.
        """

        async def report(bytes_done: int, bytes_total: int | None) -> None:
            if bytes_total is not None:
                await ctx.report_progress(
                    float(bytes_done),
                    float(bytes_total),
                    f"downloaded {bytes_done:,} of {bytes_total:,} bytes",
                )
            else:
                await ctx.report_progress(
                    float(bytes_done), None, f"downloaded {bytes_done:,} bytes"
                )

        return report

    server: MCPServer[None] = MCPServer(
        name="browser-mcp",
        title="Browser MCP",
        description="Read and interact with webpages through a real local Chrome session.",
        instructions=(
            "Read public webpages through the authenticated local Chrome extension. "
            "Use browser_read for a new immutable snapshot, browser_read_page for its "
            "remaining pages, and browser_status for connection diagnostics. For visual "
            "interaction, call browser_snapshot first, prefer its current element_id values "
            "over coordinates, and use the screenshot returned after each action to verify "
            "the result. Element references expire whenever a new page state is returned. "
            "Ask for explicit user confirmation immediately before actions that publish, "
            "send, purchase, delete, or otherwise cause consequential external side effects. "
            "Use the "
            "zhihu_*, bilibili_*, xhs_*, douyin_*, x_*, and reddit_* tools for stable "
            "site-specific "
            "structured results. Use google_search, bing_search, or sogou_search "
            "when the user chooses a web search engine. Authenticated platform tools "
            "check login before every task. If a tool reports that login is required, tell the "
            "user to log in with the provided URL and do not retry until they confirm."
        ),
        version=__version__,
        log_level="WARNING",
        lifespan=lifespan,
    )

    async def _browser_status() -> BrowserStatus:
        """Return bridge diagnostics without claiming unavailable capabilities."""
        return await browser_service.status()

    async def _browser_read(
        url: str,
        extract: ExtractMode = ExtractMode.READABILITY,
        wait_ms: int = 800,
        max_chars: int = 10_000,
    ) -> BrowserReadResult:
        """Validate and dispatch one browser-backed webpage read."""
        request = BrowserReadRequest.model_validate(
            {
                "url": url,
                "extract": extract,
                "wait_ms": wait_ms,
                "max_chars": max_chars,
            }
        )
        return await browser_service.read(request)

    async def _browser_read_page(
        snapshot_id: str,
        offset: int = 0,
        max_chars: int = 10_000,
    ) -> BrowserReadResult:
        """Validate and dispatch one immutable snapshot page read."""
        request = SnapshotPageRequest(
            snapshot_id=snapshot_id,
            offset=offset,
            max_chars=max_chars,
        )
        return await browser_service.read_page(request)

    async def _browser_snapshot(url: str | None = None, wait_ms: int = 500) -> CallToolResult:
        """Open or observe the managed tab and return its screenshot and interactive elements."""
        request = BrowserSnapshotRequest.model_validate({"url": url, "wait_ms": wait_ms})
        return _visual_tool_result(await browser_service.visual_snapshot(request))

    async def _browser_click(
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
        coordinate_space: BrowserClickCoordinateSpace = BrowserClickCoordinateSpace.SCREENSHOT,
        wait_ms: int = 500,
    ) -> CallToolResult:
        """Click one current element reference or screenshot/viewport coordinate."""
        request = BrowserClickRequest(
            element_id=element_id,
            x=x,
            y=y,
            coordinate_space=coordinate_space,
            wait_ms=wait_ms,
        )
        return _visual_tool_result(await browser_service.click(request))

    async def _browser_scroll(
        direction: BrowserScrollDirection = BrowserScrollDirection.DOWN,
        amount: int = 600,
        element_id: str | None = None,
        wait_ms: int = 300,
    ) -> CallToolResult:
        """Scroll relatively or bring one current element reference into view."""
        request = BrowserScrollRequest(
            direction=direction,
            amount=amount,
            element_id=element_id,
            wait_ms=wait_ms,
        )
        return _visual_tool_result(await browser_service.scroll(request))

    async def _browser_type(
        element_id: str,
        text: str,
        clear: bool = True,
        submit: bool = False,
        wait_ms: int = 300,
    ) -> CallToolResult:
        """Enter text into one current editable element and optionally press Enter."""
        request = BrowserTypeRequest(
            element_id=element_id,
            text=text,
            clear=clear,
            submit=submit,
            wait_ms=wait_ms,
        )
        return _visual_tool_result(await browser_service.type_text(request))

    async def _browser_press(
        key: BrowserPressKey,
        element_id: str | None = None,
        wait_ms: int = 300,
    ) -> CallToolResult:
        """Press one bounded non-text key, optionally after focusing a current element."""
        request = BrowserPressRequest(key=key, element_id=element_id, wait_ms=wait_ms)
        return _visual_tool_result(await browser_service.press(request))

    async def _browser_select(
        element_id: str,
        value: str,
        wait_ms: int = 300,
    ) -> CallToolResult:
        """Select one native option by exact value or visible label."""
        request = BrowserSelectRequest(element_id=element_id, value=value, wait_ms=wait_ms)
        return _visual_tool_result(await browser_service.select(request))

    async def _site_login_status(platform: SitePlatform) -> SiteLoginStatus:
        """Check one platform login state without executing the requested platform task."""
        return await websites.login_status(platform)

    async def _zhihu_search(
        keyword: str,
        search_type: ZhihuSearchType = ZhihuSearchType.CONTENT,
        offset: int = 0,
    ) -> ZhihuSearchResult:
        """Search Zhihu and return normalized questions, answers, and articles."""
        request = ZhihuSearchRequest(
            keyword=keyword,
            search_type=search_type,
            offset=offset,
        )
        return await websites.zhihu_search(request)

    async def _zhihu_content(url: str, max_chars: int = 10_000) -> SiteDocumentResult:
        """Read one Zhihu question, answer, or article into an immutable snapshot."""
        request = ZhihuContentRequest.model_validate({"url": url, "max_chars": max_chars})
        return await websites.zhihu_content(request)

    async def _zhihu_invitations(
        day: str | None = None,
        max_pages: int = 5,
    ) -> ZhihuInvitationsResult:
        """Read answer invitations for one China-calendar day from the logged-in account."""
        resolved_day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        request = ZhihuInvitationsRequest.model_validate(
            {"day": resolved_day, "max_pages": max_pages}
        )
        return await websites.zhihu_invitations(request)

    async def _bilibili_search(
        keyword: str,
        page: int = 1,
        order: BilibiliSearchOrder = BilibiliSearchOrder.RELEVANCE,
    ) -> BilibiliSearchResult:
        """Search Bilibili videos and return normalized metadata."""
        request = BilibiliSearchRequest(keyword=keyword, page=page, order=order)
        return await websites.bilibili_search(request)

    async def _bilibili_video(url: str) -> BilibiliVideoResult:
        """Read one Bilibili BV/AV video and its selected multipart page metadata."""
        request = BilibiliVideoRequest.model_validate({"url": url})
        return await websites.bilibili_video(request)

    async def _bilibili_download_video(
        ctx: Context,
        url: str,
        output_dir: str | None = None,
        overwrite: bool = False,
        max_file_mb: int = 2_048,
    ) -> BilibiliDownloadResult:
        """Download one Bilibili page's video and companion audio tracks."""
        request = BilibiliDownloadRequest.model_validate(
            {
                "url": url,
                "output_dir": output_dir,
                "overwrite": overwrite,
                "max_file_mb": max_file_mb,
            }
        )
        return await websites.bilibili_download_video(request, _progress(ctx))

    async def _bilibili_download_audio(
        ctx: Context,
        url: str,
        output_dir: str | None = None,
        overwrite: bool = False,
        max_file_mb: int = 2_048,
    ) -> BilibiliDownloadResult:
        """Download only one Bilibili page's best compatible audio track."""
        request = BilibiliDownloadRequest.model_validate(
            {
                "url": url,
                "output_dir": output_dir,
                "overwrite": overwrite,
                "max_file_mb": max_file_mb,
            }
        )
        return await websites.bilibili_download_audio(request, _progress(ctx))

    async def _xhs_search(
        keyword: str,
        page: int = 1,
        sort: XhsSort = XhsSort.GENERAL,
    ) -> XhsSearchResult:
        """Search Xiaohongshu through its signed web UI request."""
        request = XhsSearchRequest(keyword=keyword, page=page, sort=sort)
        return await websites.xhs_search(request)

    async def _xhs_note(url: str) -> XhsNoteResult:
        """Read one Xiaohongshu note URL returned by search or copied from Chrome."""
        request = XhsNoteRequest.model_validate({"url": url})
        return await websites.xhs_note(request)

    async def _xhs_like(url: str, enabled: bool = True) -> SiteEngagementResult:
        """Set one Xiaohongshu note's desired like state and verify the result."""
        request = SiteEngagementRequest.model_validate({"url": url, "enabled": enabled})
        return await websites.xhs_like(request)

    async def _xhs_collect(url: str, enabled: bool = True) -> SiteEngagementResult:
        """Set one Xiaohongshu note's desired collection state and verify the result."""
        request = SiteEngagementRequest.model_validate({"url": url, "enabled": enabled})
        return await websites.xhs_collect(request)

    async def _xhs_download(
        url: str,
        media: MediaSelection = MediaSelection.ALL,
        output_dir: str | None = None,
        overwrite: bool = False,
        max_file_mb: int = 1_024,
    ) -> MediaDownloadResult:
        """Download selected images or video from one Xiaohongshu note."""
        request = XhsDownloadRequest.model_validate(
            {
                "url": url,
                "media": media,
                "output_dir": output_dir,
                "overwrite": overwrite,
                "max_file_mb": max_file_mb,
            }
        )
        return await websites.xhs_download(request)

    async def _xhs_comments(url: str, max_comments: int = 500) -> XhsCommentsResult:
        """Collect top-level comments and replies from one Xiaohongshu note."""
        request = XhsCommentsRequest.model_validate({"url": url, "max_comments": max_comments})
        return await websites.xhs_comments(request)

    async def _xhs_user_notes(
        user_id: str | None = None,
        max_pages: int = 5,
    ) -> XhsUserNotesResult:
        """Read notes published by an account, defaulting to the logged-in account."""
        request = XhsUserNotesRequest(user_id=user_id, max_pages=max_pages)
        return await websites.xhs_user_notes(request)

    async def _douyin_search(keyword: str, limit: int = 20) -> DouyinSearchResult:
        """Search Douyin posts through the page's own signed streaming request."""
        return await websites.douyin_search(DouyinSearchRequest(keyword=keyword, limit=limit))

    async def _douyin_video(url: str) -> DouyinVideoResult:
        """Read one canonical Douyin video or image-post URL."""
        request = DouyinVideoRequest.model_validate({"url": url})
        return await websites.douyin_video(request)

    async def _douyin_like(url: str, enabled: bool = True) -> SiteEngagementResult:
        """Set one Douyin post's desired like state and verify the result."""
        request = SiteEngagementRequest.model_validate({"url": url, "enabled": enabled})
        return await websites.douyin_like(request)

    async def _douyin_collect(url: str, enabled: bool = True) -> SiteEngagementResult:
        """Set one Douyin post's desired collection state and verify the result."""
        request = SiteEngagementRequest.model_validate({"url": url, "enabled": enabled})
        return await websites.douyin_collect(request)

    async def _douyin_download(
        url: str,
        media: MediaSelection = MediaSelection.ALL,
        output_dir: str | None = None,
        overwrite: bool = False,
        max_file_mb: int = 1_024,
    ) -> MediaDownloadResult:
        """Download selected images or video from one Douyin post."""
        request = DouyinDownloadRequest.model_validate(
            {
                "url": url,
                "media": media,
                "output_dir": output_dir,
                "overwrite": overwrite,
                "max_file_mb": max_file_mb,
            }
        )
        return await websites.douyin_download(request)

    async def _douyin_comments(url: str, max_comments: int = 500) -> DouyinCommentsResult:
        """Collect root comments and expanded replies from one Douyin post."""
        request = DouyinCommentsRequest.model_validate({"url": url, "max_comments": max_comments})
        return await websites.douyin_comments(request)

    async def _google_search(keyword: str, limit: int = 10) -> WebSearchResult:
        """Search Google and return normalized organic web results."""
        return await websites.google_search(WebSearchRequest(keyword=keyword, limit=limit))

    async def _bing_search(keyword: str, limit: int = 10) -> WebSearchResult:
        """Search Bing and return normalized organic web results."""
        return await websites.bing_search(WebSearchRequest(keyword=keyword, limit=limit))

    async def _sogou_search(keyword: str, limit: int = 10) -> WebSearchResult:
        """Search Sogou and return normalized web results."""
        return await websites.sogou_search(WebSearchRequest(keyword=keyword, limit=limit))

    async def _x_search(
        keyword: str,
        sort: XSearchSort = XSearchSort.TOP,
        limit: int = 10,
    ) -> XSearchResult:
        """Search X posts through the current Chrome session."""
        return await websites.x_search(XSearchRequest(keyword=keyword, sort=sort, limit=limit))

    async def _x_post(url: str) -> XPostResult:
        """Read one X status URL from the rendered conversation page."""
        return await websites.x_post(XPostRequest.model_validate({"url": url}))

    async def _reddit_search(
        keyword: str,
        sort: RedditSearchSort = RedditSearchSort.RELEVANCE,
        limit: int = 10,
    ) -> RedditSearchResult:
        """Search Reddit posts through the rendered web interface."""
        request = RedditSearchRequest(keyword=keyword, sort=sort, limit=limit)
        return await websites.reddit_search(request)

    async def _reddit_post(url: str, max_comments: int = 20) -> RedditPostResult:
        """Read one Reddit post plus a bounded set of rendered comments."""
        request = RedditPostRequest.model_validate({"url": url, "max_comments": max_comments})
        return await websites.reddit_post(request)

    async def _site_read_page(
        snapshot_id: str,
        offset: int = 0,
        max_chars: int = 10_000,
    ) -> SiteDocumentResult:
        """Read a later page from an immutable website-specific document snapshot."""
        request = SitePageRequest(
            snapshot_id=snapshot_id,
            offset=offset,
            max_chars=max_chars,
        )
        return await websites.read_page(request)

    server.add_tool(
        _browser_status,
        name="browser_status",
        description="Return the local Chrome extension bridge installation and connection status.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    server.add_tool(
        _browser_read,
        name="browser_read",
        description=(
            "Load a public HTTP(S) URL in the user's real Chrome session and extract "
            "readability text, visible text, rendered HTML, or XHR responses."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _browser_read_page,
        name="browser_read_page",
        description="Read the next Unicode-safe page from an existing immutable browser snapshot.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    server.add_tool(
        _browser_snapshot,
        name="browser_snapshot",
        description=(
            "Open a public URL or observe the managed Chrome tab, returning a viewport "
            "screenshot plus fresh element references for visual interaction."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _browser_click,
        name="browser_click",
        description=(
            "Send one trusted Chrome click to a current browser_snapshot element_id. When "
            "no semantic reference is available, x/y default to pixels in the returned "
            "screenshot; set coordinate_space=viewport only for CSS viewport coordinates."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _browser_scroll,
        name="browser_scroll",
        description=(
            "Scroll the managed page in one direction or bring a current element reference "
            "into view, then return the new screenshot and references."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _browser_type,
        name="browser_type",
        description=(
            "Type text into a current editable element reference, optionally replacing its "
            "contents and pressing Enter, then return the new visual state."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _browser_press,
        name="browser_press",
        description=(
            "Press one supported non-text keyboard key, optionally focused on a current "
            "element reference, then return the new visual state."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _browser_select,
        name="browser_select",
        description=(
            "Choose a native select option by its exact value or visible label, then return "
            "the new visual state."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    server.add_tool(
        _site_login_status,
        name="site_login_status",
        description=(
            "Check whether the current Chrome Profile is logged in to Zhihu, "
            "Xiaohongshu, Douyin, X, or Reddit without executing a platform task."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _zhihu_search,
        name="zhihu_search",
        description="Search Zhihu using the current Chrome session and return normalized results.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _zhihu_content,
        name="zhihu_content",
        description="Read a Zhihu question, answer, or article as a pageable normalized document.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _zhihu_invitations,
        name="zhihu_invitations",
        description=(
            "Read the logged-in Zhihu account's answer invitations for one Asia/Shanghai date."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _bilibili_search,
        name="bilibili_search",
        description=(
            "Search Bilibili videos through the current Chrome session and return titles, "
            "authors, statistics, tags, durations, and canonical BV links."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _bilibili_video,
        name="bilibili_video",
        description=(
            "Read one Bilibili BV/AV video with content metadata, author, statistics, tags, "
            "and multipart page information. Use ?p=N to select one part."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _bilibili_download_video,
        name="bilibili_download_video",
        description=(
            "Download one Bilibili BV/AV page's best compatible video and audio. When FFmpeg "
            "is available the DASH tracks are losslessly muxed into MP4; otherwise both track "
            "files are returned separately."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _bilibili_download_audio,
        name="bilibili_download_audio",
        description=(
            "Download only the highest-bandwidth compatible audio track from one Bilibili "
            "BV/AV page. Use ?p=N to select one multipart page."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_search,
        name="xhs_search",
        description=(
            "Search Xiaohongshu through its signed web request in the current Chrome session."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_note,
        name="xhs_note",
        description="Read one Xiaohongshu explore-note URL with its xsec access parameters.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_like,
        name="xhs_like",
        description=(
            "Set the desired like state for one Xiaohongshu note in the logged-in account. "
            "This changes external account state, so obtain explicit user confirmation "
            "immediately before calling. Repeated calls with the same enabled value are no-ops."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_collect,
        name="xhs_collect",
        description=(
            "Set the desired collection state for one Xiaohongshu note in the logged-in account. "
            "This changes external account state, so obtain explicit user confirmation "
            "immediately before calling. Repeated calls with the same enabled value are no-ops."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_download,
        name="xhs_download",
        description=(
            "Download images, video, or all media from one Xiaohongshu note to a local "
            "absolute directory; defaults to Browser MCP's data downloads directory."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_comments,
        name="xhs_comments",
        description=(
            "Collect Xiaohongshu comments and replies by scrolling the note's own comment "
            "stream; returns completeness and limit metadata."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _xhs_user_notes,
        name="xhs_user_notes",
        description=(
            "List notes published by a Xiaohongshu account through the current Chrome "
            "session; omit user_id to use the logged-in account."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_search,
        name="douyin_search",
        description=(
            "Search Douyin through its signed web stream and return normalized video or "
            "image-post metadata."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_video,
        name="douyin_video",
        description=(
            "Read one Douyin video or image post with author, statistics, media, and music."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_like,
        name="douyin_like",
        description=(
            "Set the desired like state for one Douyin post in the logged-in account. "
            "This changes external account state, so obtain explicit user confirmation "
            "immediately before calling. Repeated calls with the same enabled value are no-ops."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_collect,
        name="douyin_collect",
        description=(
            "Set the desired collection state for one Douyin post in the logged-in account. "
            "This changes external account state, so obtain explicit user confirmation "
            "immediately before calling. Repeated calls with the same enabled value are no-ops."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_download,
        name="douyin_download",
        description=(
            "Download images, video, or all media from one Douyin post to a local absolute "
            "directory; defaults to Browser MCP's data downloads directory."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _douyin_comments,
        name="douyin_comments",
        description=(
            "Collect Douyin comments and replies from the post's rendered comment stream; "
            "returns completeness and limit metadata."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _x_search,
        name="x_search",
        description=(
            "Search X (formerly Twitter) posts through the user's current Chrome session."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _x_post,
        name="x_post",
        description="Read one X post with author, text, metrics, links, and media URLs.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _reddit_search,
        name="reddit_search",
        description="Search Reddit posts and return normalized post metadata and links.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _reddit_post,
        name="reddit_post",
        description="Read one Reddit post and a bounded set of comments rendered on its page.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _google_search,
        name="google_search",
        description="Search the public web with Google through the user's real Chrome session.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _bing_search,
        name="bing_search",
        description="Search the public web with Bing through the user's real Chrome session.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _sogou_search,
        name="sogou_search",
        description="Search the public web with Sogou through the user's real Chrome session.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    server.add_tool(
        _site_read_page,
        name="site_read_page",
        description="Read the next page of an existing immutable site-specific document snapshot.",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )

    return server
