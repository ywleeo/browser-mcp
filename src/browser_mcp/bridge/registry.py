"""Cross-process bridge port leases and reclamation of servers nobody is using.

The bridge port pool is small, and an MCP host keeps its server alive for as long
as the host process lives -- even when that session stopped using Browser MCP
hours ago. The owner watchdog in :mod:`browser_mcp.process_lifecycle` only covers
hosts that disappear, so idle-but-alive servers used to hold the pool until it was
exhausted and no new session could start at all.

Every server therefore records a lease for the port it bound, refreshes it on real
MCP activity, retires itself once it has been idle long enough, and -- as the last
checkpoint, when a starting server finds every port taken -- reclaims the least
recently used lease so a fresh session can always come up.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from browser_mcp.process_lifecycle import process_is_running, process_start_time

LOGGER = logging.getLogger(__name__)
LEASE_DIR_NAME: Final = "bridge-ports"
LEASE_PREFIX: Final = "bridge-"
LEASE_SUFFIX: Final = ".json"
TOUCH_INTERVAL_SECONDS: Final = 5.0
TERMINATION_TIMEOUT_SECONDS: Final = 3.0
TERMINATION_POLL_SECONDS: Final = 0.05


@dataclass(frozen=True, slots=True)
class PortLease:
    """One server process's recorded claim on a single bridge port."""

    port: int
    pid: int
    pid_start_time: str
    owner_pid: int | None
    started_at: datetime
    last_activity_at: datetime
    server_version: str

    def idle_seconds(self, now: datetime | None = None) -> float:
        """Return seconds since the owning server last served a real MCP request."""
        moment = now if now is not None else datetime.now(UTC)
        return max((moment - self.last_activity_at).total_seconds(), 0.0)

    def to_payload(self) -> dict[str, Any]:
        """Render one lease as the JSON object stored on disk."""
        return {
            "port": self.port,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "owner_pid": self.owner_pid,
            "started_at": self.started_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "server_version": self.server_version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PortLease:
        """Parse one stored lease, rejecting anything that is not fully usable."""
        owner_raw = payload.get("owner_pid")
        return cls(
            port=int(cast(int, payload["port"])),
            pid=int(cast(int, payload["pid"])),
            pid_start_time=str(payload.get("pid_start_time", "")),
            owner_pid=None if owner_raw is None else int(cast(int, owner_raw)),
            started_at=_parse_moment(cast(str, payload["started_at"])),
            last_activity_at=_parse_moment(cast(str, payload["last_activity_at"])),
            server_version=str(payload.get("server_version", "unknown")),
        )


class PortRegistry:
    """Own this process's lease file and arbitrate reclamation across servers."""

    def __init__(self, directory: Path, *, pid: int | None = None) -> None:
        """Bind the registry to one lease directory on behalf of one server process."""
        self._directory = directory / LEASE_DIR_NAME
        self._pid = os.getpid() if pid is None else pid
        self._pid_start_time = process_start_time(self._pid)
        self._lease: PortLease | None = None
        self._last_written = 0.0

    @property
    def lease(self) -> PortLease | None:
        """Return the lease this process currently holds, if it bound a port."""
        return self._lease

    def claim(self, port: int, *, owner_pid: int | None, server_version: str) -> PortLease:
        """Record this process as the owner of one freshly bound bridge port."""
        now = datetime.now(UTC)
        lease = PortLease(
            port=port,
            pid=self._pid,
            pid_start_time=self._pid_start_time,
            owner_pid=owner_pid,
            started_at=now,
            last_activity_at=now,
            server_version=server_version,
        )
        self._lease = lease
        self._write(lease)
        return lease

    def touch(self) -> None:
        """Mark real MCP activity, rate limited so hot paths never thrash the disk."""
        lease = self._lease
        if lease is None:
            return
        moment = time.monotonic()
        if moment - self._last_written < TOUCH_INTERVAL_SECONDS:
            return
        refreshed = PortLease(
            port=lease.port,
            pid=lease.pid,
            pid_start_time=lease.pid_start_time,
            owner_pid=lease.owner_pid,
            started_at=lease.started_at,
            last_activity_at=datetime.now(UTC),
            server_version=lease.server_version,
        )
        self._lease = refreshed
        self._write(refreshed)

    def release(self) -> None:
        """Drop this process's lease so the port is immediately reusable."""
        lease = self._lease
        self._lease = None
        if lease is not None:
            self._remove(lease.port)

    def idle_seconds(self) -> float | None:
        """Return how long this server has gone without real MCP activity."""
        lease = self._lease
        return None if lease is None else lease.idle_seconds()

    def leases(self, ports: Iterable[int] | None = None) -> tuple[PortLease, ...]:
        """Return every readable lease, optionally restricted to a port pool."""
        wanted = None if ports is None else set(ports)
        found: list[PortLease] = []
        try:
            entries = sorted(self._directory.glob(f"{LEASE_PREFIX}*{LEASE_SUFFIX}"))
        except OSError:
            return ()
        for path in entries:
            lease = self._read(path)
            if lease is None or (wanted is not None and lease.port not in wanted):
                continue
            found.append(lease)
        return tuple(found)

    def reclaim(self, ports: Sequence[int], *, min_idle_seconds: float) -> tuple[int, ...]:
        """Free ports held by dead or long-idle servers and report what was released.

        Leases whose server process is gone, or whose MCP host died without the
        watchdog retiring the server, are always reclaimed. Beyond those, at most
        one live server is retired per call: the single least recently used one,
        and only once it has been idle past the caller's threshold.
        """
        released: list[int] = []
        live: list[PortLease] = []
        for lease in self.leases(ports):
            if lease.pid == self._pid:
                continue
            if not _is_recorded_server(lease):
                LOGGER.info("bridge.reclaim_stale port=%s pid=%s", lease.port, lease.pid)
                self._remove(lease.port)
                released.append(lease.port)
            elif _owner_is_gone(lease):
                if self._terminate(lease, reason="owner_gone"):
                    released.append(lease.port)
            else:
                live.append(lease)

        if min_idle_seconds >= 0 and live:
            now = datetime.now(UTC)
            coldest = max(live, key=lambda candidate: candidate.idle_seconds(now))
            if coldest.idle_seconds(now) >= min_idle_seconds and self._terminate(
                coldest, reason="idle"
            ):
                released.append(coldest.port)
        return tuple(sorted(set(released)))

    def describe(self, ports: Sequence[int]) -> str:
        """Summarize who holds each pooled port, for an actionable exhaustion error."""
        now = datetime.now(UTC)
        held = {lease.port: lease for lease in self.leases(ports)}
        lines: list[str] = []
        for port in ports:
            lease = held.get(port)
            if lease is None:
                lines.append(f"  {port}: held by an unknown process")
            else:
                lines.append(
                    f"  {port}: pid {lease.pid}"
                    f" (host pid {lease.owner_pid if lease.owner_pid is not None else '?'},"
                    f" idle {lease.idle_seconds(now):.0f}s)"
                )
        return "\n".join(lines)

    def _terminate(self, lease: PortLease, *, reason: str) -> bool:
        """Ask one server to stop and confirm its process is gone before claiming its port."""
        LOGGER.info(
            "bridge.reclaim port=%s pid=%s reason=%s idle=%.0fs",
            lease.port,
            lease.pid,
            reason,
            lease.idle_seconds(),
        )
        try:
            os.kill(lease.pid, signal.SIGTERM)
        except OSError:
            self._remove(lease.port)
            return True
        deadline = time.monotonic() + TERMINATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not process_is_running(lease.pid):
                self._remove(lease.port)
                return True
            time.sleep(TERMINATION_POLL_SECONDS)
        LOGGER.warning("bridge.reclaim_timeout port=%s pid=%s", lease.port, lease.pid)
        return False

    def _path_for(self, port: int) -> Path:
        """Return the stable lease path for one pooled port."""
        return self._directory / f"{LEASE_PREFIX}{port}{LEASE_SUFFIX}"

    def _read(self, path: Path) -> PortLease | None:
        """Parse one lease file, treating any damaged record as absent."""
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        try:
            return PortLease.from_payload(cast(dict[str, Any], value))
        except (KeyError, TypeError, ValueError):
            return None

    def _write(self, lease: PortLease) -> None:
        """Replace one lease file atomically so readers never see a partial record."""
        path = self._path_for(lease.port)
        temporary = path.with_name(f"{path.name}.{self._pid}.tmp")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(lease.to_payload(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            LOGGER.warning("bridge.lease_write_failed port=%s error=%s", lease.port, error)
            temporary.unlink(missing_ok=True)
            return
        self._last_written = time.monotonic()

    def _remove(self, port: int) -> None:
        """Delete one lease file without failing on a concurrent removal."""
        try:
            self._path_for(port).unlink(missing_ok=True)
        except OSError as error:
            LOGGER.warning("bridge.lease_remove_failed port=%s error=%s", port, error)


def _is_recorded_server(lease: PortLease) -> bool:
    """Return whether the leased PID is still the very process that wrote the lease.

    Anything less certain -- a vanished PID, a PID the kernel has since handed to an
    unrelated process, or a start time this platform will not report -- is treated as
    stale. The lease is then dropped without signalling anyone, because reclaiming a
    port is never worth the risk of terminating a process that merely inherited a PID.
    """
    if not process_is_running(lease.pid) or not lease.pid_start_time:
        return False
    return process_start_time(lease.pid) == lease.pid_start_time


def _owner_is_gone(lease: PortLease) -> bool:
    """Return whether the MCP host that launched a still-running server has exited."""
    owner_pid = lease.owner_pid
    return owner_pid is not None and not process_is_running(owner_pid)


def _parse_moment(raw: str) -> datetime:
    """Parse one stored timestamp into an aware UTC datetime."""
    moment = datetime.fromisoformat(raw)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
