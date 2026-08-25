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
)
from tests.helpers import FakeBridge, allow_public_url_policy


def test_click_request_requires_exactly_one_target_strategy() -> None:
    """Clicks must never guess between absent, partial, or conflicting targets."""
    assert BrowserClickRequest(element_id="e1").element_id == "e1"
    screenshot_click = BrowserClickRequest(x=12, y=34)
    assert screenshot_click.x == 12
    assert screenshot_click.coordinate_space is BrowserClickCoordinateSpace.SCREENSHOT
    assert BrowserClickRequest(
        x=12,
        y=34,
        coordinate_space=BrowserClickCoordinateSpace.VIEWPORT,
    ).coordinate_space is BrowserClickCoordinateSpace.VIEWPORT
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
    await service.scroll(
        BrowserScrollRequest(direction=BrowserScrollDirection.DOWN, amount=480)
    )
    await service.type_text(BrowserTypeRequest(element_id="e1", text="Browser MCP"))
    await service.press(BrowserPressRequest(key=BrowserPressKey.ENTER))
    await service.select(BrowserSelectRequest(element_id="e2", value="中文"))

    assert snapshot.state.elements[0].name == "Search"
    assert [action for action, _args in bridge.interactions] == [
        "snapshot",
        "click",
        "dialog",
        "scroll",
        "type",
        "press",
        "select",
    ]
    assert bridge.interactions[3][1]["direction"] == "down"
    assert bridge.interactions[1][1]["coordinate_space"] == "screenshot"
    assert bridge.interactions[2][1]["action"] == "dismiss"
    assert bridge.interactions[5][1]["key"] == "Enter"


def test_dialog_request_validates_native_decisions() -> None:
    """Dialog requests should expose only explicit accept or dismiss decisions."""
    request = BrowserDialogRequest(action=BrowserDialogAction.ACCEPT, prompt_text="value")

    assert request.action is BrowserDialogAction.ACCEPT
    assert request.prompt_text == "value"
    with pytest.raises(ValidationError):
        BrowserDialogRequest.model_validate({"action": "escape"})
