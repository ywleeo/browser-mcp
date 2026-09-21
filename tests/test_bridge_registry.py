"""Tests for bridge port leases, idle retirement, and last-resort reclamation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from browser_mcp.bridge.manager import BridgeManager
from browser_mcp.bridge.registry import PortLease, PortRegistry
from browser_mcp.config import AppSettings
from browser_mcp.process_lifecycle import OWNER_PID_ENV, process_start_time
from tests.helpers import allow_public_url_policy, reserve_free_port

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_lease(
    registry_dir: Path,
    *,
    port: int,
    pid: int,
    pid_start_time: str | None = None,
    owner_pid: int | None = None,
    idle_seconds: float = 0.0,
) -> PortLease:
    """Store one lease directly, as another server process would have left it behind."""
    moment = datetime.now(UTC) - timedelta(seconds=idle_seconds)
    lease = PortLease(
        port=port,
        pid=pid,
        pid_start_time=process_start_time(pid) if pid_start_time is None else pid_start_time,
        owner_pid=owner_pid,
        started_at=moment,
        last_activity_at=moment,
        server_version="0.0.0-test",
    )
    directory = registry_dir / "bridge-ports"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"bridge-{port}.json").write_text(json.dumps(lease.to_payload()), encoding="utf-8")
    return lease


async def spawn_idle_process() -> asyncio.subprocess.Process:
    """Start a throwaway child that stands in for another server process."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def stop(process: asyncio.subprocess.Process) -> None:
    """Terminate one fixture process without failing on an already-dead child."""
    if process.returncode is None:
        process.kill()
        await process.wait()


