"""Release-version consistency checks across package and extension metadata."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

from browser_mcp import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_synchronized() -> None:
    """Python, extension, and lock metadata must expose one release version."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8")
    )
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = cast(list[dict[str, Any]], lock["package"])
    locked_project = next(package for package in packages if package.get("name") == "browser-mcp")

    assert project["project"]["version"] == __version__
    assert manifest["version"] == __version__
    assert locked_project["version"] == __version__
