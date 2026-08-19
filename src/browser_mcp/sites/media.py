"""Safe bounded media downloads for authenticated site adapters."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from browser_mcp.security import PublicUrlPolicy
from browser_mcp.sites.models import MediaDownloadItem, MediaDownloadResult

MAX_REDIRECTS: Final = 5
DEFAULT_MAX_FILE_BYTES: Final = 1_073_741_824
PLATFORM_HOST_SUFFIXES: Final = {
    "xhs": (".xhscdn.com", ".xiaohongshu.com"),
    "douyin": (
        ".douyin.com",
        ".douyinpic.com",
        ".douyinvod.com",
        ".byteimg.com",
        ".bytegoofy.com",
    ),
}
CONTENT_EXTENSIONS: Final = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


class MediaDownloadError(RuntimeError):
    """Raised when a media response is unsafe, invalid, or exceeds its budget."""


@dataclass(frozen=True, slots=True)
class MediaSource:
    """One page-derived media URL and its normalized kind."""

    kind: Literal["image", "video"]
    url: str


class MediaDownloader:
    """Stream page-derived media to collision-safe local files."""

    def __init__(
        self,
        default_directory: Path,
        *,
        url_policy: PublicUrlPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a downloader with injectable network boundaries for tests."""
        if not default_directory.is_absolute():
            raise ValueError("default media download directory must be absolute")
        self._default_directory = default_directory
        self._url_policy = url_policy or PublicUrlPolicy()
        self._transport = transport

    async def download(
        self,
        *,
        platform: Literal["xhs", "douyin"],
        post_id: str,
        page_url: str,
        sources: tuple[MediaSource, ...],
        output_dir: str | None,
        overwrite: bool,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> MediaDownloadResult:
        """Download every selected source and return exact file metadata."""
        directory = self._directory(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        items: list[MediaDownloadItem] = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=False,
            transport=self._transport,
            headers={
                "Referer": page_url,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
                ),
            },
        ) as client:
            for index, source in enumerate(sources, start=1):
                item = await self._download_one(
                    client,
                    platform=platform,
                    post_id=post_id,
                    source=source,
                    index=index,
                    directory=directory,
                    overwrite=overwrite,
                    max_file_bytes=max_file_bytes,
                )
                items.append(item)
        return MediaDownloadResult(
            platform=platform,
            post_id=post_id,
            output_dir=str(directory),
            downloaded=len(items),
            total_bytes=sum(item.bytes for item in items),
            items=tuple(items),
        )

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        *,
        platform: Literal["xhs", "douyin"],
        post_id: str,
        source: MediaSource,
        index: int,
        directory: Path,
        overwrite: bool,
        max_file_bytes: int,
    ) -> MediaDownloadItem:
        """Follow validated redirects and atomically publish one bounded file."""
        response, final_url = await self._open_response(client, platform, source.url)
        try:
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            self._validate_content_type(source.kind, content_type)
            declared_size = _content_length(response)
            if declared_size is not None and declared_size > max_file_bytes:
                raise MediaDownloadError(
                    f"media file exceeds {max_file_bytes} byte limit: {declared_size}"
                )
            extension = _media_extension(source.kind, content_type, final_url)
            stem = f"{platform}_{post_id}_{source.kind}_{index:02d}"
            target = _target_path(directory, stem, extension, overwrite)
            temporary = target.with_name(f".{target.name}.part")
            digest = hashlib.sha256()
            size = 0
            try:
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_file_bytes:
                            raise MediaDownloadError(
                                f"media file exceeded {max_file_bytes} byte limit while streaming"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            await response.aclose()
        return MediaDownloadItem(
            index=index,
            media_type=source.kind,
            source_url=source.url,
            final_url=final_url,
            path=str(target),
            bytes=size,
            content_type=content_type,
            sha256=digest.hexdigest(),
        )

    async def _open_response(
        self,
        client: httpx.AsyncClient,
        platform: Literal["xhs", "douyin"],
        source_url: str,
    ) -> tuple[httpx.Response, str]:
        """Open one response while validating every redirect target."""
        current = source_url
        for _redirect in range(MAX_REDIRECTS + 1):
            await self._validate_media_url(platform, current)
            request = client.build_request("GET", current)
            response = await client.send(request, stream=True)
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.raise_for_status()
                return response, str(response.url)
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise MediaDownloadError("media redirect did not include a location")
            current = urljoin(current, location)
        raise MediaDownloadError(f"media download exceeded {MAX_REDIRECTS} redirects")

    async def _validate_media_url(
        self,
        platform: Literal["xhs", "douyin"],
        url: str,
    ) -> None:
        """Require an approved platform CDN host and a globally routable address."""
        hostname = (urlsplit(url).hostname or "").lower()
        suffixes = PLATFORM_HOST_SUFFIXES[platform]
        if not any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in suffixes):
            raise MediaDownloadError(f"unsupported {platform} media host: {hostname or 'missing'}")
        await self._url_policy.validate(url)

    def _directory(self, output_dir: str | None) -> Path:
        """Resolve a caller-selected absolute directory or the application default."""
        if output_dir is None:
            return self._default_directory
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            raise ValueError("output_dir must be an absolute path")
        return directory

    @staticmethod
    def _validate_content_type(kind: str, content_type: str) -> None:
        """Reject HTML, JSON, and mismatched media responses before writing."""
        if not content_type.startswith(f"{kind}/"):
            raise MediaDownloadError(
                f"expected {kind} response but received {content_type or 'unknown content type'}"
            )


def _content_length(response: httpx.Response) -> int | None:
    """Parse a non-negative Content-Length header when present."""
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise MediaDownloadError("media response has invalid Content-Length") from error
    if value < 0:
        raise MediaDownloadError("media response has negative Content-Length")
    return value


def _media_extension(kind: str, content_type: str, final_url: str) -> str:
    """Choose a stable extension from content type, then URL, then media kind."""
    known = CONTENT_EXTENSIONS.get(content_type)
    if known is not None:
        return known
    url_extension = Path(urlsplit(final_url).path).suffix.lower()
    if url_extension and len(url_extension) <= 8:
        return url_extension
    guessed = mimetypes.guess_extension(content_type)
    if guessed:
        return guessed
    return ".jpg" if kind == "image" else ".mp4"


def _target_path(directory: Path, stem: str, extension: str, overwrite: bool) -> Path:
    """Return an overwrite target or the first unused collision-safe filename."""
    initial = directory / f"{stem}{extension}"
    if overwrite or not initial.exists():
        return initial
    for suffix in range(2, 10_000):
        candidate = directory / f"{stem}_{suffix}{extension}"
        if not candidate.exists():
            return candidate
    raise MediaDownloadError(f"could not allocate a unique filename for {initial.name}")