async def wait_for_listener(port: int, timeout: float = 6.0) -> None:
    """Wait until something is accepting connections on one localhost port."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"nothing bound port {port}")


def port_is_free(port: int) -> bool:
    """Return whether one localhost port can be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def unused_pid() -> int:
    """Return a PID that is guaranteed not to be running in this session."""
    for candidate in range(90_000, 99_999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise RuntimeError("no free PID for the crashed-server fixture")


def test_lease_survives_a_storage_round_trip(tmp_path: Path) -> None:
    """A stored lease must come back with every field a reclaim decision depends on."""
    registry = PortRegistry(tmp_path)
    claimed = registry.claim(17_880, owner_pid=4_242, server_version="9.9.9")

    restored = PortRegistry(tmp_path).leases()
    assert len(restored) == 1
    assert restored[0] == claimed
    assert restored[0].owner_pid == 4_242
    assert restored[0].pid_start_time == process_start_time(os.getpid())
    assert restored[0].idle_seconds() < 5


def test_release_frees_the_lease(tmp_path: Path) -> None:
    """A cleanly stopped server must not leave a lease behind to be reclaimed later."""
    registry = PortRegistry(tmp_path)
    registry.claim(17_880, owner_pid=None, server_version="9.9.9")
    registry.release()

    assert PortRegistry(tmp_path).leases() == ()
    assert registry.idle_seconds() is None


def test_touch_is_rate_limited_but_keeps_idle_current(tmp_path: Path) -> None:
    """Hot MCP paths must refresh idle time in memory without writing on every call."""
    registry = PortRegistry(tmp_path)
    registry.claim(17_880, owner_pid=None, server_version="9.9.9")
    first_write = registry.leases()[0].last_activity_at

    for _ in range(100):
        registry.touch()

    assert registry.leases()[0].last_activity_at == first_write
    assert registry.idle_seconds() is not None


def test_reclaim_drops_leases_whose_server_is_gone(tmp_path: Path) -> None:
    """A lease left by a crashed server must never keep its port out of the pool."""
    registry = PortRegistry(tmp_path)
    write_lease(tmp_path, port=17_880, pid=unused_pid(), pid_start_time="whenever", idle_seconds=1)

    assert registry.reclaim([17_880], min_idle_seconds=3_600) == (17_880,)
    assert registry.leases() == ()


async def test_reclaim_never_signals_a_pid_the_kernel_reused(tmp_path: Path) -> None:
    """A PID inherited by an unrelated process must lose its lease, not receive a signal."""
    survivor = await spawn_idle_process()
    try:
        registry = PortRegistry(tmp_path, pid=os.getpid())
        write_lease(
            tmp_path,
            port=17_880,
            pid=survivor.pid,
            pid_start_time="Thu Jan  1 00:00:00 1970",
            owner_pid=os.getpid(),
            idle_seconds=10_000,
        )

        assert registry.reclaim([17_880], min_idle_seconds=0) == (17_880,)
        assert registry.leases() == ()
        await asyncio.sleep(0.2)
        assert survivor.returncode is None
    finally:
        await stop(survivor)


async def test_reclaim_spares_a_server_that_was_used_recently(tmp_path: Path) -> None:
    """A session that is still working must keep its port when a new server starts."""
    busy = await spawn_idle_process()
    try:
        registry = PortRegistry(tmp_path, pid=os.getpid())
        write_lease(tmp_path, port=17_880, pid=busy.pid, owner_pid=os.getpid(), idle_seconds=30)

        assert registry.reclaim([17_880], min_idle_seconds=300) == ()
        assert len(registry.leases()) == 1
        assert busy.returncode is None
    finally:
        await stop(busy)


@pytest.mark.skipif(os.name != "posix", reason="reclamation signals POSIX processes")
async def test_reclaim_retires_the_single_coldest_live_server(tmp_path: Path) -> None:
    """Exhaustion must cost exactly one port, taken from the least recently used server."""
    coldest = await spawn_idle_process()
    warmer = await spawn_idle_process()
    try:
        registry = PortRegistry(tmp_path, pid=os.getpid())
        write_lease(
            tmp_path, port=17_880, pid=coldest.pid, owner_pid=os.getpid(), idle_seconds=9_000
        )
        write_lease(tmp_path, port=17_881, pid=warmer.pid, owner_pid=os.getpid(), idle_seconds=600)

        assert registry.reclaim([17_880, 17_881], min_idle_seconds=300) == (17_880,)
        await asyncio.wait_for(coldest.wait(), timeout=5)
        assert warmer.returncode is None
        assert [lease.port for lease in registry.leases()] == [17_881]
    finally:
        await stop(coldest)
        await stop(warmer)


@pytest.mark.skipif(os.name != "posix", reason="reclamation signals POSIX processes")
async def test_reclaim_retires_a_server_whose_host_died(tmp_path: Path) -> None:
    """A server orphaned by a dead MCP host must be reclaimed however recently it was used."""
    orphan = await spawn_idle_process()
    host = await spawn_idle_process()
    host.terminate()
    await host.wait()
    try:
        registry = PortRegistry(tmp_path, pid=os.getpid())
        write_lease(tmp_path, port=17_880, pid=orphan.pid, owner_pid=host.pid, idle_seconds=1)

        assert registry.reclaim([17_880], min_idle_seconds=86_400) == (17_880,)
        await asyncio.wait_for(orphan.wait(), timeout=5)
    finally:
        await stop(orphan)
        await stop(host)


def test_exhaustion_error_names_the_processes_holding_the_pool(tmp_path: Path) -> None:
    """The failure a user actually sees must identify who to quit, not just the port range."""
    port = reserve_free_port()
    manager = BridgeManager(
        AppSettings(
            bridge_port=port,
            bridge_port_pool_size=1,
            data_dir=tmp_path,
            extension_dir=tmp_path / "extension",
        ),
        allow_public_url_policy(),
    )
    write_lease(
        tmp_path, port=port, pid=4_242, pid_start_time="whenever", owner_pid=9_999, idle_seconds=12
    )

    message = manager._pool_exhausted_message()  # pyright: ignore[reportPrivateUsage]
    assert f"{port}-{port}" in message
    assert "pid 4242" in message
    assert "host pid 9999" in message


@pytest.mark.skipif(os.name != "posix", reason="reclamation signals POSIX processes")
async def test_a_starting_server_reclaims_the_port_of_an_unused_one(tmp_path: Path) -> None:
    """A new session must always be able to start, even with the whole pool taken."""
    port = reserve_free_port()
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "browser_mcp",
        cwd=PROJECT_ROOT,
        env=os.environ
        | {
            "BROWSER_MCP_BRIDGE_PORT": str(port),
            "BROWSER_MCP_DATA_DIR": str(tmp_path),
            "BROWSER_MCP_LOG_LEVEL": "CRITICAL",
            "BROWSER_MCP_IDLE_TIMEOUT_SECONDS": "0",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    manager = BridgeManager(
        AppSettings(
            bridge_port=port,
            bridge_port_pool_size=1,
            reclaim_idle_seconds=0,
            idle_timeout_seconds=0,
            data_dir=tmp_path,
            extension_dir=tmp_path / "extension",
        ),
        allow_public_url_policy(),
    )
    try:
        await asyncio.wait_for(wait_for_listener(port), timeout=8)
        assert [lease.pid for lease in PortRegistry(tmp_path).leases()] == [holder.pid]

        await asyncio.wait_for(manager.start(), timeout=15)

        assert (await manager.status()).bridge_port == port
        await asyncio.wait_for(holder.wait(), timeout=5)
        assert [lease.pid for lease in PortRegistry(tmp_path).leases()] == [os.getpid()]
    finally:
        await manager.close()
        await stop(holder)


@pytest.mark.skipif(os.name != "posix", reason="idle retirement signals the POSIX process")
async def test_an_unused_server_retires_and_returns_its_port(tmp_path: Path) -> None:
    """A server nobody calls must exit on its own instead of holding a pooled port forever."""
    port = reserve_free_port()
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "browser_mcp",
        cwd=PROJECT_ROOT,
        env=os.environ
        | {
            "BROWSER_MCP_BRIDGE_PORT": str(port),
            "BROWSER_MCP_DATA_DIR": str(tmp_path),
            "BROWSER_MCP_LOG_LEVEL": "CRITICAL",
            "BROWSER_MCP_IDLE_TIMEOUT_SECONDS": "1",
            OWNER_PID_ENV: "0",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(wait_for_listener(port), timeout=8)
        await asyncio.wait_for(server.wait(), timeout=15)
        assert port_is_free(port)
        assert PortRegistry(tmp_path).leases() == ()
    finally:
        await stop(server)
