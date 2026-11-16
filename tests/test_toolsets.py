"""Tool-group selection: one connection must not have to expose all 41 tools."""

from __future__ import annotations

import asyncio

import pytest

from browser_mcp.config import TOOL_GROUP_NAMES, AppSettings
from browser_mcp.mcp.server import TOOL_GROUP_BY_NAME, create_server, exposed_tool_names


def _names(settings: AppSettings) -> list[str]:
    server = create_server(settings)

    async def list_names() -> list[str]:
        tools = await server.list_tools()
        return sorted(tool.name for tool in tools)

    return asyncio.run(list_names())


def test_every_group_covers_every_registered_tool() -> None:
    """The group table must stay in step with the tools the server registers."""
    assert len(_names(AppSettings())) == 41
    assert len(TOOL_GROUP_BY_NAME) == 41
    assert set(TOOL_GROUP_NAMES) == set(TOOL_GROUP_BY_NAME.values())


def test_default_keeps_every_tool() -> None:
    """An unset selection preserves the previous behavior."""
    assert len(_names(AppSettings(toolsets=None))) == 41


def test_core_and_search_are_the_small_default_set() -> None:
    """The two everyday groups stay well under half of the full surface."""
    names = _names(AppSettings(toolsets=frozenset({"core", "search"})))

    assert len(names) == 15
    assert "browser_read" in names
    assert "google_search" in names
    assert "xhs_search" not in names
    assert "zhihu_content" not in names


def test_platform_groups_are_isolated() -> None:
    """Selecting one platform must not drag in another platform's tools."""
    names = _names(AppSettings(toolsets=frozenset({"zhihu", "xhs"})))

    assert "zhihu_content" in names
    assert "xhs_note" in names
    assert "douyin_video" not in names
    assert "browser_read" not in names


def test_environment_selection_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented variable selects groups with the same validation."""
    monkeypatch.setenv("BROWSER_MCP_TOOLSETS", "core, search")

    settings = AppSettings.from_env()

    assert settings.toolsets == frozenset({"core", "search"})
    assert exposed_tool_names(settings.toolsets) is not None


def test_unknown_group_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo fails at startup and lists the valid groups."""
    monkeypatch.setenv("BROWSER_MCP_TOOLSETS", "core,bogus")

    with pytest.raises(ValueError, match="unknown group"):
        AppSettings.from_env()
