"""End-to-end stdio contract test using the official MCP client."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from tests.helpers import reserve_free_port

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_initialize_list_call_and_clean_shutdown(tmp_path: Path) -> None:
    """A subprocess must complete the real MCP handshake and tool call lifecycle."""
    bridge_port = reserve_free_port()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "browser_mcp"],
        cwd=PROJECT_ROOT,
        env={
            "BROWSER_MCP_BRIDGE_PORT": str(bridge_port),
            "BROWSER_MCP_DATA_DIR": str(tmp_path),
        },
    )

    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            unavailable = await session.call_tool("browser_read", {"url": "https://93.184.216.34/"})
            status = await session.call_tool("browser_status", {})

            assert initialized.server_info.name == "browser-mcp"
            assert [tool.name for tool in tools.tools] == [
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
                "xhs_like",
                "xhs_collect",
                "xhs_download",
                "xhs_comments",
                "xhs_user_notes",
                "douyin_search",
                "douyin_video",
                "douyin_like",
                "douyin_collect",
                "douyin_download",
                "douyin_comments",
                "x_search",
                "x_post",
                "reddit_search",
                "reddit_post",
                "google_search",
                "bing_search",
                "sogou_search",
                "site_read_page",
            ]
            assert unavailable.is_error is True
            assert status.is_error is False
            assert status.structured_content is not None
            structured = cast(dict[str, object], status.structured_content)
            assert structured["state"] == "disconnected"
            assert structured["bridge_port"] == bridge_port
            assert structured["extension_dir"] == str(tmp_path / "extension")
            assert structured["server_version"] == "0.10.0"
            assert structured["install_mode"] == "source"
            assert structured["project_root"] == str(PROJECT_ROOT)
            assert "--check --json" in cast(str, structured["upgrade_check_command"])
            assert "--apply --json" in cast(str, structured["upgrade_apply_command"])

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", bridge_port))
