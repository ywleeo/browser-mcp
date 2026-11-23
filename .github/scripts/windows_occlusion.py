"""Run the Browser MCP mouse actions behind an opaque Windows desktop window."""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import tempfile
import tkinter
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from browser_mcp.bridge.manager import BridgeManager
from browser_mcp.config import AppSettings
from browser_mcp.security import PublicUrlPolicy, UrlPolicyError

TITLE = "Browser MCP occlusion fixture"
PAGE = f"""
<!doctype html><title>{TITLE}</title>
<style>
  html, body {{ margin: 0; height: 3600px; }}
  button {{ position: fixed; inset: 40% 30%; }}
</style>
<button onclick="document.title = '{TITLE} clicked'; fetch('/click')">Record click</button>
<script>
  addEventListener('scroll', () => fetch('/scroll'), {{once:true}})
  addEventListener('visibilitychange', () => fetch('/' + document.visibilityState))
</script>
""".encode()
EFFECTS = {name: Event() for name in ("click", "scroll", "hidden")}

user32 = ctypes.windll.user32
window_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (window_callback, wintypes.LPARAM)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, *([ctypes.c_int] * 4), wintypes.UINT)
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND


class FixtureHandler(BaseHTTPRequestHandler):
    """Serve one deterministic scrollable page on loopback."""

    def do_GET(self) -> None:
        """Serve the fixture with no network dependencies."""
        if effect := EFFECTS.get(self.path.removeprefix("/")):
            effect.set()
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine access logs."""


class FixturePolicy(PublicUrlPolicy):
    """Approve only the local test fixture's origin for this bridge instance."""

    def __init__(self, url: str) -> None:
        """Keep the local fixture separate from production URL checks."""
        super().__init__()
        self.url = url

    async def validate(self, raw_url: str) -> str:
        """Allow the exact fixture URL, including no external redirects."""
        if raw_url != self.url:
            raise UrlPolicyError("unexpected fixture URL")
        return raw_url


def chrome_window() -> tuple[int, wintypes.RECT]:
    """Find the visible native Chrome window hosting the fixture."""
    matches: list[int] = []

    @window_callback
    def visit(window: int, _data: int) -> bool:
        """Stop at the unique fixture page's native window."""
        title = ctypes.create_unicode_buffer(500)
        user32.GetWindowTextW(window, title, len(title))
        if not title.value.startswith(TITLE):
            return True
        matches.append(window)
        return False

    user32.EnumWindows(visit, 0)
    if not matches:
        raise RuntimeError("fixture has no visible native Chrome window")
    bounds = wintypes.RECT()
    if not user32.GetWindowRect(matches[0], ctypes.byref(bounds)):
        raise ctypes.WinError()
    return matches[0], bounds


