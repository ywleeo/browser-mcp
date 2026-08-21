"""Tests for bounded platform media downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from browser_mcp.sites import media as media_module
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


@pytest.mark.asyncio
async def test_media_downloader_supports_bilibili_audio_cdn_contract(tmp_path: Path) -> None:
    """Verified Bilibili CDN audio may use generic or MP4-container content types."""
    observed_headers: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        """Capture Bilibili Referer/Origin and return one audio track."""
        observed_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"audio-track",
            request=request,
        )

    downloader = MediaDownloader(
        tmp_path,
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    result = await downloader.download(
        platform="bilibili",
        post_id="BV1eaMH6gEDx_p01",
        page_url="https://www.bilibili.com/video/BV1eaMH6gEDx/",
        sources=(MediaSource(kind="audio", url="https://audio.mcdn.bilivideo.cn/track.m4s"),),
        output_dir=None,
        overwrite=False,
    )

    assert Path(result.items[0].path).suffix == ".m4a"
    assert observed_headers["origin"] == "https://www.bilibili.com"
    assert observed_headers["referer"].endswith("BV1eaMH6gEDx/")


@pytest.mark.asyncio
async def test_media_downloader_reports_progress_and_ignores_reporter_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress callbacks run during streaming; a failing reporter never fails the download."""
    content = b"x" * 4096
    calls: list[tuple[int, int | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": str(len(content))},
            content=content,
            request=request,
        )

    async def record(done: int, total: int | None) -> None:
        calls.append((done, total))

    # Report on every chunk instead of waiting for the throttle interval.
    monkeypatch.setattr(media_module, "PROGRESS_REPORT_INTERVAL_SECONDS", 0.0)

    downloader = MediaDownloader(
        tmp_path,
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    result = await downloader.download(
        platform="xhs",
        post_id="progress1",
        page_url="https://www.xiaohongshu.com/explore/progress1",
        sources=(MediaSource(kind="image", url="https://sns-img.xhscdn.com/pic"),),
        output_dir=None,
        overwrite=False,
        progress_callback=record,
    )

    assert result.total_bytes == len(content)
    assert calls, "progress callback must fire while streaming"
    assert calls[-1] == (len(content), len(content))

    # A raising reporter must not abort the download.
    async def explode(_done: int, _total: int | None) -> None:
        raise RuntimeError("reporter failed")

    result = await downloader.download(
        platform="xhs",
        post_id="progress2",
        page_url="https://www.xiaohongshu.com/explore/progress2",
        sources=(MediaSource(kind="image", url="https://sns-img.xhscdn.com/pic"),),
        output_dir=None,
        overwrite=False,
        progress_callback=explode,
    )
    assert result.downloaded == 1
