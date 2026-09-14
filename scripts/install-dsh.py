#!/usr/bin/env python3
"""Register Browser MCP as a DeepSeek Harness (DSH) global MCP server."""

from __future__ import annotations

import argparse
import os
import shutil
import sys

PLUGIN_ID = "mcp-browser"
PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client"
SERVER_NAME = "browser"
PATCH_FILENAME = "cordis.patch.yml"


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Register Browser MCP with DeepSeek Harness.",
    )
    parser.add_argument("--profile", default=os.environ.get("DSH_PROFILE", "web"))
    parser.add_argument("--uv", default=os.environ.get("UV_BIN", ""))
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("BROWSER_MCP_DIR", ""),
        help="browser-mcp 项目根目录（含 src/ 与 pyproject.toml）。",
    )
    return parser.parse_args()


def find_uv(explicit: str) -> str:
    """Locate the uv executable, or exit with an error.

    Prefers an explicitly passed path, then the ``uv`` / ``uvx`` on PATH,
    then a few well-known install locations.
    """
    if explicit:
        return explicit
    for name in ("uv", "uvx"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        os.path.expanduser("~/.local/bin/uv"),
        "/usr/local/bin/uv",
        "/opt/homebrew/bin/uv",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    sys.exit("找不到 uv；请用 --uv <path> 指定，或先安装 uv（pip install uv）。")


def find_project_dir(explicit: str) -> str:
    """Locate the browser-mcp project root directory."""
    if explicit and not os.path.isdir(explicit):
        sys.exit(f"--project-dir 不存在: {explicit}")
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (here, os.path.dirname(here)):
        if os.path.isfile(os.path.join(candidate, "pyproject.toml")) or os.path.isdir(
            os.path.join(candidate, "src"),
        ):
            return candidate
    sys.exit("无法确定 browser-mcp 项目根；请用 --project-dir 指定。")


def find_profile(profile: str) -> str:
    """Resolve the absolute path of a DSH profile's ``cordis.patch.yml``."""
    home = os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")
    profile_dir = os.path.join(home, "profiles", profile)
    if not os.path.isdir(profile_dir):
        sys.exit(f"DSH profile 不存在: {profile_dir}（可用 --profile <name> 指定或先创建）")
    return os.path.join(profile_dir, PATCH_FILENAME)


def main() -> None:
    """Write the browser-mcp plugin entry into the profile patch file."""
    args = parse_args()
    uv = find_uv(args.uv)
    project = find_project_dir(args.project_dir)
    patch_file = find_profile(args.profile)

    if not os.path.isfile(patch_file):
        sys.exit(f"缺少 {patch_file}；请先让 DSH 初始化该 profile。")

    with open(patch_file, encoding="utf-8") as handle:
        content = handle.read()

    if PLUGIN_ID in content:
        print(f"已配置 (id={PLUGIN_ID} 存在于 {patch_file})，跳过。")
        return

    block_lines = [
        "# Browser MCP (ywleeo/browser-mcp): 通过真实 Chrome 读取/操作网页。",
        "# 工具以 mcp__browser__* 名字出现；首次需在 chrome://extensions 加载扩展。",
        "- insert:",
        f"    - id: {PLUGIN_ID}",
        f"      name: '{PLUGIN_NAME}'",
        "      config:",
        f"        serverName: {SERVER_NAME}",
        "        transport: stdio",
        f'        command: "{uv}"',
        "        args:",
        "          - --directory",
        f'          - "{project}"',
        "          - run",
        "          - browser-mcp",
        "        failOnStartupError: false",
    ]

    stripped = content.strip()
    body_lines = [
        line for line in stripped.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    is_empty_doc = body_lines == ["[]"]

    if is_empty_doc:
        header = "\n".join(
            [line for line in stripped.splitlines() if line.strip().startswith("#")],
        )
        new_content = (header + "\n" if header else "") + "\n".join(block_lines) + "\n"
    else:
        new_content = content.rstrip("\n") + "\n" + "\n".join(block_lines) + "\n"

    with open(patch_file, "w", encoding="utf-8") as handle:
        handle.write(new_content)

    print(f"已写入 {patch_file}。")
    print("重启会话或由 DSH 热重载后生效；工具将出现为 mcp__browser__* 。")


if __name__ == "__main__":
    main()
