"""Typed request and result contracts for website-specific MCP tools."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class ZhihuSearchType(StrEnum):
    """Supported Zhihu search result filters."""

    CONTENT = "content"
    ANSWER = "answer"
    ARTICLE = "article"
    QUESTION = "question"


class ZhihuSearchRequest(BaseModel):
    """Validated Zhihu keyword search input."""

    keyword: str = Field(min_length=1, max_length=200)
    search_type: ZhihuSearchType = ZhihuSearchType.CONTENT
    offset: int = Field(default=0, ge=0, le=10_000)


class ZhihuSearchItem(BaseModel):
    """One normalized Zhihu search result."""

    index: int
    kind: str
    title: str
    author: str
    voteup: int
    comments: int
    excerpt: str
    url: str


class ZhihuSearchResult(BaseModel):
    """Normalized Zhihu search page."""

    keyword: str
    search_type: ZhihuSearchType
    offset: int
    has_more: bool
    items: tuple[ZhihuSearchItem, ...]


class ZhihuContentRequest(BaseModel):
    """Validated Zhihu question, answer, or article input."""

    url: HttpUrl
    max_chars: int = Field(default=10_000, ge=1, le=100_000)


class ZhihuInvitationsRequest(BaseModel):
    """Validated request for one China-calendar day of answer invitations."""

    day: date
    max_pages: int = Field(default=5, ge=1, le=10)


class ZhihuInvitationItem(BaseModel):
    """One normalized invitation to answer a Zhihu question."""

    invited_at: str
    verb: str
    inviters: tuple[str, ...]
    source: str
    question: str
    url: str
    merge_count: int


class ZhihuInvitationsResult(BaseModel):
    """Answer invitations for one day with an explicit completeness signal."""

    day: date
    complete: bool
    pages_fetched: int
    items: tuple[ZhihuInvitationItem, ...]


class SitePageRequest(BaseModel):
    """Continuation request for any immutable site document snapshot."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(ge=0)
    max_chars: int = Field(default=10_000, ge=1, le=100_000)


class SiteDocumentResult(BaseModel):
    """One Unicode-safe page from a normalized website document."""

    snapshot_id: str
    platform: str
    kind: str
    url: str
    title: str
    total_chars: int
    range_start: int
    range_end: int
    complete: bool
    next_offset: int | None
    content: str


class SitePlatform(StrEnum):
    """Platforms whose MCP tools require an authenticated Chrome session."""

    ZHIHU = "zhihu"
    XHS = "xhs"
    DOUYIN = "douyin"
    X = "x"
    REDDIT = "reddit"


class SiteLoginState(StrEnum):
    """Possible outcomes of a rendered-page platform login check."""

    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


class SiteLoginStatus(BaseModel):
    """One platform's login state without exposing browser credentials."""

    platform: SitePlatform
    state: SiteLoginState
    logged_in: bool
    login_url: str
    account_label: str
    detail: str


class SiteEngagementAction(StrEnum):
    """Post-level account interactions supported by authenticated site adapters."""

    LIKE = "like"
    COLLECT = "collect"


class SiteEngagementRequest(BaseModel):
    """Validated desired state for one post-level like or collection action."""

    url: HttpUrl
    enabled: bool = True


class SiteEngagementResult(BaseModel):
    """Verified final state of one idempotent post-level account interaction."""

    platform: SitePlatform
    post_id: str
    action: SiteEngagementAction
    requested_state: bool
    active: bool
    changed: bool
    url: str


class SearchEngine(StrEnum):
    """Supported public web search engines."""

    GOOGLE = "google"
    BING = "bing"
    SOGOU = "sogou"


class WebSearchRequest(BaseModel):
    """Validated keyword search shared by isolated search-engine adapters."""

    keyword: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)


class WebSearchItem(BaseModel):
    """One normalized organic web search result."""

    index: int
    title: str
    url: str
    display_url: str
    snippet: str


class WebSearchResult(BaseModel):
    """Normalized first-page results from one web search engine."""

    engine: SearchEngine
    keyword: str
    items: tuple[WebSearchItem, ...]


class XSearchSort(StrEnum):
    """Search timelines exposed by the X web interface."""

    TOP = "top"
    LATEST = "latest"


class XSearchRequest(BaseModel):
    """Validated X keyword search input."""

    keyword: str = Field(min_length=1, max_length=500)
    sort: XSearchSort = XSearchSort.TOP
    limit: int = Field(default=10, ge=1, le=20)


class XPostRequest(BaseModel):
    """Validated X post URL input."""

    url: HttpUrl


