"""Process-owner watchdog for stdio MCP servers launched through wrappers such as uv."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Final

LOGGER = logging.getLogger(__name__)
OWNER_PID_ENV: Final = "BROWSER_MCP_OWNER_PID"
OWNER_CHECK_INTERVAL_SECONDS: Final = 2.0


def resolve_owner_pid() -> int | None:
    """Resolve the MCP host PID, skipping an intermediate uv launcher on POSIX."""
    configured = os.getenv(OWNER_PID_ENV)
    if configured is not None:
        return _parse_owner_pid(configured)

    parent_pid = os.getppid()
    if parent_pid <= 1:
        return None
    if os.name == "posix" and _process_name(parent_pid) == "uv":
        launcher_parent = _process_parent_pid(parent_pid)
        if launcher_parent is not None and launcher_parent > 1:
            return launcher_parent
    return parent_pid


def start_owner_watchdog(owner_pid: int | None = None) -> threading.Thread | None:
    """Terminate this server if its resolved MCP host disappears without closing stdio."""
    resolved_owner = resolve_owner_pid() if owner_pid is None else owner_pid
    server_pid = os.getpid()
    if resolved_owner is None or resolved_owner in {1, server_pid}:
        return None
    thread = threading.Thread(
        target=_watch_owner,
        args=(resolved_owner, server_pid),
        name="browser-mcp-owner-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def _watch_owner(owner_pid: int, server_pid: int) -> None:
    """Poll one immutable owner PID and signal this process after it disappears."""
    while _process_exists(owner_pid):
        time.sleep(OWNER_CHECK_INTERVAL_SECONDS)
    LOGGER.warning("process.owner_gone owner_pid=%s; stopping Browser MCP", owner_pid)
    try:
        os.kill(server_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _process_exists(pid: int) -> bool:
    """Return whether a PID still exists without requiring permission to signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_name(pid: int) -> str:
    """Read a POSIX process executable basename for wrapper detection."""
    completed = _run_ps("comm=", pid)
    if completed is None:
        return ""
    command = completed.stdout.strip().splitlines()
    return Path(command[0]).name if command else ""


def _process_parent_pid(pid: int) -> int | None:
    """Read one POSIX process parent PID without adding a platform dependency."""
    completed = _run_ps("ppid=", pid)
    if completed is None:
        return None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def _run_ps(field: str, pid: int) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded, shell-free ps lookup and suppress unavailable-platform failures."""
    try:
        completed = subprocess.run(
            ["ps", "-o", field, "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed if completed.returncode == 0 else None


def _parse_owner_pid(raw: str) -> int | None:
    """Parse an explicit owner PID while allowing zero to disable the watchdog."""
    try:
        owner_pid = int(raw)
    except ValueError as error:
        raise ValueError(f"{OWNER_PID_ENV} must be an integer") from error
    if owner_pid == 0:
        return None
    if owner_pid < 2:
        raise ValueError(f"{OWNER_PID_ENV} must be zero or at least 2")
    return owner_pid
