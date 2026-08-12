"""Environment-backed process configuration."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

DEFAULT_BRIDGE_PORT = 17_880
DEFAULT_BRIDGE_PORT_POOL_SIZE = 10
DEFAULT_DATA_DIR = user_data_path("browser-mcp", appauthor=False)
LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Immutable settings shared by the MCP and future bridge layers."""

    bridge_port: int = DEFAULT_BRIDGE_PORT
    bridge_port_pool_size: int = DEFAULT_BRIDGE_PORT_POOL_SIZE
    data_dir: Path = DEFAULT_DATA_DIR
    extension_dir: Path = Path()
    log_level: int = logging.INFO

    def __post_init__(self) -> None:
        """Reject invalid direct construction as strictly as environment parsing."""
        if not 1 <= self.bridge_port <= 65_535:
            raise ValueError("bridge_port must be between 1 and 65535")
        if not 1 <= self.bridge_port_pool_size <= 100:
            raise ValueError("bridge_port_pool_size must be between 1 and 100")
        if self.bridge_port + self.bridge_port_pool_size - 1 > 65_535:
            raise ValueError("bridge port pool must end at or below 65535")
        if not self.data_dir.is_absolute():
            raise ValueError("data_dir must be an absolute path")
        extension_dir = self.extension_dir
        if extension_dir == Path():
            extension_dir = (
                self.data_dir / "extension"
                if self.data_dir != DEFAULT_DATA_DIR
                else _default_extension_dir()
            )
            object.__setattr__(self, "extension_dir", extension_dir)
        if not extension_dir.is_absolute():
            raise ValueError("extension_dir must be an absolute path")

    @property
    def bridge_ports(self) -> tuple[int, ...]:
        """Return every valid port in the configured contiguous pool."""
        return tuple(range(self.bridge_port, self.bridge_port + self.bridge_port_pool_size))

    @property
    def bridge_port_range(self) -> tuple[int, int]:
        """Return the inclusive configured bridge port range."""
        return self.bridge_ports[0], self.bridge_ports[-1]

    @classmethod
    def from_env(cls) -> AppSettings:
        """Build validated settings from Browser MCP environment variables."""
        bridge_port = _parse_port(os.getenv("BROWSER_MCP_BRIDGE_PORT"))
        data_dir = _parse_data_dir(os.getenv("BROWSER_MCP_DATA_DIR"))
        extension_dir = _parse_extension_dir(os.getenv("BROWSER_MCP_EXTENSION_DIR"))
        log_level = _parse_log_level(os.getenv("BROWSER_MCP_LOG_LEVEL"))
        return cls(
            bridge_port=bridge_port,
            data_dir=data_dir,
            extension_dir=extension_dir,
            log_level=log_level,
        )


def discover_project_root() -> Path | None:
    """Find a Browser MCP source checkout without depending on the process working directory."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "browser_mcp"
        ).is_dir():
            return candidate
    return None


def _default_extension_dir() -> Path:
    """Prefer the visible project extension directory and fall back for wheel installs."""
    project_root = discover_project_root()
    if project_root is not None:
        return project_root / "extension"
    return DEFAULT_DATA_DIR / "extension"


def _parse_port(raw: str | None) -> int:
    """Parse the optional bridge port while rejecting invalid values early."""
    if raw is None:
        return DEFAULT_BRIDGE_PORT
    try:
        port = int(raw)
    except ValueError as error:
        raise ValueError("BROWSER_MCP_BRIDGE_PORT must be an integer") from error
    if not 1 <= port <= 65_535:
        raise ValueError("BROWSER_MCP_BRIDGE_PORT must be between 1 and 65535")
    return port


def _parse_log_level(raw: str | None) -> int:
    """Translate a standard log-level name into its numeric value."""
    if raw is None:
        return logging.INFO
    value = LOG_LEVELS.get(raw.upper())
    if value is None:
        raise ValueError(f"unsupported BROWSER_MCP_LOG_LEVEL: {raw}")
    return value


def _parse_data_dir(raw: str | None) -> Path:
    """Resolve an optional data-directory override without creating it yet."""
    if raw is None:
        return DEFAULT_DATA_DIR
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError("BROWSER_MCP_DATA_DIR must be an absolute path")
    return expanded


def _parse_extension_dir(raw: str | None) -> Path:
    """Resolve an optional unpacked-extension override to an absolute path."""
    if raw is None:
        return Path()
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError("BROWSER_MCP_EXTENSION_DIR must be an absolute path")
    return expanded