class XPostItem(BaseModel):
    """One normalized X post from a search timeline or status page."""

    index: int
    post_id: str
    url: str
    author: str
    handle: str
    text: str
    published_at: str
    replies: str
    reposts: str
    likes: str
    views: str
    media_urls: tuple[str, ...] = ()
    links: tuple[str, ...] = ()


class XSearchResult(BaseModel):
    """Normalized posts from an X search timeline."""

    keyword: str
    sort: XSearchSort
    items: tuple[XPostItem, ...]


class XPostResult(BaseModel):
    """Normalized detail for one X status URL."""

    post_id: str
    url: str
    author: str
    handle: str
    text: str
    published_at: str
    replies: str
    reposts: str
    likes: str
    views: str
    media_urls: tuple[str, ...]
    links: tuple[str, ...]


class RedditSearchSort(StrEnum):
    """Post order values supported by Reddit search."""

    RELEVANCE = "relevance"
    HOT = "hot"
    TOP = "top"
    NEW = "new"
    COMMENTS = "comments"


class RedditSearchRequest(BaseModel):
    """Validated Reddit post-search input."""

    keyword: str = Field(min_length=1, max_length=500)
    sort: RedditSearchSort = RedditSearchSort.RELEVANCE
    limit: int = Field(default=10, ge=1, le=20)


class RedditPostRequest(BaseModel):
    """Validated Reddit post URL and comment budget."""

    url: HttpUrl
    max_comments: int = Field(default=20, ge=0, le=100)


class RedditSearchItem(BaseModel):
    """One normalized Reddit post search result."""

    index: int
    post_id: str
    url: str
    title: str
    subreddit: str
    author: str
    published_at: str
    score: int
    comments: int


class RedditSearchResult(BaseModel):
    """Normalized posts returned by Reddit search."""

    keyword: str
    sort: RedditSearchSort
    items: tuple[RedditSearchItem, ...]


class RedditComment(BaseModel):
    """One rendered Reddit comment included on a post page."""

    comment_id: str
    url: str
    author: str
    published_at: str
    score: int
    depth: int
    text: str


class RedditPostResult(BaseModel):
    """Normalized Reddit post metadata, body, media, and rendered comments."""

    post_id: str
    url: str
    title: str
    subreddit: str
    author: str
    published_at: str
    score: int
    comment_count: int
    post_type: str
    body: str
    media_url: str | None
    comments: tuple[RedditComment, ...]


class XhsSort(StrEnum):
    """Search order values accepted by the Xiaohongshu web client."""

    GENERAL = "general"
    LATEST = "time_descending"
    POPULAR = "popularity_descending"


class XhsSearchRequest(BaseModel):
    """Validated Xiaohongshu signed search input."""

    keyword: str = Field(min_length=1, max_length=200)
    page: int = Field(default=1, ge=1, le=100)
    sort: XhsSort = XhsSort.GENERAL


class XhsSearchItem(BaseModel):
    """One normalized Xiaohongshu note card."""

    index: int
    note_id: str
    xsec_token: str
    url: str
    title: str
    author: str
    cover: str
    note_type: str
    likes: str
    collects: str
    comments: str


class XhsSearchResult(BaseModel):
    """Normalized Xiaohongshu search page."""

    keyword: str
    page: int
    sort: XhsSort
    has_more: bool
    items: tuple[XhsSearchItem, ...]


class XhsUserNotesRequest(BaseModel):
    """Validated request for one Xiaohongshu account's published notes."""

    user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    max_pages: int = Field(default=5, ge=1, le=10)


class XhsUserNoteItem(BaseModel):
    """One normalized note published by a Xiaohongshu account."""

    index: int
    note_id: str
    xsec_token: str
    url: str
    title: str
    author: str
    cover: str
    note_type: str
    published_at: str
    published_at_ms: int | None
    likes: str
    is_sticky: bool


class XhsUserNotesResult(BaseModel):
    """Published-note list with explicit pagination completeness."""

    user_id: str
    nickname: str
    red_id: str
    complete: bool
    pages_fetched: int
    has_more: bool
    cursor: str
    items: tuple[XhsUserNoteItem, ...]


class XhsNoteRequest(BaseModel):
    """Validated Xiaohongshu note URL returned by search or copied from Chrome."""

    url: HttpUrl


class MediaSelection(StrEnum):
    """Media kinds accepted by platform download tools."""

    ALL = "all"
    IMAGES = "images"
    VIDEO = "video"


