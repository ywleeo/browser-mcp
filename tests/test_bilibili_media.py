"""Tests for Bilibili-specific media download orchestration."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from browser_mcp.sites import bilibili_media
from browser_mcp.sites.bilibili import BilibiliMediaStreams, BilibiliMediaTrack
from browser_mcp.sites.bilibili_media import BilibiliMediaDownloader
from browser_mcp.sites.media import MediaDownloader
from browser_mcp.sites.models import BilibiliDownloadRequest
from tests.helpers import allow_public_url_policy


def _streams() -> BilibiliMediaStreams:
    """Build deterministic separate DASH tracks for orchestration tests."""
    return BilibiliMediaStreams(
        bvid="BV1eaMH6gEDx",
        cid=301,
        page=1,
        video=BilibiliMediaTrack(
            kind="video",
            url="https://video.bilivideo.com/video.m4s",
            quality_id=80,
            quality_label="高清 1080P",
            codec="avc1.640032",
            bandwidth=1_000_000,
        ),
        audio=BilibiliMediaTrack(
            kind="audio",
            url="https://audio.bilivideo.com/audio.m4s",
            quality_id=30_280,
            quality_label="audio",
            codec="mp4a.40.2",
            bandwidth=192_000,
        ),
    )


@pytest.mark.asyncio
async def test_video_download_returns_separate_tracks_without_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing FFmpeg should return tracks instead of claiming a completed mux."""

    def handle(request: httpx.Request) -> httpx.Response:
        """Return distinct MP4-container bytes for each approved Bilibili CDN URL."""
        content = b"audio" if request.url.host == "audio.bilivideo.com" else b"video"
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=content,
            request=request,
        )

    def no_ffmpeg(_command: str) -> None:
        """Simulate a system where FFmpeg is unavailable."""
        return None

    monkeypatch.setattr(bilibili_media.shutil, "which", no_ffmpeg)
    shared = MediaDownloader(
        tmp_path,
        url_policy=allow_public_url_policy(),
        transport=httpx.MockTransport(handle),
    )
    downloader = BilibiliMediaDownloader(shared)

    result = await downloader.download_video(
        _streams(),
        BilibiliDownloadRequest.model_validate(
            {
                "url": "https://www.bilibili.com/video/BV1eaMH6gEDx/",
                "output_dir": str(tmp_path),
            }
        ),
    )

    assert result.muxed is False
    assert result.downloaded == 2
    assert [item.media_type for item in result.items] == ["video", "audio"]
    assert [Path(item.path).suffix for item in result.items] == [".mp4", ".m4a"]
