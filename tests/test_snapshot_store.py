"""Tests for bounded immutable browser snapshots."""

from datetime import timedelta

import pytest

from browser_mcp.models import ExtractMode
from browser_mcp.snapshot import SnapshotError, SnapshotStore


@pytest.mark.asyncio
async def test_unicode_pages_reconstruct_content_under_byte_limit() -> None:
    """Sequential offsets must preserve every code point exactly once."""
    source = "甲乙丙丁🙂" * 100
    store = SnapshotStore(page_byte_limit=31)
    page = await store.create(
        url="https://example.com",
        final_url="https://example.com/final",
        extract_mode=ExtractMode.TEXT,
        load_timed_out=False,
        warnings=(),
        content=source,
        max_chars=1000,
    )
    reconstructed = page.content
    while not page.complete:
        assert page.next_offset is not None
        page = await store.read(page.snapshot_id, page.next_offset, 1000)
        assert len(page.content.encode("utf-8")) <= 31
        reconstructed += page.content

    assert reconstructed == source


@pytest.mark.asyncio
async def test_lru_count_limit_evicts_only_the_oldest_snapshot() -> None:
    """Count pressure should retain the recently created snapshot and reject the evicted id."""
    store = SnapshotStore(max_snapshots=1)
    first = await store.create(
        url="https://one.example",
        final_url="https://one.example",
        extract_mode=ExtractMode.TEXT,
        load_timed_out=False,
        warnings=(),
        content="first",
        max_chars=100,
    )
    second = await store.create(
        url="https://two.example",
        final_url="https://two.example",
        extract_mode=ExtractMode.TEXT,
        load_timed_out=False,
        warnings=(),
        content="second",
        max_chars=100,
    )

    with pytest.raises(SnapshotError, match="not found or expired"):
        await store.read(first.snapshot_id, 0, 100)
    assert (await store.read(second.snapshot_id, 0, 100)).content == "second"


def test_snapshot_limits_must_be_positive() -> None:
    """Invalid memory and lifetime configuration must fail at construction."""
    with pytest.raises(ValueError):
        SnapshotStore(total_byte_limit=0)
    with pytest.raises(ValueError):
        SnapshotStore(ttl=timedelta(0))
