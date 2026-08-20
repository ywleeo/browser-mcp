"""Tests for abnormal MCP-host exit detection and process cleanup."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

from browser_mcp import process_lifecycle
from browser_mcp.process_lifecycle import OWNER_PID_ENV, resolve_owner_pid
from tests.helpers import reserve_free_port

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _wait_for_listener(port: int) -> None:
    """Wait until one subprocess has bound its localhost bridge listener."""
    for _attempt in range(50):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        del reader
        return
    raise TimeoutError(f"bridge listener did not bind port {port}")


def test_explicit_owner_pid_can_override_or_disable_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP launcher should be able to provide an exact owner or disable monitoring."""
    monkeypatch.setenv(OWNER_PID_ENV, "4242")
    assert resolve_owner_pid() == 4242

    monkeypatch.setenv(OWNER_PID_ENV, "0")
    assert resolve_owner_pid() is None

    monkeypatch.setenv(OWNER_PID_ENV, "invalid")
    with pytest.raises(ValueError, match="must be an integer"):
        resolve_owner_pid()


@pytest.mark.skipif(os.name != "posix", reason="uv ancestor discovery uses POSIX ps")
def test_owner_resolution_skips_uv_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog should track the MCP host above uv, not the uv wrapper itself."""

    def process_name(pid: int) -> str:
        """Return uv for the deterministic direct parent fixture."""
        assert pid == 200
        return "uv"

    def process_parent_pid(pid: int) -> int:
        """Return the deterministic MCP host above the uv fixture."""
        assert pid == 200
        return 100

    monkeypatch.delenv(OWNER_PID_ENV, raising=False)
    monkeypatch.setattr(process_lifecycle.os, "getppid", lambda: 200)
    monkeypatch.setattr(process_lifecycle, "_process_name", process_name)
    monkeypatch.setattr(process_lifecycle, "_process_parent_pid", process_parent_pid)

    assert resolve_owner_pid() == 100


@pytest.mark.asyncio
async def test_missing_owner_watchdog_releases_port_even_while_stdin_remains_open(
    tmp_path: Path,
) -> None:
    """A vanished MCP host must terminate the server without relying on stdio EOF."""
    bridge_port = reserve_free_port()
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    environment = os.environ | {
        "BROWSER_MCP_BRIDGE_PORT": str(bridge_port),
        "BROWSER_MCP_DATA_DIR": str(tmp_path),
        "BROWSER_MCP_LOG_LEVEL": "CRITICAL",
        OWNER_PID_ENV: str(owner.pid),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "browser_mcp",
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(_wait_for_listener(bridge_port), timeout=4)
        owner.terminate()
        await owner.wait()
        await asyncio.wait_for(process.wait(), timeout=6)
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()
        if process.returncode is None:
            process.kill()
            await process.wait()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", bridge_port))
