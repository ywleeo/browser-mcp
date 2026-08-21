"""Bilibili-specific DASH download and lossless mux orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from browser_mcp.sites.bilibili import BilibiliMediaStreams
from browser_mcp.sites.media import (
    MediaDownloader,
    MediaDownloadError,
    MediaSource,
    ProgressCallback,
)
from browser_mcp.sites.models import (
    BilibiliDownloadRequest,
    BilibiliDownloadResult,
    MediaDownloadItem,
    MediaDownloadResult,
)

MUX_TIMEOUT_SECONDS: Final = 600.0


class BilibiliMediaDownloader:
    """Download Bilibili tracks safely and mux DASH audio/video when FFmpeg is available."""

    def __init__(self, downloader: MediaDownloader, ffmpeg_path: str | None = None) -> None:
        """Bind the shared bounded downloader and an optional explicit FFmpeg executable."""
        self._downloader = downloader
        self._ffmpeg_path = ffmpeg_path

    async def download_video(
        self,
        streams: BilibiliMediaStreams,
        request: BilibiliDownloadRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> BilibiliDownloadResult:
        """Download the selected video and mux its companion audio without transcoding."""
        page_url = _page_url(streams)
        post_id = _download_id(streams)
        max_file_bytes = request.max_file_mb * 1_048_576
        if streams.audio is None:
            downloaded = await self._downloader.download(
                platform="bilibili",
                post_id=post_id,
                page_url=page_url,
                sources=(MediaSource(kind="video", url=streams.video.url),),
                output_dir=request.output_dir,
                overwrite=request.overwrite,
                max_file_bytes=max_file_bytes,
                progress_callback=progress_callback,
            )
            return _result(streams, "video", False, downloaded)

        ffmpeg = self._ffmpeg_path or shutil.which("ffmpeg")
        if not ffmpeg:
            downloaded = await self._downloader.download(
                platform="bilibili",
                post_id=post_id,
                page_url=page_url,
                sources=(
                    MediaSource(kind="video", url=streams.video.url),
                    MediaSource(kind="audio", url=streams.audio.url),
                ),
                output_dir=request.output_dir,
                overwrite=request.overwrite,
                max_file_bytes=max_file_bytes,
                progress_callback=progress_callback,
            )
            return _result(streams, "video", False, downloaded)

        destination = self._downloader.resolve_directory(request.output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        target = _target_path(destination, post_id, request.overwrite)
        nonce = secrets.token_hex(6)
        temporary_target = target.with_name(f".{target.stem}.{nonce}.part{target.suffix}")
        temporary_target.unlink(missing_ok=True)
        try:
            with TemporaryDirectory(prefix="browser-mcp-bilibili-") as staging:
                staged = await self._downloader.download(
                    platform="bilibili",
                    post_id=post_id,
                    page_url=page_url,
                    sources=(
                        MediaSource(kind="video", url=streams.video.url),
                        MediaSource(kind="audio", url=streams.audio.url),
                    ),
                    output_dir=staging,
                    overwrite=True,
                    max_file_bytes=max_file_bytes,
                    progress_callback=progress_callback,
                )
                await _mux_tracks(
                    ffmpeg,
                    Path(staged.items[0].path),
                    Path(staged.items[1].path),
                    temporary_target,
                )
                size = temporary_target.stat().st_size
                if size > max_file_bytes:
                    raise MediaDownloadError(
                        f"muxed Bilibili video exceeds {max_file_bytes} byte limit: {size}"
                    )
                digest = await asyncio.to_thread(_sha256_file, temporary_target)
                temporary_target.replace(target)
        finally:
            temporary_target.unlink(missing_ok=True)
        item = MediaDownloadItem(
            index=1,
            media_type="video",
            source_url=streams.video.url,
            final_url=streams.video.url,
            path=str(target),
            bytes=size,
            content_type="video/mp4",
            sha256=digest,
        )
        return BilibiliDownloadResult(
            platform="bilibili",
            post_id=streams.bvid,
            cid=streams.cid,
            page=streams.page,
            media="video",
            quality_id=streams.video.quality_id,
            quality_label=streams.video.quality_label,
            codec=streams.video.codec,
            muxed=True,
            output_dir=str(destination),
            downloaded=1,
            total_bytes=size,
            items=(item,),
        )

    async def download_audio(
        self,
        streams: BilibiliMediaStreams,
        request: BilibiliDownloadRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> BilibiliDownloadResult:
        """Download only the highest-bandwidth compatible audio track."""
        if streams.audio is None:
            raise MediaDownloadError("Bilibili playinfo contains no separate audio stream")
        downloaded = await self._downloader.download(
            platform="bilibili",
            post_id=_download_id(streams),
            page_url=_page_url(streams),
            sources=(MediaSource(kind="audio", url=streams.audio.url),),
            output_dir=request.output_dir,
            overwrite=request.overwrite,
            max_file_bytes=request.max_file_mb * 1_048_576,
            progress_callback=progress_callback,
        )
        return _result(streams, "audio", False, downloaded)


async def _mux_tracks(ffmpeg: str, video: Path, audio: Path, output: Path) -> None:
    """Run one bounded FFmpeg stream-copy operation without invoking a shell."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=MUX_TIMEOUT_SECONDS)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise MediaDownloadError("FFmpeg timed out while muxing Bilibili tracks") from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-1_000:]
        raise MediaDownloadError(f"FFmpeg could not mux Bilibili tracks: {detail or 'unknown'}")
    if not output.is_file() or output.stat().st_size <= 0:
        raise MediaDownloadError("FFmpeg returned success without a Bilibili video file")


def _result(
    streams: BilibiliMediaStreams,
    media: str,
    muxed: bool,
    downloaded: MediaDownloadResult,
) -> BilibiliDownloadResult:
    """Wrap shared downloader output with Bilibili page and track metadata."""
    selected = streams.audio if media == "audio" else streams.video
    if selected is None:
        raise MediaDownloadError(f"Bilibili playinfo contains no {media} stream")
    return BilibiliDownloadResult(
        platform="bilibili",
        post_id=streams.bvid,
        cid=streams.cid,
        page=streams.page,
        media=media,
        quality_id=selected.quality_id,
        quality_label=selected.quality_label,
        codec=selected.codec,
        muxed=muxed,
        output_dir=downloaded.output_dir,
        downloaded=downloaded.downloaded,
        total_bytes=downloaded.total_bytes,
        items=downloaded.items,
    )


def _page_url(streams: BilibiliMediaStreams) -> str:
    """Build the canonical selected-page Referer used by Bilibili CDNs."""
    suffix = f"?p={streams.page}" if streams.page > 1 else "/"
    return f"https://www.bilibili.com/video/{streams.bvid}{suffix}"


def _download_id(streams: BilibiliMediaStreams) -> str:
    """Return a collision-resistant filename identity for one multipart page."""
    return f"{streams.bvid}_p{streams.page:02d}"


def _target_path(directory: Path, post_id: str, overwrite: bool) -> Path:
    """Return an overwrite target or the first unused muxed-video filename."""
    initial = directory / f"bilibili_{post_id}.mp4"
    if overwrite or not initial.exists():
        return initial
    for suffix in range(2, 10_000):
        candidate = directory / f"bilibili_{post_id}_{suffix}.mp4"
        if not candidate.exists():
            return candidate
    raise MediaDownloadError(f"could not allocate a unique filename for {initial.name}")


def _sha256_file(path: Path) -> str:
    """Hash one completed local media file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()