class XhsDownloadRequest(BaseModel):
    """Validated request to download media from one Xiaohongshu note."""

    url: HttpUrl
    media: MediaSelection = MediaSelection.ALL
    output_dir: str | None = Field(default=None, max_length=4_096)
    overwrite: bool = False
    max_file_mb: int = Field(default=1_024, ge=1, le=4_096)


class MediaDownloadItem(BaseModel):
    """One successfully downloaded local media file."""

    index: int
    media_type: str
    source_url: str
    final_url: str
    path: str
    bytes: int
    content_type: str
    sha256: str


class MediaDownloadResult(BaseModel):
    """Downloaded media files and their exact local destination."""

    platform: str
    post_id: str
    output_dir: str
    downloaded: int
    total_bytes: int
    items: tuple[MediaDownloadItem, ...]


class XhsImage(BaseModel):
    """One normalized Xiaohongshu image URL."""

    url: str


class XhsNoteResult(BaseModel):
    """Normalized Xiaohongshu note detail from SSR initial state."""

    note_id: str
    note_type: str
    url: str
    title: str
    description: str
    author: str
    published_at: str
    published_at_ms: int | None
    likes: str
    collects: str
    comments: str
    images: tuple[XhsImage, ...]
    video_url: str | None


class XhsCommentsRequest(BaseModel):
    """Validated request for comments loaded by one Xiaohongshu note page."""

    url: HttpUrl
    max_comments: int = Field(default=500, ge=1, le=5_000)


class XhsComment(BaseModel):
    """One normalized Xiaohongshu top-level comment or reply."""

    index: int
    comment_id: str
    root_comment_id: str
    parent_comment_id: str | None
    depth: int
    user_id: str
    author: str
    text: str
    published_at: str
    published_at_ms: int | None
    ip_location: str
    likes: str
    reply_count: int
    reply_to: str


class XhsCommentsResult(BaseModel):
    """Bounded Xiaohongshu comment collection with explicit completeness metadata."""

    note_id: str
    url: str
    total: int | None
    fetched: int
    complete: bool
    limit_reached: bool
    pages_fetched: int
    scrolls: int
    items: tuple[XhsComment, ...]


class DouyinSearchRequest(BaseModel):
    """Validated request for one Douyin keyword-search result page."""

    keyword: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=20)


class DouyinSearchItem(BaseModel):
    """One normalized Douyin video or image post returned by search."""

    index: int
    aweme_id: str
    aweme_type: str
    url: str
    description: str
    author: str
    author_id: str
    sec_uid: str
    published_at: str
    published_at_ms: int | None
    duration_ms: int | None
    likes: int
    comments: int
    collects: int
    shares: int
    cover_url: str


class DouyinSearchResult(BaseModel):
    """Normalized first batch from Douyin's signed streaming search response."""

    keyword: str
    has_more: bool
    cursor: int | None
    items: tuple[DouyinSearchItem, ...]


class DouyinVideoRequest(BaseModel):
    """Validated canonical Douyin video or image-post URL."""

    url: HttpUrl


class DouyinDownloadRequest(BaseModel):
    """Validated request to download media from one Douyin post."""

    url: HttpUrl
    media: MediaSelection = MediaSelection.ALL
    output_dir: str | None = Field(default=None, max_length=4_096)
    overwrite: bool = False
    max_file_mb: int = Field(default=1_024, ge=1, le=4_096)


class DouyinVideoResult(BaseModel):
    """Normalized metadata and media addresses for one Douyin post."""

    aweme_id: str
    aweme_type: str
    url: str
    description: str
    author: str
    author_id: str
    sec_uid: str
    published_at: str
    published_at_ms: int | None
    duration_ms: int | None
    likes: int
    comments: int
    collects: int
    shares: int
    cover_url: str
    media_urls: tuple[str, ...]
    music_title: str
    music_author: str


class DouyinCommentsRequest(BaseModel):
    """Validated request for comments loaded by one Douyin post page."""

    url: HttpUrl
    max_comments: int = Field(default=500, ge=1, le=5_000)


class DouyinComment(BaseModel):
    """One normalized Douyin top-level comment or reply."""

    index: int
    comment_id: str
    root_comment_id: str
    parent_comment_id: str | None
    depth: int
    user_id: str
    author: str
    text: str
    published_at: str
    published_at_ms: int | None
    ip_location: str
    likes: int
    reply_count: int
    reply_to: str


class DouyinCommentsResult(BaseModel):
    """Bounded Douyin comment collection with explicit completeness metadata."""

    aweme_id: str
    url: str
    total: int | None
    fetched: int
    complete: bool
    limit_reached: bool
    pages_fetched: int
    scrolls: int
    items: tuple[DouyinComment, ...]
