"""Tests for environment-backed application settings."""

import logging
from pathlib import Path

import pytest

from browser_mcp.config import AppSettings, discover_project_root


def test_source_checkout_uses_visible_project_extension_directory() -> None:
    """Local GitHub development should expose one predictable unpacked-extension path."""
    project_root = discover_project_root()

    assert project_root is not None
    assert AppSettings().extension_dir == project_root / "extension"


def test_custom_data_directory_keeps_an_isolated_extension_default(tmp_path: Path) -> None:
    """Tests and packaged overrides should not write pairing files into the source tree."""
    assert AppSettings(data_dir=tmp_path).extension_dir == tmp_path / "extension"


def test_settings_accept_valid_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Supported environment values should produce typed immutable settings."""
    monkeypatch.setenv("BROWSER_MCP_BRIDGE_PORT", "19000")
    monkeypatch.setenv("BROWSER_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BROWSER_MCP_EXTENSION_DIR", str(tmp_path / "browser-extension"))
    monkeypatch.setenv("BROWSER_MCP_LOG_LEVEL", "debug")

    settings = AppSettings.from_env()

    assert settings.bridge_port == 19_000
    assert settings.data_dir == tmp_path
    assert settings.extension_dir == tmp_path / "browser-extension"
    assert settings.log_level == logging.DEBUG


def test_settings_reject_relative_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime data must never depend on a client's arbitrary working directory."""
    monkeypatch.setenv("BROWSER_MCP_DATA_DIR", "relative/runtime")

    with pytest.raises(ValueError, match="absolute path"):
        AppSettings.from_env()


def test_settings_reject_relative_extension_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chrome load paths must be stable absolute locations."""
    monkeypatch.setenv("BROWSER_MCP_EXTENSION_DIR", "relative/extension")

    with pytest.raises(ValueError, match="absolute path"):
        AppSettings.from_env()


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_settings_reject_invalid_ports(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Invalid ports must fail before the MCP transport starts."""
    monkeypatch.setenv("BROWSER_MCP_BRIDGE_PORT", value)

    with pytest.raises(ValueError, match="BROWSER_MCP_BRIDGE_PORT"):
        AppSettings.from_env()
