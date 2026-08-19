"""Application orchestration for isolated website-specific read adapters."""

from __future__ import annotations

from datetime import datetime, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from browser_mcp.application import BrowserService
from browser_mcp.config import DEFAULT_DATA_DIR
from browser_mcp.models import BrowserFetchPayload, BrowserReadRequest, ExtractMode
from browser_mcp.sites.auth import (
    login_probe_url,
    parse_site_login_status,
    require_site_login,
)
from browser_mcp.sites.douyin import (
    parse_douyin_aweme_url,
    shape_douyin_comments,
    shape_douyin_search,
    shape_douyin_video,
)
from browser_mcp.sites.media import MediaDownloader, MediaSource
from browser_mcp.sites.models import (
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
    SiteDocumentResult,
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
    XhsUserNotesRequest,
    XhsUserNotesResult,
    XPostRequest,
    XPostResult,
    XSearchRequest,
    XSearchResult,
    ZhihuContentRequest,
    ZhihuInvitationsRequest,
    ZhihuInvitationsResult,
    ZhihuSearchRequest,
    ZhihuSearchResult,
)
from browser_mcp.sites.reddit import (
    parse_reddit_post,
    parse_reddit_post_url,
    parse_reddit_search,
)
from browser_mcp.sites.search_engines import (
    parse_bing_search,
    parse_google_search,
    parse_sogou_search,
)
from browser_mcp.sites.snapshot import SiteSnapshotStore
from browser_mcp.sites.x import parse_x_post, parse_x_post_url, parse_x_search
from browser_mcp.sites.xhs import (
    parse_xhs_note_url,
    shape_xhs_comments,
    shape_xhs_note,
    shape_xhs_search,
    shape_xhs_user_notes,
)
from browser_mcp.sites.zhihu import (
    classify_zhihu_url,
    map_zhihu_search_type,
    parse_zhihu_content,
    parse_zhihu_invitations,
    parse_zhihu_search,
)


