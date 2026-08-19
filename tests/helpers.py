"""Shared deterministic test doubles."""

import ipaddress
import socket
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from browser_mcp.models import (
    BrowserElement,
    BrowserFetchPayload,
    BrowserPageState,
    BrowserReadRequest,
    BrowserStatus,
    BrowserViewport,
    BrowserVisualResult,
)
from browser_mcp.security import PublicUrlPolicy


def reserve_free_port() -> int:
    """Ask the kernel for an unused localhost port for one short test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], listener.getsockname())[1]


class FakeBridge:
    """Application-test bridge without sockets or filesystem writes."""

    def __init__(
        self,
        extension_dir: Path,
        payload: BrowserFetchPayload | None = None,
    ) -> None:
        """Initialize observable lifecycle state."""
        self.extension_dir = extension_dir
        self.payload = payload or BrowserFetchPayload(
            final_url="https://example.com/article",
            html="<html><head><title>Example</title></head><body>Rendered</body></html>",
            text="Rendered visible text",
        )
        self.fetches: list[BrowserReadRequest] = []
        self.site_responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.site_requests: list[tuple[str, str, dict[str, object]]] = []
        self.interactions: list[tuple[str, dict[str, object]]] = []
        self.logged_in_sites = {
            "zhihu": True,
            "xhs": True,
            "douyin": True,
            "x": True,
            "reddit": True,
        }
        self.started = False
        self.closed = False

    async def start(self) -> None:
        """Record bridge startup."""
        self.started = True

    async def close(self) -> None:
        """Record bridge shutdown."""
        self.closed = True

    async def status(self) -> BrowserStatus:
        """Return a stable disconnected stage-2 status."""
        return BrowserStatus(
            state="disconnected",
            connected=False,
            bridge_port=17_880,
            bridge_port_pool=(17_880, 17_889),
            extension_dir=str(self.extension_dir),
            detail="Bridge is listening.",
        )

    async def fetch(self, request: BrowserReadRequest) -> BrowserFetchPayload:
        """Return a configured rendered payload and retain the validated request."""
        self.fetches.append(request)
        login_payload = self._login_probe_payload(str(request.url))
        if login_payload is not None:
            return login_payload
        return self.payload

    def _login_probe_payload(self, url: str) -> BrowserFetchPayload | None:
        """Return deterministic login markup for exact harmless platform probe URLs."""
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/") or "/"
        if host == "www.zhihu.com" and path == "/":
            current_user = '"test-user"' if self.logged_in_sites["zhihu"] else "null"
            return BrowserFetchPayload(
                final_url="https://www.zhihu.com/",
                html=(
                    '<script id="js-initialData">'
                    f'{{"initialState":{{"currentUser":{current_user}}}}}'
                    "</script>"
                ),
            )
        if host == "www.xiaohongshu.com" and path == "/explore":
            logged_in = "true" if self.logged_in_sites["xhs"] else "false"
            return BrowserFetchPayload(
                final_url="https://www.xiaohongshu.com/explore",
                html=(
                    "<script>window.__INITIAL_STATE__ = "
                    f'{{"user":{{"loggedIn":{logged_in},"userInfo":'
                    '{"nickname":"测试账号"}}}</script>'
                ),
            )
        if host == "www.douyin.com" and path == "/":
            if self.logged_in_sites["douyin"]:
                return BrowserFetchPayload(
                    final_url="https://www.douyin.com/",
                    html='<a href="/user/self">我的</a>',
                )
            return BrowserFetchPayload(
                final_url="https://www.douyin.com/",
                html="<button>登录</button>",
            )
        if host == "x.com" and path == "/home":
            if self.logged_in_sites["x"]:
                return BrowserFetchPayload(
                    final_url="https://x.com/home",
                    html='<a data-testid="AppTabBar_Profile_Link" href="/test_user"></a>',
                )
            return BrowserFetchPayload(
                final_url="https://x.com/i/flow/login",
                html='<a data-testid="loginButton" href="/i/flow/login"></a>',
            )
        if host == "www.reddit.com" and path == "/":
            logged_in = "true" if self.logged_in_sites["reddit"] else "false"
            return BrowserFetchPayload(
                final_url="https://www.reddit.com/",
                html=f'<shreddit-app user-logged-in="{logged_in}"></shreddit-app>',
            )
        return None

    async def request(
        self,
        message_type: str,
        action: str,
        args: dict[str, object],
        *,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Return one configured site-adapter response and record its namespace."""
        del timeout_seconds
        self.site_requests.append((message_type, action, args))
        try:
            return self.site_responses[(message_type, action)]
        except KeyError as error:
            raise RuntimeError(f"missing fake response for {message_type}:{action}") from error

    async def interact(self, action: str, args: dict[str, object]) -> BrowserVisualResult:
        """Return one deterministic visual state and record the browser action."""
        self.interactions.append((action, args))
        return BrowserVisualResult(
            state=BrowserPageState(
                action=action,
                url=str(args.get("url", "https://example.com/form")),
                title="Example form",
                screenshot_mime_type="image/jpeg",
                viewport=BrowserViewport(
                    width=1280,
                    height=720,
                    device_scale_factor=2,
                    scroll_x=0,
                    scroll_y=0,
                    document_width=1280,
                    document_height=1600,
                ),
                elements=(
                    BrowserElement(
                        element_id="e1",
                        tag="input",
                        role="textbox",
                        name="Search",
                        input_type="search",
                        x=20,
                        y=30,
                        width=240,
                        height=36,
                    ),
                ),
                visible_text="Example form Search",
            ),
            screenshot_data="/9j/2Q==",
        )


def allow_public_url_policy() -> PublicUrlPolicy:
    """Return a deterministic policy whose DNS names resolve to a documentation address."""

    async def resolve(
        hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """Resolve every test hostname without touching the network."""
        del hostname, port
        return (ipaddress.ip_address("93.184.216.34"),)

    return PublicUrlPolicy(resolve)
