"""Tests for validated visual browser interaction requests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from browser_mcp.application.browser_service import BrowserService
from browser_mcp.config import AppSettings
from browser_mcp.models import (
    BrowserClickCoordinateSpace,
    BrowserClickRequest,
    BrowserDialogAction,
    BrowserDialogRequest,
    BrowserPressKey,
    BrowserPressRequest,
    BrowserScrollDirection,
    BrowserScrollRequest,
    BrowserSelectRequest,
    BrowserSnapshotRequest,
    BrowserTypeRequest,
    BrowserUploadRequest,
    BrowserViewport,
)
from browser_mcp.security import UploadPolicyError
from tests.helpers import FakeBridge, allow_public_url_policy


def test_viewport_accepts_fractional_css_pixels() -> None:
    """Page zoom reports fractional CSS geometry that must not fail the page state contract."""
    viewport = BrowserViewport.model_validate(
        {
            "width": 710.4,
            "height": 717.6,
            "screenshot_width": 888,
            "screenshot_height": 897,
            "device_scale_factor": 1.25,
            "scroll_x": 0,
            "scroll_y": 0,
            "document_width": 710.4,
            "document_height": 3184.8,
        },
    )
    assert (viewport.width, viewport.height) == (710, 718)
    assert (viewport.document_width, viewport.document_height) == (710, 3185)
    assert viewport.device_scale_factor == 1.25


def test_click_request_requires_exactly_one_target_strategy() -> None:
    """Clicks must never guess between absent, partial, or conflicting targets."""
    assert BrowserClickRequest(element_id="e1").element_id == "e1"
    screenshot_click = BrowserClickRequest(x=12, y=34)
    assert screenshot_click.x == 12
    assert screenshot_click.coordinate_space is BrowserClickCoordinateSpace.SCREENSHOT
    assert (
        BrowserClickRequest(
            x=12,
            y=34,
            coordinate_space=BrowserClickCoordinateSpace.VIEWPORT,
        ).coordinate_space
        is BrowserClickCoordinateSpace.VIEWPORT
    )
    with pytest.raises(ValidationError, match="either element_id or both x and y"):
        BrowserClickRequest()
    with pytest.raises(ValidationError, match="x and y must be provided together"):
        BrowserClickRequest(x=12)
    with pytest.raises(ValidationError, match="either element_id or both x and y"):
        BrowserClickRequest(element_id="e1", x=12, y=34)


@pytest.mark.asyncio
async def test_browser_service_dispatches_every_visual_action_with_typed_arguments(
    tmp_path: Path,
) -> None:
    """Application methods should preserve action semantics below the MCP transport."""
    bridge = FakeBridge(tmp_path / "extension")
    service = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )

    snapshot = await service.visual_snapshot(
        BrowserSnapshotRequest.model_validate({"url": "https://example.com/form"})
    )
    await service.click(BrowserClickRequest(element_id="e1"))
    await service.handle_dialog(BrowserDialogRequest(action=BrowserDialogAction.DISMISS))
    await service.scroll(BrowserScrollRequest(direction=BrowserScrollDirection.DOWN, amount=480))
    await service.type_text(BrowserTypeRequest(element_id="e1", text="Browser MCP"))
    await service.press(BrowserPressRequest(key=BrowserPressKey.ENTER))
    await service.select(BrowserSelectRequest(element_id="e2", value="中文"))
    poster = tmp_path / "poster.png"
    poster.write_bytes(b"png-bytes")
    await service.upload(BrowserUploadRequest(paths=(str(poster),)))

    assert snapshot.state.elements[0].name == "Search"
    assert [action for action, _args in bridge.interactions] == [
        "snapshot",
        "click",
        "dialog",
        "scroll",
        "type",
        "press",
        "select",
        "upload",
    ]
    assert bridge.interactions[3][1]["direction"] == "down"
    assert bridge.interactions[1][1]["coordinate_space"] == "screenshot"
    assert bridge.interactions[2][1]["action"] == "dismiss"
    assert bridge.interactions[5][1]["key"] == "Enter"
    assert bridge.interactions[7][1]["paths"] == [str(poster.resolve())]


def test_dialog_request_validates_native_decisions() -> None:
    """Dialog requests should expose only explicit accept or dismiss decisions."""
    request = BrowserDialogRequest(action=BrowserDialogAction.ACCEPT, prompt_text="value")

    assert request.action is BrowserDialogAction.ACCEPT
    assert request.prompt_text == "value"
    with pytest.raises(ValidationError):
        BrowserDialogRequest.model_validate({"action": "escape"})


def test_scroll_request_targets_a_container_through_a_complete_point() -> None:
    """A wheel needs both coordinates, or it would scroll whichever pane sits at the centre."""
    targeted = BrowserScrollRequest(direction=BrowserScrollDirection.DOWN, x=640, y=480)
    assert targeted.model_dump(exclude_none=True, mode="json")["x"] == 640

    centred = BrowserScrollRequest()
    assert "x" not in centred.model_dump(exclude_none=True, mode="json")

    with pytest.raises(ValidationError, match="require both x and y"):
        BrowserScrollRequest(x=640)
    with pytest.raises(ValidationError, match="require both x and y"):
        BrowserScrollRequest(y=480)


@pytest.mark.asyncio
async def test_upload_policy_runs_before_the_extension_is_reached(tmp_path: Path) -> None:
    """A refused path must never be dispatched, because Chrome itself reads the file."""
    bridge = FakeBridge(tmp_path / "extension")
    service = BrowserService(
        AppSettings(data_dir=tmp_path),
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )
    secrets = tmp_path / ".ssh"
    secrets.mkdir()
    key = secrets / "id_rsa"
    key.write_text("private")

    with pytest.raises(UploadPolicyError):
        await service.upload(BrowserUploadRequest(paths=(str(key),)))

    assert bridge.interactions == []
