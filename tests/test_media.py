"""Tests for bounded platform media downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from browser_mcp.sites.media import MediaDownloader, MediaDownloadError, MediaSource
from tests.helpers import allow_public_url_policy


@pytest.mark.asyncio
async def test_media_downloader_streams_redirect_and_avoids_collisions(tmp_path: Path) -> None:
    """Validated CDN redirects should publish exact files without overwriting prior runs."""
    content = b"test-image-content"

    def handle(request: httpx.Request) -> httpx.Response:
        """Return one approved redirect followed by a JPEG payload."""
        if request.url.host == "sns-img.xhscdn.com":
            return httpx.Response(
                302,
                headers={"location": "https://sns-img-qc.xhscdn.com/final"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": str(len(content))},
            content=content,
            request=request,
        )

    downloader = MediaDownloader(
        tmp_path,
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    arguments = {
        "platform": "xhs",
        "post_id": "note1",
        "page_url": "https://www.xiaohongshu.com/explore/note1",
        "sources": (MediaSource(kind="image", url="https://sns-img.xhscdn.com/start"),),
        "output_dir": None,
        "overwrite": False,
    }

    first = await downloader.download(**arguments)  # type: ignore[arg-type]
    second = await downloader.download(**arguments)  # type: ignore[arg-type]

    assert first.downloaded == 1
    assert first.total_bytes == len(content)
    assert first.items[0].sha256 == hashlib.sha256(content).hexdigest()
    assert Path(first.items[0].path).read_bytes() == content
    assert first.items[0].final_url == "https://sns-img-qc.xhscdn.com/final"
    assert second.items[0].path.endswith("xhs_note1_image_01_2.jpg")


@pytest.mark.asyncio
async def test_media_downloader_rejects_wrong_host_and_content_type(tmp_path: Path) -> None:
    """Page-derived URLs still must remain on approved CDNs and return expected media."""

    def handle(request: httpx.Request) -> httpx.Response:
        """Return a non-media response for an otherwise approved host."""
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"blocked",
            request=request,
        )

    downloader = MediaDownloader(
        tmp_path,
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    common = {
        "platform": "douyin",
        "post_id": "123",
        "page_url": "https://www.douyin.com/video/123",
        "output_dir": None,
        "overwrite": False,
    }

    with pytest.raises(MediaDownloadError, match="unsupported douyin media host"):
        await downloader.download(
            **common,  # type: ignore[arg-type]
            sources=(MediaSource(kind="video", url="https://example.com/video.mp4"),),
        )
    with pytest.raises(MediaDownloadError, match="expected video response"):
        await downloader.download(
            **common,  # type: ignore[arg-type]
            sources=(MediaSource(kind="video", url="https://v.douyinvod.com/video"),),
        )


@pytest.mark.asyncio
async def test_media_downloader_requires_absolute_output_directory(tmp_path: Path) -> None:
    """Relative output paths must not depend on the MCP process working directory."""
    downloader = MediaDownloader(tmp_path, url_policy=allow_public_url_policy())

    with pytest.raises(ValueError, match="output_dir must be an absolute path"):
        await downloader.download(
            platform="xhs",
            post_id="n1",
            page_url="https://www.xiaohongshu.com/explore/n1",
            sources=(),
            output_dir="downloads",
            overwrite=False,
        )
