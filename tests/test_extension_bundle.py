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
    assert (first.directory / "comment_sessions.js").is_file()
    assert first.build_id in (first.directory / "build-info.js").read_text(encoding="utf-8")
    assert pairing["base_port"] == 17_880
    assert pairing["pool_size"] == 10
    assert pairing["path"] == BRIDGE_PATH
    assert pairing["token"] == first.token
    background = (first.directory / "background.js").read_text(encoding="utf-8")
    assert "const URL_CHECK_TIMEOUT_MS = 20000;" in background
    assert 'message.type === "browser.interact"' in background
    manifest = json.loads((first.directory / "manifest.json").read_text(encoding="utf-8"))
    assert "webNavigation" in manifest["permissions"]
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Page.captureScreenshot"' in background
    assert "captureBeyondViewport: false" in background
    assert "chrome.windows.create" in background
    assert "focused: false" in background
    assert "chrome.tabs.update(tabId, { url, active: true })" not in background
    assert "chrome.tabs.update(tabId, { url })" in background
    assert "chrome.tabs.captureVisibleTab" not in background
    assert "async function executeTrustedClick" in background
    click_implementation = background.split("async function executeTrustedClick", 1)[1].split(
        "/** Apply one bounded keyboard behavior", 1
    )[0]
    assert ".click()" not in click_implementation
    assert "await dispatchTrustedPointerMove(debuggerTarget, clickPoint)" in click_implementation
    assert "await readInteractionHoverNode(debuggerTarget, clickPoint)" in click_implementation
    assert (
        "await dispatchTrustedPointClick(debuggerTarget, clickPoint, false)"
        in click_implementation
    )
    assert "DOM.resolveNode" not in click_implementation
    assert "querySelector" not in click_implementation
    assert 'chrome.debugger.sendCommand(debuggerTarget, "DOM.getNodeForLocation"' in background
    assert "async function readInteractionHoverNode" in background
    assert "async function captureVisualClickState" in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Page.getLayoutMetrics"' in background
    assert (
        'if (action !== "click" && action !== "dialog") '
        "await removeForeignExtensionFrames(tabId);"
        in background
    )
    assert "async function focusManagedInteractionWindow" in background
    assert 'chrome.windows.update(managedWindowId, { focused: true })' in background
    assert "async function restoreInteractionWindowFocus" in background
    assert 'method === "Page.javascriptDialogOpening"' in background
    assert 'method === "Page.javascriptDialogClosed"' in background
    assert '"Page.handleJavaScriptDialog"' in background
    assert "async function executeNativeDialog" in background
    assert "function interactionDialogVisualState" in background
    assert "function isStaleInteractionReferenceError" in background
    assert "click skipped and visual state refreshed" in background
    assert "targets: visual.clickTargets" in background
    assert "elements: visual.state.elements" in background
    assert (
        "debuggerSession = await interactionDebuggerSession(state, session, tabId);" in background
    )
    assert "async function executeInteractionFrames" in background
    assert "chrome.webNavigation.getAllFrames({ tabId })" in background
    assert "target: { tabId, frameIds: [frame.frameId] }" in background
    assert "chrome-extension|moz-extension|safari-web-extension" in background
    assert "function isForeignExtensionFrameError" in background
    assert "async function removeForeignExtensionFrames" in background
    assert 'root.querySelectorAll("iframe[src],frame[src]")' in background
    assert "Child frames were skipped because a browser extension owns" in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Page.getFrameTree"' in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Page.createIsolatedWorld"' in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "DOM.describeNode"' in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Input.insertText"' in background
    assert 'chrome.debugger.sendCommand(debuggerTarget, "Input.dispatchKeyEvent"' in background
    assert 'String(args.coordinate_space || "screenshot")' in background
    assert "const scaleDifference = Math.abs(scaleX - scaleY)" in background
    assert "createImageBitmap(blob)" in background
    assert 'referenceAttribute = "data-browser-mcp-ref"' in background
    assert "crypto.randomUUID().slice(0, 8)" in background
    assert 'document.querySelector(".note-scroller")' in background
    assert 'type: "mouseWheel"' in background
    assert "scrollerRect.left + scrollerRect.width / 2" in background
    assert "wheelDelta * loop.scrollDirection" in background
    assert "const DEFAULT_COMMENT_BUDGET_MS = 40000;" in background
    assert 'from "./comment_sessions.js"' in background
    sessions = (first.directory / "comment_sessions.js").read_text(encoding="utf-8")
    assert "export function findCommentSession" in sessions
    assert "export async function suspendCommentSession" in sessions
    assert "if (ui.clicked === 0)" in background
    assert "scroller.scrollTop = scroller.scrollHeight" not in background
    assert "function readXhsNoteRuntimeState" in background
    assert "window.__INITIAL_STATE__?.note?.noteDetailMap" in background
    assert "func: readXhsNoteRuntimeState" in background
    assert 'message.action === "comments"' in background
    assert 'message.type === "douyin.fetch"' in background
    assert 'message.type === "bilibili.fetch"' in background
    assert "async function runBilibiliSearch" in background
    assert "async function readBilibiliSearchPageTab" in background
    assert "Bilibili rendered search page exposed no video cards" in background
    assert "async function runBilibiliVideo" in background
    assert "window.__playinfo__" in background
    assert 'message.type === "bridge.shutdown"' in background
    assert "async function cleanupBridgeSessionsForPort" in background
    assert "if (state.socket !== socket) return;" in background
    assert 'message.type === "xhs.mutate"' in background
    assert 'message.type === "douyin.mutate"' in background
    assert "function readXhsEngagementControl" in background
    assert 'inactive: "#collected"' not in background
    assert 'active: "#collected"' in background
    assert "function readDouyinEngagementControl" in background
    assert 'data-e2e="video-player-digg"' in background
    assert 'data-e2e="detail-video-info"' in background
    assert 'data-e2e="video-share-icon-container"' in background
    assert "async function dispatchTrustedPointClick" in background
    assert "the click was not retried" in background
    assert "Chrome did not create an isolated ${platform} engagement window" in background
    assert '"https://www.xiaohongshu.com/explore",\n      "XHS"' in background
    assert '"https://www.douyin.com/",\n      "Douyin"' in background
    assert "async function createEngagementWindow" in background
    assert "width: 1512" in background
    assert "height: 900" in background
    assert "function handleObservedDouyinResponse" in background
    assert "/general\\/search\\/stream" in background
    assert "function runDouyinComments" in background
    assert "Douyin rendered comment stream was not found" in background

    if os.name != "nt":
        token_mode = (tmp_path / "pairing-token").stat().st_mode & 0o777
        pairing_mode = (first.directory / "pairing.json").stat().st_mode & 0o777
        assert token_mode == 0o600
        assert pairing_mode == 0o600