async def cover_chrome(window: tkinter.Tk) -> None:
    """Place an opaque native window above Chrome and verify the exposed desktop points."""
    target, bounds = chrome_window()
    window.attributes("-topmost", True)
    window.deiconify()
    window.update()
    window.focus_force()
    window.update()
    # Make the managed window the last focused Chrome window before covering it.
    user32.SetForegroundWindow(target)
    await asyncio.sleep(0.2)
    if user32.GetForegroundWindow() != target:
        raise RuntimeError("could not focus the managed Chrome window on this desktop")
    window.attributes("-topmost", False)
    window.update()
    handle = user32.GetAncestor(window.winfo_id(), 2)
    if not user32.SetWindowPos(
        handle, None, bounds.left - 20, bounds.top - 20,
        bounds.right - bounds.left + 40, bounds.bottom - bounds.top + 40, 0,
    ):
        raise ctypes.WinError()
    window.focus_force()
    window.update()

    # Leave Chrome enough time to calculate a stable occlusion state.
    await asyncio.sleep(2)
    points = [
        (bounds.left + 12, bounds.top + 12),
        (bounds.right - 12, bounds.top + 12),
        (bounds.left + 12, bounds.bottom - 12),
        (bounds.right - 12, bounds.bottom - 12),
        ((bounds.left + bounds.right) // 2, (bounds.top + bounds.bottom) // 2),
    ]
    for point in points:
        hit = user32.WindowFromPoint(wintypes.POINT(*point))
        if user32.GetAncestor(hit, 2) != handle:
            window.withdraw()
            raise RuntimeError(f"native cover did not hide Chrome at desktop point {point}")
    print(f"Verified native cover at {len(points)} points, "
          f"page reported hidden={EFFECTS['hidden'].is_set()}")


async def trial(url: str, window: tkinter.Tk, action: str, *, focus: bool) -> str:
    """Launch a fresh browser and exercise one action under the checked native cover."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        settings = AppSettings(
            bridge_port_pool_size=1,
            data_dir=root / "bridge",
            focus_before_input=focus,
        )
        manager = BridgeManager(settings, url_policy=FixturePolicy(url))
        await manager.start()
        with (root / "chrome.log").open("w+", encoding="utf-8") as log:
            chrome = subprocess.Popen(
                [
                    os.environ["BROWSER_MCP_TEST_CHROME"],
                    f"--user-data-dir={root / 'profile'}",
                    f"--load-extension={settings.extension_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=700,500",
                    "about:blank",
                ],
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(30):
                    if (await manager.status()).connected:
                        break
                    if chrome.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(f"Chrome exited before connecting: {log.read()}")
                    await asyncio.sleep(0.5)
                else:
                    log.seek(0)
                    raise RuntimeError(f"extension did not connect: {log.read()}")

                snapshot = await asyncio.wait_for(manager.interact("snapshot", {"url": url}), 20)
                assert snapshot.state.title == TITLE, snapshot.state.title
                print(f"Opened fixture for {action}, focus_before_input={focus}")
                EFFECTS["hidden"].clear()
                await cover_chrome(window)
                EFFECTS[action].clear()
                args = (
                    {"x": snapshot.state.viewport.width / 2,
                     "y": snapshot.state.viewport.height / 2,
                     "coordinate_space": "viewport"}
                    if action == "click"
                    else {"amount": 600, "direction": "down",
                          "x": 16, "y": 100, "coordinate_space": "viewport"}
                )
                started = monotonic()
                try:
                    result = await asyncio.wait_for(manager.interact(action, args), 12)
                except TimeoutError:
                    elapsed = monotonic() - started
                    print(f"{action} focus={focus}: no reply after {elapsed:.1f}s, "
                          f"page effect observed={EFFECTS[action].is_set()}")
                    return "timeout"
                if action == "click":
                    changed = result.state.title == f"{TITLE} clicked"
                else:
                    changed = result.state.viewport.scroll_y > 0
                elapsed = monotonic() - started
                print(f"{action} focus={focus}: replied in {elapsed:.2f}s, changed={changed}")
                return "passed" if changed else "no_effect"
            finally:
                window.withdraw()
                window.update()
                # Stop only this test's browser and its child processes.
                subprocess.run(["taskkill", "/PID", str(chrome.pid), "/T", "/F"], check=False)
                await asyncio.to_thread(chrome.wait, 10)
                await manager.close()


async def main() -> None:
    """Compare covered input with foreground activation disabled and enabled."""
    user32.SetProcessDPIAware()
    window = tkinter.Tk()
    window.title("Browser MCP opaque native cover")
    window.configure(background="#304355")
    window.withdraw()
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as fixture:
        thread = Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{fixture.server_port}/"
        try:
            for action in ("scroll", "click"):
                baseline = await trial(url, window, action, focus=False)
                opt_in = await trial(url, window, action, focus=True)
                outcome = (
                    "baseline reproduced; opt-in succeeds"
                    if baseline == "timeout" and opt_in == "passed"
                    else f"baseline={baseline}; opt-in={opt_in}"
                )
                print(f"RESULT {action}: {outcome}")
                if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                    with Path(summary).open("a", encoding="utf-8") as stream:
                        stream.write(f"- **{action}**: {outcome}\n")
                if opt_in != "passed":
                    raise RuntimeError(f"{action} failed with foreground activation: {opt_in}")
        finally:
            fixture.shutdown()
            window.destroy()


if __name__ == "__main__":
    asyncio.run(main())
