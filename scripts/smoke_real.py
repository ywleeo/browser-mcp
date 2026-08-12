"""Run concise opt-in smoke tests against real Zhihu and Xiaohongshu pages."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse a narrow site selector for reproducible manual verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        choices=(
            "all",
            "auth",
            "zhihu",
            "xhs",
            "x",
            "reddit",
            "search",
            "interaction",
            "xhs-page",
            "xhs-xhr",
        ),
        default="all",
    )
    return parser.parse_args(argv)


async def _wait_connected(session: ClientSession) -> dict[str, Any]:
    """Wait briefly for the loaded Extension to attach to this smoke-test process."""
    for _attempt in range(20):
        result = await session.call_tool("browser_status", {})
        data = cast(dict[str, Any], result.structured_content or {})
        if data.get("connected") is True:
            return data
        await asyncio.sleep(1)
    raise RuntimeError("Chrome Extension did not connect within 20 seconds")


def _print_result(name: str, result: CallToolResult) -> dict[str, Any]:
    """Print a bounded summary without dumping full third-party page content."""
    data = cast(dict[str, Any], result.structured_content or {})
    summary: dict[str, object] = {"tool": name, "is_error": result.is_error}
    items = data.get("items")
    if isinstance(items, list):
        summary["item_count"] = len(items)
        if "complete" in data:
            summary["complete"] = data.get("complete")
        if "pages_fetched" in data:
            summary["pages_fetched"] = data.get("pages_fetched")
        if items and isinstance(items[0], dict):
            first = cast(dict[str, object], items[0])
            summary["first_title"] = (
                first.get("title") or first.get("question") or str(first.get("text") or "")[:120]
            )
            summary["first_url"] = first.get("url")
    elif result.is_error:
        summary["error"] = getattr(result.content[0], "text", "")[:1000]
    elif "title" in data:
        summary["title"] = data.get("title")
        summary["url"] = data.get("url")
        summary["kind"] = data.get("kind") or data.get("note_type")
        summary["complete"] = data.get("complete")
        summary["image_count"] = len(data.get("images") or [])
        if "comments" in data:
            summary["comments_returned"] = len(data.get("comments") or [])
    elif "post_id" in data:
        summary["post_id"] = data.get("post_id")
        summary["url"] = data.get("url")
        summary["author"] = data.get("handle") or data.get("author")
        summary["text_preview"] = str(data.get("text") or "")[:200]
    else:
        summary["final_url"] = data.get("final_url")
        content = str(data.get("content") or "")
        if "Captured " in content and "fetch/XHR responses" in content:
            summary["request_lines"] = [
                line for line in content.splitlines() if line.startswith("[")
            ]
        else:
            summary["content_preview"] = content[:500]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return data


def _print_visual_result(name: str, result: CallToolResult) -> dict[str, Any]:
    """Assert and summarize one screenshot-bearing interaction result."""
    if result.is_error:
        error = getattr(result.content[0], "text", "unknown interaction error")
        raise RuntimeError(f"{name} failed: {error}")
    data = cast(dict[str, Any], result.structured_content or {})
    image_count = sum(1 for content in result.content if content.type == "image")
    elements = data.get("elements")
    if image_count != 1 or not isinstance(elements, list):
        raise RuntimeError(f"{name} returned no native screenshot or element list")
    print(
        json.dumps(
            {
                "tool": name,
                "url": data.get("url"),
                "title": data.get("title"),
                "image_count": image_count,
                "element_count": len(elements),
                "scroll_y": cast(dict[str, object], data.get("viewport") or {}).get("scroll_y"),
            },
            ensure_ascii=False,
        )
    )
    return data


def _element_id(data: dict[str, Any], role: str, *, checked: bool | None = None) -> str:
    """Return the first current element reference matching one semantic smoke-test target."""
    elements = cast(list[dict[str, Any]], data.get("elements") or [])
    for element in elements:
        if element.get("role") != role:
            continue
        if checked is not None and element.get("checked") is not checked:
            continue
        element_id = element.get("element_id")
        if isinstance(element_id, str):
            return element_id
    raise RuntimeError(f"no current {role} element found in visual snapshot")


async def _run(site: str) -> None:
    """Start an official MCP stdio client and execute selected real-site reads."""
    parameters = StdioServerParameters(
        command="uv",
        args=["--directory", str(PROJECT_ROOT), "run", "browser-mcp"],
        cwd=PROJECT_ROOT,
        env={"BROWSER_MCP_LOG_LEVEL": "WARNING"},
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            status = await _wait_connected(session)
            print(
                json.dumps(
                    {
                        "extension_version": status.get("extension_version"),
                        "extension_build_id": status.get("extension_build_id"),
                        "connected": status.get("connected"),
                    },
                    ensure_ascii=False,
                )
            )
            if site in {"all", "auth"}:
                for platform in ("zhihu", "xhs", "x", "reddit"):
                    result = await session.call_tool(
                        "site_login_status",
                        {"platform": platform},
                    )
                    data = cast(dict[str, Any], result.structured_content or {})
                    print(
                        json.dumps(
                            {
                                "tool": "site_login_status",
                                "platform": platform,
                                "is_error": result.is_error,
                                "state": data.get("state"),
                                "logged_in": data.get("logged_in"),
                            },
                            ensure_ascii=False,
                        )
                    )
            if site in {"all", "zhihu"}:
                result = await session.call_tool("zhihu_search", {"keyword": "MCP"})
                data = _print_result("zhihu_search", result)
                items = data.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    first = cast(dict[str, object], items[0])
                    content = await session.call_tool(
                        "zhihu_content",
                        {"url": first.get("url"), "max_chars": 2000},
                    )
                    _print_result("zhihu_content", content)
                invitations = await session.call_tool("zhihu_invitations", {})
                _print_result("zhihu_invitations", invitations)
            if site in {"all", "xhs"}:
                user_notes = await session.call_tool("xhs_user_notes", {"max_pages": 5})
                _print_result("xhs_user_notes", user_notes)
                result = await session.call_tool("xhs_search", {"keyword": "露营"})
                data = _print_result("xhs_search", result)
                items = data.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    first = cast(dict[str, object], items[0])
                    note = await session.call_tool("xhs_note", {"url": first.get("url")})
                    _print_result("xhs_note", note)
            if site in {"all", "x"}:
                result = await session.call_tool(
                    "x_search",
                    {"keyword": "OpenAI", "sort": "latest", "limit": 5},
                )
                data = _print_result("x_search", result)
                items = data.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    first = cast(dict[str, object], items[0])
                    post = await session.call_tool("x_post", {"url": first.get("url")})
                    _print_result("x_post", post)
            if site in {"all", "reddit"}:
                result = await session.call_tool(
                    "reddit_search",
                    {"keyword": "OpenAI", "limit": 5},
                )
                data = _print_result("reddit_search", result)
                items = data.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    first = cast(dict[str, object], items[0])
                    post = await session.call_tool(
                        "reddit_post",
                        {"url": first.get("url"), "max_comments": 5},
                    )
                    _print_result("reddit_post", post)
            if site in {"all", "search"}:
                for tool in ("google_search", "bing_search", "sogou_search"):
                    result = await session.call_tool(tool, {"keyword": "OpenAI", "limit": 5})
                    _print_result(tool, result)
            if site in {"interaction"}:
                result = await session.call_tool(
                    "browser_snapshot",
                    {
                        "url": (
                            "https://testpages.eviltester.com/pages/forms/html-form/"
                            "?browser-mcp-smoke=0.8.0"
                        )
                    },
                )
                data = _print_visual_result("browser_snapshot", result)
                text_id = _element_id(data, "textbox")
                result = await session.call_tool(
                    "browser_type",
                    {"element_id": text_id, "text": "Browser MCP visual test"},
                )
                data = _print_visual_result("browser_type", result)
                if not any(
                    element.get("role") == "textbox"
                    and element.get("value") == "Browser MCP visual test"
                    for element in cast(list[dict[str, Any]], data.get("elements") or [])
                ):
                    raise RuntimeError("browser_type did not update the target value")
                stale = await session.call_tool(
                    "browser_press",
                    {"element_id": text_id, "key": "End"},
                )
                if not stale.is_error:
                    raise RuntimeError("stale element reference was unexpectedly accepted")
                print(
                    json.dumps(
                        {"tool": "browser_press[stale-ref]", "rejected": True},
                        ensure_ascii=False,
                    )
                )
                before_press = cast(dict[str, int], data["viewport"])["scroll_y"]
                result = await session.call_tool(
                    "browser_press",
                    {"element_id": _element_id(data, "textbox"), "key": "End"},
                )
                data = _print_visual_result("browser_press", result)
                if cast(dict[str, int], data["viewport"])["scroll_y"] <= before_press:
                    raise RuntimeError("browser_press End did not move the page")
                select_id = _element_id(data, "combobox")
                result = await session.call_tool(
                    "browser_select",
                    {"element_id": select_id, "value": "ms2"},
                )
                data = _print_visual_result("browser_select", result)
                if not any(
                    element.get("role") == "combobox" and element.get("value") == "ms2"
                    for element in cast(list[dict[str, Any]], data.get("elements") or [])
                ):
                    raise RuntimeError("browser_select did not update the selected value")
                checkbox_id = _element_id(data, "checkbox")
                checkbox_before = next(
                    element
                    for element in cast(list[dict[str, Any]], data.get("elements") or [])
                    if element.get("element_id") == checkbox_id
                )
                checkbox_name = checkbox_before.get("name")
                result = await session.call_tool(
                    "browser_click",
                    {"element_id": checkbox_id},
                )
                data = _print_visual_result("browser_click", result)
                matching_checkbox = [
                    element
                    for element in cast(list[dict[str, Any]], data.get("elements") or [])
                    if element.get("role") == "checkbox"
                    and element.get("name") == checkbox_name
                ]
                if not matching_checkbox or (
                    matching_checkbox[0].get("checked") is checkbox_before.get("checked")
                ):
                    raise RuntimeError("browser_click did not toggle the target checkbox")
                coordinate_target = matching_checkbox[0]
                result = await session.call_tool(
                    "browser_click",
                    {
                        "x": float(coordinate_target["x"])
                        + float(coordinate_target["width"]) / 2,
                        "y": float(coordinate_target["y"])
                        + float(coordinate_target["height"]) / 2,
                    },
                )
                data = _print_visual_result("browser_click[coordinate]", result)
                coordinate_after = next(
                    element
                    for element in cast(list[dict[str, Any]], data.get("elements") or [])
                    if element.get("role") == "checkbox"
                    and element.get("name") == coordinate_target.get("name")
                )
                if coordinate_after.get("checked") is coordinate_target.get("checked"):
                    raise RuntimeError(
                        "coordinate browser_click did not toggle the target checkbox"
                    )
                before_scroll = cast(dict[str, int], data["viewport"])["scroll_y"]
                result = await session.call_tool(
                    "browser_scroll",
                    {"direction": "down", "amount": 500},
                )
                data = _print_visual_result("browser_scroll", result)
                after_scroll = cast(dict[str, int], data["viewport"])["scroll_y"]
                if after_scroll <= before_scroll:
                    raise RuntimeError("browser_scroll did not advance the page")
            if site in {"xhs-page", "xhs-xhr"}:
                result = await session.call_tool(
                    "browser_read",
                    {
                        "url": (
                            "https://www.xiaohongshu.com/search_result"
                            "?keyword=%E9%9C%B2%E8%90%A5&source=web_explore_feed&type=0"
                        ),
                        "extract": "xhr" if site == "xhs-xhr" else "text",
                        "wait_ms": 6000 if site == "xhs-xhr" else 3000,
                        "max_chars": 100_000 if site == "xhs-xhr" else 3000,
                    },
                )
                _print_result("browser_read[xhs-page]", result)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the selected asynchronous real-site smoke test."""
    asyncio.run(_run(_arguments(argv).site))


if __name__ == "__main__":
    main()
