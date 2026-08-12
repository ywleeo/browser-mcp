"""Bounded immutable pagination for normalized site documents."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from browser_mcp.sites.models import SiteDocumentResult


class SiteSnapshotError(RuntimeError):
    """Raised for missing, expired, oversized, or invalid site snapshot pages."""


@dataclass(slots=True)
class _SiteSnapshot:
    """Immutable content plus resource and LRU metadata."""

    snapshot_id: str
    platform: str
    kind: str
    url: str
    title: str
    content: str
    total_chars: int
    byte_size: int
    created_at: float
    last_accessed_at: float


class SiteSnapshotStore:
    """Keep site documents pageable without refetching mutable upstream pages."""

    def __init__(
        self,
        *,
        max_snapshots: int = 32,
        ttl: timedelta = timedelta(hours=2),
        page_byte_limit: int = 24_000,
        total_byte_limit: int = 32 * 1024 * 1024,
    ) -> None:
        """Create an empty bounded document store."""
        if max_snapshots <= 0:
            raise ValueError("max_snapshots must be positive")
        if ttl.total_seconds() <= 0:
            raise ValueError("ttl must be positive")
        if page_byte_limit <= 0:
            raise ValueError("page_byte_limit must be positive")
        if total_byte_limit <= 0:
            raise ValueError("total_byte_limit must be positive")
        self._max_snapshots = max_snapshots
        self._ttl_seconds = ttl.total_seconds()
        self._page_byte_limit = page_byte_limit
        self._total_byte_limit = total_byte_limit
        self._snapshots: dict[str, _SiteSnapshot] = {}
        self._total_bytes = 0
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        platform: str,
        kind: str,
        url: str,
        title: str,
        content: str,
        max_chars: int,
    ) -> SiteDocumentResult:
        """Store a complete site document and return its first bounded page."""
        byte_size = len(content.encode("utf-8"))
        if byte_size > self._total_byte_limit:
            raise SiteSnapshotError("site document exceeds the snapshot memory budget")
        now = time.monotonic()
        snapshot = _SiteSnapshot(
            snapshot_id=uuid4().hex,
            platform=platform,
            kind=kind,
            url=url,
            title=title,
            content=content,
            total_chars=len(content),
            byte_size=byte_size,
            created_at=now,
            last_accessed_at=now,
        )
        async with self._lock:
            self._expire(now)
            self._evict_for(byte_size)
            self._snapshots[snapshot.snapshot_id] = snapshot
            self._total_bytes += byte_size
            return self._page(snapshot, 0, max_chars)

    async def read(self, snapshot_id: str, offset: int, max_chars: int) -> SiteDocumentResult:
        """Read another immutable page without visiting the website again."""
        now = time.monotonic()
        async with self._lock:
            self._expire(now)
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                raise SiteSnapshotError(f"site snapshot '{snapshot_id}' was not found or expired")
            snapshot.last_accessed_at = now
            return self._page(snapshot, offset, max_chars)

    def _expire(self, now: float) -> None:
        """Delete snapshots beyond their creation-time TTL."""
        expired = [
            snapshot_id
            for snapshot_id, snapshot in self._snapshots.items()
            if now - snapshot.created_at >= self._ttl_seconds
        ]
        for snapshot_id in expired:
            self._remove(snapshot_id)

    def _evict_for(self, incoming_bytes: int) -> None:
        """Evict least-recently-used documents until count and memory fit."""
        while self._snapshots and (
            len(self._snapshots) >= self._max_snapshots
            or self._total_bytes + incoming_bytes > self._total_byte_limit
        ):
            oldest = min(
                self._snapshots,
                key=lambda key: self._snapshots[key].last_accessed_at,
            )
            self._remove(oldest)

    def _remove(self, snapshot_id: str) -> None:
        """Remove one snapshot while preserving byte accounting."""
        snapshot = self._snapshots.pop(snapshot_id)
        self._total_bytes -= snapshot.byte_size

    def _page(self, snapshot: _SiteSnapshot, offset: int, max_chars: int) -> SiteDocumentResult:
        """Cut a Unicode-safe page under both character and UTF-8 byte limits."""
        if offset > snapshot.total_chars:
            raise SiteSnapshotError(
                f"site page offset {offset} exceeds document length {snapshot.total_chars}"
            )
        characters: list[str] = []
        byte_count = 0
        for character in snapshot.content[offset:]:
            size = len(character.encode("utf-8"))
            if len(characters) >= max_chars or byte_count + size > self._page_byte_limit:
                break
            characters.append(character)
            byte_count += size
        content = "".join(characters)
        end = offset + len(characters)
        complete = end >= snapshot.total_chars
        return SiteDocumentResult(
            snapshot_id=snapshot.snapshot_id,
            platform=snapshot.platform,
            kind=snapshot.kind,
            url=snapshot.url,
            title=snapshot.title,
            total_chars=snapshot.total_chars,
            range_start=offset,
            range_end=end,
            complete=complete,
            next_offset=None if complete else end,
            content=content,
        )
