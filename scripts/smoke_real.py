"""Run concise opt-in smoke tests against real browser-backed website adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
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
            "xhs-media",
            "douyin",
            "douyin-search",
            "douyin-first",
            "douyin-comments",
            "download",
            "download-matrix",
            "download-douyin-note",
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
        expected_build = ""
        build_info = PROJECT_ROOT / "extension" / "build-info.js"
        if build_info.is_file():
            match = re.search(
                r'BUNDLE_BUILD_ID\s*=\s*"([0-9a-f]+)"',
                build_info.read_text(encoding="utf-8"),
            )
            expected_build = match.group(1) if match is not None else ""
        if data.get("connected") is True and (
            not expected_build or data.get("extension_build_id") == expected_build
        ):
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
        if "downloaded" in data:
            summary["downloaded"] = data.get("downloaded")
            summary["total_bytes"] = data.get("total_bytes")
            if items and isinstance(items[0], dict):
                summary["first_path"] = items[0].get("path")
        if name.endswith("_comments"):
            summary["root_count"] = sum(
                1 for item in items if isinstance(item, dict) and item.get("depth") == 0
            )
            summary["reply_count"] = sum(
                1 for item in items if isinstance(item, dict) and item.get("depth") != 0
            )
            summary["total"] = data.get("total")
        if "complete" in data:
            summary["complete"] = data.get("complete")
        if "pages_fetched" in data:
            summary["pages_fetched"] = data.get("pages_fetched")
        if items and isinstance(items[0], dict):
            first = cast(dict[str, object], items[0])
            summary["first_title"] = (
                first.get("title")
                or first.get("question")
                or first.get("description")
                or str(first.get("text") or "")[:120]
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
    elif "aweme_id" in data:
        summary["aweme_id"] = data.get("aweme_id")
        summary["url"] = data.get("url")
        summary["author"] = data.get("author")
        summary["description_preview"] = str(data.get("description") or "")[:200]
        summary["fetched"] = data.get("fetched")
        summary["complete"] = data.get("complete")
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
                for platform in ("zhihu", "xhs", "douyin", "x", "reddit"):
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
            if site in {"xhs-media"}:
                image_download = await session.call_tool(
                    "xhs_download",
                    {
                        "url": (
                            "https://www.xiaohongshu.com/explore/6a7e86010000000032021f2b"
                            "?xsec_token=AB9OF0PnPVXAU5uBPH-g3fXXFr1ywhGgvbOXulXfskASw%3D"
                            "&xsec_source=pc_search"
                        ),
                        "media": "images",
                        "max_file_mb": 256,
                    },
                )
                _print_result("xhs_download[images]", image_download)
                video_download = await session.call_tool(
                    "xhs_download",
                    {
                        "url": (
                            "https://www.xiaohongshu.com/explore/68c3c653000000001c005b89"
                            "?xsec_token=ABgcNAzS19QZjMU_864PXkO5TeN0pHfOj_xsnW0pW1TCU%3D"
                            "&xsec_source=pc_search"
                        ),
                        "media": "video",
                        "max_file_mb": 512,
                    },
                )
                _print_result("xhs_download[video]", video_download)
            if site in {"douyin", "douyin-search"}:
                result = await session.call_tool(
                    "douyin_search",
                    {"keyword": "牵手 APP", "limit": 5},
                )
                data = _print_result("douyin_search", result)
                items = data.get("items")
                if site == "douyin-search" and isinstance(items, list):
                    print(
                        json.dumps(
                            {
                                "results": [
                                    {
                                        "index": item.get("index"),
                                        "aweme_type": item.get("aweme_type"),
                                        "author": item.get("author"),
                                        "comments": item.get("comments"),
                                        "description": str(item.get("description") or "")[:160],
                                        "url": item.get("url"),
                                    }
                                    for item in items
                                    if isinstance(item, dict)
                                ]
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                if (
                    site == "douyin"
                    and isinstance(items, list)
                    and items
                    and isinstance(items[0], dict)
                ):
                    first = cast(dict[str, object], items[0])
                    video = await session.call_tool("douyin_video", {"url": first.get("url")})
                    _print_result("douyin_video", video)
                    comments = await session.call_tool(
                        "douyin_comments",
                        {"url": first.get("url"), "max_comments": 30},
                    )
                    _print_result("douyin_comments", comments)
            if site in {"douyin-comments"}:
                comments = await session.call_tool(
                    "douyin_comments",
                    {
                        "url": "https://www.douyin.com/video/7478048831087725875",
                        "max_comments": 30,
                    },
                )
                _print_result("douyin_comments", comments)
            if site in {"douyin-first"}:
                first_url = "https://www.douyin.com/video/7478048831087725875"
                download = await session.call_tool(
                    "douyin_download",
                    {
                        "url": first_url,
                        "media": "video",
                        "max_file_mb": 256,
                    },
                )
                _print_result("douyin_download", download)
                comments = await session.call_tool(
                    "douyin_comments",
                    {"url": first_url, "max_comments": 100},
                )
                comment_data = _print_result("douyin_comments", comments)
                comment_items = comment_data.get("items")
                if isinstance(comment_items, list):
                    print(
                        json.dumps(
                            {
                                "comments": [
                                    {
                                        "index": item.get("index"),
                                        "depth": item.get("depth"),
                                        "author": item.get("author"),
                                        "reply_to": item.get("reply_to"),
                                        "text": item.get("text"),
                                        "likes": item.get("likes"),
                                        "ip_location": item.get("ip_location"),
                                    }
                                    for item in comment_items
                                    if isinstance(item, dict)
                                ]
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            if site in {"download"}:
                with TemporaryDirectory(prefix="browser-mcp-media-smoke-") as temporary:
                    destination = Path(temporary)
                    xhs_search = await session.call_tool(
                        "xhs_search",
                        {"keyword": "牵手 APP", "page": 1},
                    )
                    xhs_data = _print_result("xhs_search", xhs_search)
                    xhs_items = xhs_data.get("items")
                    if isinstance(xhs_items, list) and xhs_items:
                        first_xhs = cast(dict[str, object], xhs_items[0])
                        xhs_download = await session.call_tool(
                            "xhs_download",
                            {
                                "url": first_xhs.get("url"),
                                "media": "all",
                                "output_dir": str(destination / "xhs"),
                                "max_file_mb": 256,
                            },
                        )
                        _print_result("xhs_download", xhs_download)
                    douyin_search = await session.call_tool(
                        "douyin_search",
                        {"keyword": "牵手 APP", "limit": 5},
                    )
                    douyin_data = _print_result("douyin_search", douyin_search)
                    douyin_items = douyin_data.get("items")
                    if isinstance(douyin_items, list) and douyin_items:
                        first_douyin = cast(dict[str, object], douyin_items[0])
                        douyin_download = await session.call_tool(
                            "douyin_download",
                            {
                                "url": first_douyin.get("url"),
                                "media": "all",
                                "output_dir": str(destination / "douyin"),
                                "max_file_mb": 256,
                            },
                        )
                        _print_result("douyin_download", douyin_download)
            if site in {"download-matrix", "download-douyin-note"}:
                with TemporaryDirectory(prefix="browser-mcp-media-matrix-") as temporary:
                    destination = Path(temporary)
                    if site == "download-matrix":
                        xhs_video = await session.call_tool(
                            "xhs_download",
                            {
                                "url": (
                                    "https://www.xiaohongshu.com/explore/6a8186ae00000000270212e7"
                                    "?xsec_token=ABqw20tRrnooUenYYL0PhAaRVnqh7YayBnvlZY0MYxTw8%3D"
                                    "&xsec_source=pc_search"
                                ),
                                "media": "video",
                                "output_dir": str(destination / "xhs-video"),
                                "max_file_mb": 256,
                            },
                        )
                        _print_result("xhs_download[video]", xhs_video)
                    douyin_search = await session.call_tool(
                        "douyin_search",
                        {"keyword": "旅行攻略 图文", "limit": 20},
                    )
                    douyin_data = _print_result("douyin_search[note]", douyin_search)
                    douyin_items = douyin_data.get("items")
                    note_item = next(
                        (
                            item
                            for item in douyin_items
                            if isinstance(item, dict) and item.get("aweme_type") == "note"
                        ),
                        None,
                    ) if isinstance(douyin_items, list) else None
                    if isinstance(note_item, dict):
                        douyin_images = await session.call_tool(
                            "douyin_download",
                            {
                                "url": note_item.get("url"),
                                "media": "images",
                                "output_dir": str(destination / "douyin-images"),
                                "max_file_mb": 256,
                            },
                        )
                        _print_result("douyin_download[images]", douyin_images)
                    else:
                        print(json.dumps({"tool": "douyin_download[images]", "skipped": True}))
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
                            "?browser-mcp-smoke=0.9.0"
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