class SiteService:
    """Coordinate Chrome transport, pure parsers, and immutable site pagination."""

    def __init__(
        self,
        browser: BrowserService,
        snapshots: SiteSnapshotStore | None = None,
        media_downloader: MediaDownloader | None = None,
    ) -> None:
        """Bind site use cases to the existing authenticated browser application port."""
        self._browser = browser
        self._snapshots = snapshots or SiteSnapshotStore()
        self._media_downloader = media_downloader or MediaDownloader(
            DEFAULT_DATA_DIR / "downloads"
        )

    async def zhihu_search(self, request: ZhihuSearchRequest) -> ZhihuSearchResult:
        """Search Zhihu through its authenticated web API and normalize the result."""
        await self._require_login(SitePlatform.ZHIHU)
        raw = await self._browser.gateway.request(
            "zhihu.fetch",
            "search",
            {
                "keyword": request.keyword.strip(),
                "type": map_zhihu_search_type(request.search_type),
                "offset": request.offset,
            },
        )
        return parse_zhihu_search(raw, request)

    async def zhihu_content(self, request: ZhihuContentRequest) -> SiteDocumentResult:
        """Read and snapshot a Zhihu question, answer, or article SSR document."""
        url = str(request.url)
        page = classify_zhihu_url(url)
        await self._require_login(SitePlatform.ZHIHU)
        payload = await self._browser.fetch_payload(
            BrowserReadRequest(
                url=request.url,
                extract=ExtractMode.RAW,
                wait_ms=500,
                max_chars=request.max_chars,
            )
        )
        final_page = classify_zhihu_url(payload.final_url)
        if final_page != page:
            raise ValueError(
                "Zhihu redirected to a different content identity; refusing to parse mutable target"
            )
        document = parse_zhihu_content(payload.html, final_page)
        return await self._snapshots.create(
            platform="zhihu",
            kind=document.kind,
            url=payload.final_url,
            title=document.title,
            content=document.content,
            max_chars=request.max_chars,
        )

    async def zhihu_invitations(self, request: ZhihuInvitationsRequest) -> ZhihuInvitationsResult:
        """Read answer invitations for one day through the authenticated Zhihu session."""
        await self._require_login(SitePlatform.ZHIHU)
        day_start = datetime.combine(
            request.day,
            time.min,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        raw = await self._browser.gateway.request(
            "zhihu.fetch",
            "invitations",
            {
                "startTimestamp": int(day_start.timestamp()),
                "maxPages": request.max_pages,
            },
            timeout_seconds=45.0,
        )
        return parse_zhihu_invitations(raw, request.day)

    async def xhs_search(self, request: XhsSearchRequest) -> XhsSearchResult:
        """Navigate the signed Xiaohongshu search UI and normalize its response."""
        await self._require_login(SitePlatform.XHS)
        raw = await self._browser.gateway.request(
            "xhs.fetch",
            "search",
            {
                "keyword": request.keyword.strip(),
                "page": request.page,
                "sort": request.sort.value,
            },
            timeout_seconds=40.0,
        )
        return shape_xhs_search(raw, request)

    async def xhs_note(self, request: XhsNoteRequest) -> XhsNoteResult:
        """Fetch one Xiaohongshu note with its signed URL parameters and normalize it."""
        identity = parse_xhs_note_url(str(request.url))
        await self._require_login(SitePlatform.XHS)
        raw = await self._browser.gateway.request(
            "xhs.fetch",
            "note",
            {
                "noteId": identity.note_id,
                "xsecToken": identity.xsec_token,
                "xsecSource": identity.xsec_source,
            },
            timeout_seconds=40.0,
        )
        return shape_xhs_note(raw, identity)

    async def xhs_download(self, request: XhsDownloadRequest) -> MediaDownloadResult:
        """Resolve one XHS note through Chrome and download its selected media files."""
        note = await self.xhs_note(XhsNoteRequest(url=request.url))
        sources: list[MediaSource] = []
        if request.media in {MediaSelection.ALL, MediaSelection.IMAGES}:
            sources.extend(MediaSource(kind="image", url=image.url) for image in note.images)
        if request.media in {MediaSelection.ALL, MediaSelection.VIDEO} and note.video_url:
            sources.append(MediaSource(kind="video", url=note.video_url))
        if not sources:
            raise ValueError(f"XHS note contains no selected {request.media.value} media")
        return await self._media_downloader.download(
            platform="xhs",
            post_id=note.note_id,
            page_url=note.url,
            sources=tuple(sources),
            output_dir=request.output_dir,
            overwrite=request.overwrite,
            max_file_bytes=request.max_file_mb * 1_048_576,
        )

    async def xhs_comments(self, request: XhsCommentsRequest) -> XhsCommentsResult:
        """Collect comments by scrolling the note's own stream and expanding replies."""
        identity = parse_xhs_note_url(str(request.url))
        await self._require_login(SitePlatform.XHS)
        raw = await self._browser.gateway.request(
            "xhs.fetch",
            "comments",
            {
                "noteId": identity.note_id,
                "xsecToken": identity.xsec_token,
                "xsecSource": identity.xsec_source,
                "maxComments": request.max_comments,
            },
            timeout_seconds=180.0,
        )
        return shape_xhs_comments(raw, identity, request)

    async def xhs_user_notes(self, request: XhsUserNotesRequest) -> XhsUserNotesResult:
        """Read published notes for an account, defaulting to the logged-in account."""
        await self._require_login(SitePlatform.XHS)
        raw = await self._browser.gateway.request(
            "xhs.fetch",
            "user_notes",
            {
                "userId": request.user_id or "",
                "maxPages": request.max_pages,
            },
            timeout_seconds=65.0,
        )
        return shape_xhs_user_notes(raw, request)

    async def douyin_search(self, request: DouyinSearchRequest) -> DouyinSearchResult:
        """Navigate Douyin search and normalize its page-signed streaming response."""
        await self._require_login(SitePlatform.DOUYIN)
        raw = await self._browser.gateway.request(
            "douyin.fetch",
            "search",
            {
                "keyword": request.keyword.strip(),
                "limit": request.limit,
            },
            timeout_seconds=45.0,
        )
        return shape_douyin_search(raw, request)

    async def douyin_video(self, request: DouyinVideoRequest) -> DouyinVideoResult:
        """Read one canonical Douyin video or image post through its signed detail request."""
        identity = parse_douyin_aweme_url(str(request.url))
        await self._require_login(SitePlatform.DOUYIN)
        raw = await self._browser.gateway.request(
            "douyin.fetch",
            "video",
            {
                "awemeId": identity.aweme_id,
                "pageKind": identity.page_kind,
            },
            timeout_seconds=45.0,
        )
        return shape_douyin_video(raw, identity)

    async def douyin_download(self, request: DouyinDownloadRequest) -> MediaDownloadResult:
        """Resolve one Douyin post through Chrome and download its selected media files."""
        post = await self.douyin_video(DouyinVideoRequest(url=request.url))
        sources: list[MediaSource] = []
        if post.aweme_type == "note" and request.media in {
            MediaSelection.ALL,
            MediaSelection.IMAGES,
        }:
            sources.extend(MediaSource(kind="image", url=url) for url in post.media_urls)
        if post.aweme_type == "video" and request.media in {
            MediaSelection.ALL,
            MediaSelection.VIDEO,
        }:
            sources.extend(MediaSource(kind="video", url=url) for url in post.media_urls[:1])
        if not sources:
            raise ValueError(f"Douyin post contains no selected {request.media.value} media")
        return await self._media_downloader.download(
            platform="douyin",
            post_id=post.aweme_id,
            page_url=post.url,
            sources=tuple(sources),
            output_dir=request.output_dir,
            overwrite=request.overwrite,
            max_file_bytes=request.max_file_mb * 1_048_576,
        )

    async def douyin_comments(self, request: DouyinCommentsRequest) -> DouyinCommentsResult:
        """Collect Douyin root comments and expanded replies from the post comment stream."""
        identity = parse_douyin_aweme_url(str(request.url))
        await self._require_login(SitePlatform.DOUYIN)
        raw = await self._browser.gateway.request(
            "douyin.fetch",
            "comments",
            {
                "awemeId": identity.aweme_id,
                "pageKind": identity.page_kind,
                "maxComments": request.max_comments,
            },
            timeout_seconds=180.0,
        )
        return shape_douyin_comments(raw, identity, request)

    async def google_search(self, request: WebSearchRequest) -> WebSearchResult:
        """Search Google through rendered Chrome results."""
        payload = await self._search_payload(
            f"https://www.google.com/search?{urlencode({'q': request.keyword.strip()})}"
        )
        return parse_google_search(payload.html, request)

    async def bing_search(self, request: WebSearchRequest) -> WebSearchResult:
        """Search Bing through rendered Chrome results."""
        payload = await self._search_payload(
            f"https://cn.bing.com/search?{urlencode({'q': request.keyword.strip()})}"
        )
        return parse_bing_search(payload.html, request)

    async def sogou_search(self, request: WebSearchRequest) -> WebSearchResult:
        """Search Sogou through rendered Chrome results."""
        payload = await self._search_payload(
            f"https://www.sogou.com/web?{urlencode({'query': request.keyword.strip()})}"
        )
        return parse_sogou_search(payload.html, request)

    async def x_search(self, request: XSearchRequest) -> XSearchResult:
        """Search the selected X timeline through the current Chrome session."""
        await self._require_login(SitePlatform.X)
        filter_name = "live" if request.sort.value == "latest" else "top"
        query = urlencode(
            {
                "q": request.keyword.strip(),
                "src": "typed_query",
                "f": filter_name,
            }
        )
        payload = await self._search_payload(f"https://x.com/search?{query}")
        return parse_x_search(payload.html, request)

    async def x_post(self, request: XPostRequest) -> XPostResult:
        """Read one exact X status page and normalize its requested post."""
        identity = parse_x_post_url(str(request.url))
        await self._require_login(SitePlatform.X)
        payload = await self._search_payload(str(request.url))
        final_identity = parse_x_post_url(payload.final_url)
        if final_identity.post_id != identity.post_id:
            raise ValueError("X redirected to a different post identity")
        return parse_x_post(payload.html, identity)

    async def reddit_search(self, request: RedditSearchRequest) -> RedditSearchResult:
        """Search Reddit posts through the rendered web interface."""
        await self._require_login(SitePlatform.REDDIT)
        query = urlencode(
            {
                "q": request.keyword.strip(),
                "sort": request.sort.value,
                "type": "posts",
            }
        )
        payload = await self._search_payload(f"https://www.reddit.com/search/?{query}")
        return parse_reddit_search(payload.html, request)

    async def reddit_post(self, request: RedditPostRequest) -> RedditPostResult:
        """Read one exact Reddit post and a bounded set of rendered comments."""
        post_id = parse_reddit_post_url(str(request.url))
        await self._require_login(SitePlatform.REDDIT)
        payload = await self._search_payload(str(request.url))
        if parse_reddit_post_url(payload.final_url) != post_id:
            raise ValueError("Reddit redirected to a different post identity")
        return parse_reddit_post(payload.html, post_id, request.max_comments)

    async def login_status(self, platform: SitePlatform) -> SiteLoginStatus:
        """Inspect one platform session through a harmless rendered home page."""
        url = login_probe_url(platform)
        payload = await self._browser.fetch_payload(
            BrowserReadRequest.model_validate(
                {
                    "url": url,
                    "extract": ExtractMode.RAW,
                    "wait_ms": 1_200,
                    "max_chars": 100_000,
                }
            )
        )
        return parse_site_login_status(platform, payload.html, payload.final_url)

    async def _require_login(self, platform: SitePlatform) -> None:
        """Stop a platform task before its target request unless login is confirmed."""
        require_site_login(await self.login_status(platform))

    async def _search_payload(self, url: str) -> BrowserFetchPayload:
        """Load a rendered search or social page through the shared safe browser gateway."""
        return await self._browser.fetch_payload(
            BrowserReadRequest.model_validate(
                {
                    "url": url,
                    "extract": ExtractMode.RAW,
                    "wait_ms": 4_000,
                    "max_chars": 100_000,
                }
            )
        )

    async def read_page(self, request: SitePageRequest) -> SiteDocumentResult:
        """Return a later page from an immutable normalized site document."""
        return await self._snapshots.read(
            request.snapshot_id,
            request.offset,
            request.max_chars,
        )
