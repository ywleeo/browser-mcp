"""Bounded immutable in-memory snapshots with Unicode-safe pagination."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import uuid4

from browser_mcp.models import BrowserReadResult, ExtractMode

DEFAULT_MAX_SNAPSHOTS: Final = 32
DEFAULT_TTL: Final = timedelta(hours=2)
DEFAULT_PAGE_BYTE_LIMIT: Final = 24_000
DEFAULT_TOTAL_BYTE_LIMIT: Final = 32 * 1024 * 1024


class SnapshotError(RuntimeError):
    """Base failure for expired, missing, oversized, or invalid snapshot reads."""


@dataclass(slots=True)
class Snapshot:
    """Immutable extracted content plus mutable LRU access metadata."""

    snapshot_id: str
    url: str
    final_url: str
    extract_mode: ExtractMode
    load_timed_out: bool
    warnings: tuple[str, ...]
    content: str
    byte_size: int
    total_chars: int
    created_at: float
    last_accessed_at: float


class SnapshotStore:
    """Retain complete extraction results under count, age, and memory limits."""

    def __init__(
        self,
        *,
        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
        ttl: timedelta = DEFAULT_TTL,
        page_byte_limit: int = DEFAULT_PAGE_BYTE_LIMIT,
        total_byte_limit: int = DEFAULT_TOTAL_BYTE_LIMIT,
    ) -> None:
        """Create an empty store with explicit resource ceilings."""
        if min(max_snapshots, page_byte_limit, total_byte_limit) <= 0:
            raise ValueError("snapshot limits must be positive")
        if ttl.total_seconds() <= 0:
            raise ValueError("snapshot TTL must be positive")
        self._max_snapshots = max_snapshots
        self._ttl_seconds = ttl.total_seconds()
        self._page_byte_limit = page_byte_limit
        self._total_byte_limit = total_byte_limit
        self._snapshots: dict[str, Snapshot] = {}
        self._total_bytes = 0
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        url: str,
        final_url: str,
        extract_mode: ExtractMode,
        load_timed_out: bool,
        warnings: tuple[str, ...],
        content: str,
        max_chars: int,
    ) -> BrowserReadResult:
        """Store one complete extraction and return its first bounded page."""
        byte_size = len(content.encode("utf-8"))
        if byte_size > self._total_byte_limit:
            raise SnapshotError(
                f"browser extraction is too large ({byte_size} bytes); "
                f"snapshot budget is {self._total_byte_limit} bytes"
            )
        now = time.monotonic()
        snapshot = Snapshot(
            snapshot_id=uuid4().hex,
            url=url,
            final_url=final_url,
            extract_mode=extract_mode,
            load_timed_out=load_timed_out,
            warnings=warnings,
            content=content,
            byte_size=byte_size,
            total_chars=len(content),
            created_at=now,
            last_accessed_at=now,
        )
        async with self._lock:
            self._expire(now)
            self._evict_for(byte_size)
            self._snapshots[snapshot.snapshot_id] = snapshot
            self._total_bytes += byte_size
            return self._page(snapshot, offset=0, max_chars=max_chars)

    async def read(self, snapshot_id: str, offset: int, max_chars: int) -> BrowserReadResult:
        """Read a consecutive page without revisiting or mutating its content."""
        now = time.monotonic()
        async with self._lock:
            self._expire(now)
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise SnapshotError(
                    f"browser snapshot '{snapshot_id}' was not found or expired; "
                    "call browser_read again"
                )
            snapshot.last_accessed_at = now
            return self._page(snapshot, offset=offset, max_chars=max_chars)

    def _expire(self, now: float) -> None:
        """Remove snapshots older than the configured creation-time TTL."""
        expired = [
            snapshot_id
            for snapshot_id, snapshot in self._snapshots.items()
            if now - snapshot.created_at >= self._ttl_seconds
        ]
        for snapshot_id in expired:
            self._remove(snapshot_id)

    def _evict_for(self, byte_size: int) -> None:
        """Evict least-recently-used snapshots until count and memory fit."""
        while self._snapshots and (
            len(self._snapshots) >= self._max_snapshots
            or self._total_bytes + byte_size > self._total_byte_limit
        ):
            oldest_id = min(
                self._snapshots,
                key=lambda snapshot_id: self._snapshots[snapshot_id].last_accessed_at,
            )
            self._remove(oldest_id)

    def _remove(self, snapshot_id: str) -> None:
        """Remove one known snapshot while maintaining the byte accounting invariant."""
        snapshot = self._snapshots.pop(snapshot_id)
        self._total_bytes -= snapshot.byte_size

    def _page(self, snapshot: Snapshot, offset: int, max_chars: int) -> BrowserReadResult:
        """Cut one page without splitting Unicode or exceeding the UTF-8 byte ceiling."""
        if offset > snapshot.total_chars:
            raise SnapshotError(
                f"browser page offset {offset} is past the end of the snapshot "
                f"({snapshot.total_chars} chars)"
            )
        characters: list[str] = []
        byte_count = 0
        for character in snapshot.content[offset:]:
            encoded_size = len(character.encode("utf-8"))
            if len(characters) >= max_chars or byte_count + encoded_size > self._page_byte_limit:
                break
            characters.append(character)
            byte_count += encoded_size
        content = "".join(characters)
        range_end = offset + len(characters)
        complete = range_end >= snapshot.total_chars
        return BrowserReadResult(
            snapshot_id=snapshot.snapshot_id,
            url=snapshot.url,
            final_url=snapshot.final_url,
            extract_mode=snapshot.extract_mode,
            total_chars=snapshot.total_chars,
            range_start=offset,
            range_end=range_end,
            complete=complete,
            next_offset=None if complete else range_end,
            load_timed_out=snapshot.load_timed_out,
            warnings=snapshot.warnings,
            content=content,
        )
