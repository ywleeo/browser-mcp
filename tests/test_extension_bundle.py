"""Tests for unpacked extension installation and pairing metadata."""

import json
import os
from pathlib import Path
from typing import cast

from browser_mcp.bridge.bundle import ExtensionBundle
from browser_mcp.bridge.manager import BRIDGE_PATH


def test_bundle_is_refreshed_while_pairing_token_stays_stable(tmp_path: Path) -> None:
    """Repeated starts should update code without invalidating the loaded extension."""
    bundle = ExtensionBundle(tmp_path, tmp_path / "extension", 17_880, 10, BRIDGE_PATH)

    first = bundle.ensure_installed()
    second = bundle.ensure_installed()
    pairing = cast(
        dict[str, object],
        json.loads((first.directory / "pairing.json").read_text(encoding="utf-8")),
    )

    assert first.token == second.token
    assert first.build_id == second.build_id
    assert (first.directory / "manifest.json").is_file()
    assert (first.directory / "background.js").is_file()
    assert (first.directory / "content_inject.js").is_file()
    assert (first.directory / "content_bridge.js").is_file()
    assert (first.directory / "douyin_content_inject.js").is_file()
    assert (first.directory / "douyin_content_bridge.js").is_file()
    assert first.build_id in (first.directory / "build-info.js").read_text(encoding="utf-8")
    assert pairing["base_port"] == 17_880
    assert pairing["pool_size"] == 10
    assert pairing["path"] == BRIDGE_PATH
    assert pairing["token"] == first.token
    background = (first.directory / "background.js").read_text(encoding="utf-8")
    assert "const URL_CHECK_TIMEOUT_MS = 20000;" in background
    assert 'message.type === "browser.interact"' in background
    assert "chrome.tabs.captureVisibleTab" in background
    assert "chrome.windows.create" in background
    assert "focused: false" in background
    assert "chrome.tabs.update(tabId, { url, active: true })" not in background
    assert "chrome.tabs.update(tabId, { url })" in background
    assert "refusing to activate an interaction tab in the user's current window" in background
    assert "async function executeDomClick" in background
    assert 'new InputEvent("input"' in background
    assert 'referenceAttribute = "data-browser-mcp-ref"' in background
    assert "crypto.randomUUID().slice(0, 8)" in background
    assert 'document.querySelector(".note-scroller")' in background
    assert 'type: "mouseWheel"' in background
    assert "scrollerRect.left + scrollerRect.width / 2" in background
    assert "wheelDelta * scrollDirection" in background
    assert "if (ui.clicked === 0)" in background
    assert "scroller.scrollTop = scroller.scrollHeight" not in background
    assert "function readXhsNoteRuntimeState" in background
    assert "window.__INITIAL_STATE__?.note?.noteDetailMap" in background
    assert "func: readXhsNoteRuntimeState" in background
    assert 'message.action === "comments"' in background
    assert 'message.type === "douyin.fetch"' in background
    assert "function handleObservedDouyinResponse" in background
    assert "/general\\/search\\/stream" in background
    assert "function runDouyinComments" in background
    assert "Douyin rendered comment stream was not found" in background

    if os.name != "nt":
        token_mode = (tmp_path / "pairing-token").stat().st_mode & 0o777
        pairing_mode = (first.directory / "pairing.json").stat().st_mode & 0o777
        assert token_mode == 0o600
        assert pairing_mode == 0o600
