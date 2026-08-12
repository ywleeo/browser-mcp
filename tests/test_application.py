"""Tests for stage-gated browser application behavior."""

from pathlib import Path

import pytest

from browser_mcp.application.browser_service import BrowserService
from browser_mcp.config import AppSettings
from browser_mcp.models import BrowserFetchPayload, BrowserReadRequest, SnapshotPageRequest
from tests.helpers import FakeBridge, allow_public_url_policy


@pytest.mark.asyncio
async def test_stage_two_status_delegates_to_independent_bridge(tmp_path: Path) -> None:
    """Application status must expose the real bridge state and independent ports."""
    bridge = FakeBridge(tmp_path / "extension")
    service = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )

    await service.start()
    status = await service.status()
    await service.close()

    assert bridge.started is True
    assert bridge.closed is True
    assert status.state == "disconnected"
    assert status.connected is False
    assert status.bridge_port == 17_880
    assert status.bridge_port_pool == (17_880, 17_889)
    assert status.extension_dir == str(tmp_path / "extension")


@pytest.mark.asyncio
async def test_fetch_snapshot_pages_are_immutable_and_lossless(tmp_path: Path) -> None:
    """The first fetch and later pages must reconstruct one rendered Chrome result."""
    payload = BrowserFetchPayload(
        final_url="https://example.com/final",
        html="<html><head><title>Dashboard</title></head><body>ignored</body></html>",
        text="甲乙丙丁戊己庚辛",
        load_timed_out=True,
    )
    bridge = FakeBridge(tmp_path / "extension", payload)
    service = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    request = BrowserReadRequest.model_validate(
        {
            "url": "https://example.com/start",
            "extract": "text",
            "max_chars": 8,
        }
    )

    first = await service.read(request)
    second = await service.read_page(
        SnapshotPageRequest(
            snapshot_id=first.snapshot_id,
            offset=first.next_offset or 0,
            max_chars=100,
        )
    )

    assert bridge.fetches == [request]
    assert first.final_url == payload.final_url
    assert first.load_timed_out is True
    assert first.warnings
    assert first.content + second.content == (
        "# Dashboard\nURL: https://example.com/final\n\n甲乙丙丁戊己庚辛"
    )
    assert second.complete is True
