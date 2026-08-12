"""In-process tests for MCP tool registration and result conversion."""

import json
from pathlib import Path
from typing import cast

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ImageContent, TextContent

from browser_mcp.application import BrowserService
from browser_mcp.config import AppSettings
from browser_mcp.mcp.server import create_server
from tests.helpers import FakeBridge, allow_public_url_policy


@pytest.mark.asyncio
async def test_server_exposes_completed_read_and_interaction_tools(tmp_path: Path) -> None:
    """The public tool surface should describe read and interaction risk accurately."""
    settings = AppSettings(data_dir=tmp_path)
    service = BrowserService(
        settings,
        bridge=FakeBridge(tmp_path / "extension"),
        url_policy=allow_public_url_policy(),
    )
    server = create_server(settings, service)

    tools = await server.list_tools()
    names = [tool.name for tool in tools]

    assert names == [
        "browser_status",
        "browser_read",
        "browser_read_page",
        "browser_snapshot",
        "browser_click",
        "browser_scroll",
        "browser_type",
        "browser_press",
        "browser_select",
        "site_login_status",
        "zhihu_search",
        "zhihu_content",
        "zhihu_invitations",
        "xhs_search",
        "xhs_note",
        "xhs_user_notes",
        "x_search",
        "x_post",
        "reddit_search",
        "reddit_post",
        "google_search",
        "bing_search",
        "sogou_search",
        "site_read_page",
    ]
    assert all(tool.annotations is not None for tool in tools)
    annotations = {tool.name: tool.annotations for tool in tools}
    snapshot_annotations = annotations["browser_snapshot"]
    scroll_annotations = annotations["browser_scroll"]
    click_annotations = annotations["browser_click"]
    type_annotations = annotations["browser_type"]
    press_annotations = annotations["browser_press"]
    assert snapshot_annotations is not None and snapshot_annotations.read_only_hint is True
    assert scroll_annotations is not None and scroll_annotations.read_only_hint is True
    assert click_annotations is not None and click_annotations.read_only_hint is False
    assert click_annotations.destructive_hint is True
    assert type_annotations is not None and type_annotations.read_only_hint is False
    assert press_annotations is not None and press_annotations.destructive_hint is True
    assert tools[0].annotations is not None
    assert tools[0].annotations.open_world_hint is False
    assert tools[1].annotations is not None
    assert tools[1].annotations.open_world_hint is True


@pytest.mark.asyncio
async def test_browser_snapshot_returns_native_image_elements_and_structured_state(
    tmp_path: Path,
) -> None:
    """Visual state must be directly visible to agents without base64 in the JSON text."""
    settings = AppSettings(data_dir=tmp_path)
    bridge = FakeBridge(tmp_path / "extension")
    service = BrowserService(
        settings,
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )

    result = cast(
        CallToolResult,
        await create_server(settings, service).call_tool(
            "browser_snapshot",
            {"url": "https://example.com/form"},
        ),
    )

    assert result.is_error is False
    assert isinstance(result.content[0], ImageContent)
    assert result.content[0].mime_type == "image/jpeg"
    assert isinstance(result.content[1], TextContent)
    assert "/9j/2Q==" not in result.content[1].text
    assert result.structured_content is not None
    state = cast(dict[str, object], result.structured_content)
    assert state["action"] == "snapshot"
    assert cast(list[dict[str, object]], state["elements"])[0]["element_id"] == "e1"
    assert bridge.interactions == [
        ("snapshot", {"wait_ms": 500, "url": "https://example.com/form"})
    ]


@pytest.mark.asyncio
async def test_browser_status_has_structured_and_text_content(tmp_path: Path) -> None:
    """Structured output should remain usable by clients that only render text."""
    settings = AppSettings(data_dir=tmp_path)
    service = BrowserService(
        settings,
        bridge=FakeBridge(tmp_path / "extension"),
        url_policy=allow_public_url_policy(),
    )
    result = cast(
        CallToolResult,
        await create_server(settings, service).call_tool("browser_status", {}),
    )

    assert result.is_error is False
    assert result.structured_content is not None
    structured = cast(dict[str, object], result.structured_content)
    assert structured["state"] == "disconnected"
    assert result.content
    first_content = result.content[0]
    assert isinstance(first_content, TextContent)
    rendered = cast(dict[str, object], json.loads(first_content.text))
    assert rendered["bridge_port_pool"] == [17_880, 17_889]


@pytest.mark.asyncio
async def test_browser_read_returns_a_structured_snapshot_without_stopping_server(
    tmp_path: Path,
) -> None:
    """Fetch MVP should return structured content and leave later status calls healthy."""
    settings = AppSettings(data_dir=tmp_path)
    service = BrowserService(
        settings,
        bridge=FakeBridge(tmp_path / "extension"),
        url_policy=allow_public_url_policy(),
    )
    server = create_server(settings, service)

    result = cast(
        CallToolResult,
        await server.call_tool(
            "browser_read",
            {"url": "https://example.com", "extract": "text"},
        ),
    )
    healthy = cast(CallToolResult, await server.call_tool("browser_status", {}))

    assert result.is_error is False
    assert result.structured_content is not None
    assert healthy.is_error is False


@pytest.mark.asyncio
async def test_platform_tool_returns_login_prompt_without_executing_task(tmp_path: Path) -> None:
    """MCP callers should receive an actionable error when the platform is logged out."""
    settings = AppSettings(data_dir=tmp_path)
    bridge = FakeBridge(tmp_path / "extension")
    bridge.logged_in_sites["x"] = False
    service = BrowserService(
        settings,
        bridge=bridge,
        url_policy=allow_public_url_policy(),
    )

    with pytest.raises(ToolError, match="本次任务未执行"):
        await create_server(settings, service).call_tool(
            "x_search",
            {"keyword": "不应执行"},
        )

    assert len(bridge.fetches) == 1
