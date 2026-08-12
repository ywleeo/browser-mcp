"""Rendered-page login detection for authenticated platform capabilities."""

from __future__ import annotations

import json
import re
from typing import Any, Final, cast
from urllib.parse import urlsplit

from browser_mcp.sites.html_utils import attribute, first_node, parse_rendered_html
from browser_mcp.sites.models import SiteLoginState, SiteLoginStatus, SitePlatform


class SiteLoginRequiredError(RuntimeError):
    """Tell an MCP client to ask the user to log in before retrying a platform task."""


_PROBE_URLS: Final[dict[SitePlatform, str]] = {
    SitePlatform.ZHIHU: "https://www.zhihu.com/",
    SitePlatform.XHS: "https://www.xiaohongshu.com/explore",
    SitePlatform.X: "https://x.com/home",
    SitePlatform.REDDIT: "https://www.reddit.com/",
}

_LOGIN_URLS: Final[dict[SitePlatform, str]] = {
    SitePlatform.ZHIHU: "https://www.zhihu.com/signin",
    SitePlatform.XHS: "https://www.xiaohongshu.com/explore",
    SitePlatform.X: "https://x.com/i/flow/login",
    SitePlatform.REDDIT: "https://www.reddit.com/login/",
}

_PLATFORM_NAMES: Final[dict[SitePlatform, str]] = {
    SitePlatform.ZHIHU: "知乎",
    SitePlatform.XHS: "小红书",
    SitePlatform.X: "X",
    SitePlatform.REDDIT: "Reddit",
}


def login_probe_url(platform: SitePlatform) -> str:
    """Return the harmless page used to inspect one platform session."""
    return _PROBE_URLS[platform]


def parse_site_login_status(
    platform: SitePlatform,
    html: str,
    final_url: str,
) -> SiteLoginStatus:
    """Detect a platform login state from rendered HTML and its final URL."""
    if platform is SitePlatform.ZHIHU:
        state, account = _zhihu_status(html)
    elif platform is SitePlatform.XHS:
        state, account = _xhs_status(html)
    elif platform is SitePlatform.X:
        state, account = _x_status(html, final_url)
    else:
        state, account = _reddit_status(html, final_url)
    name = _PLATFORM_NAMES[platform]
    if state is SiteLoginState.LOGGED_IN:
        detail = f"当前 Chrome Profile 已登录{name}。"
    elif state is SiteLoginState.LOGGED_OUT:
        detail = f"当前 Chrome Profile 尚未登录{name}，请先打开登录页完成登录。"
    else:
        detail = f"无法确认当前 Chrome Profile 的{name}登录状态，请打开登录页检查后重试。"
    return SiteLoginStatus(
        platform=platform,
        state=state,
        logged_in=state is SiteLoginState.LOGGED_IN,
        login_url=_LOGIN_URLS[platform],
        account_label=account,
        detail=detail,
    )


def require_site_login(status: SiteLoginStatus) -> None:
    """Block a platform task and provide an actionable user-facing login prompt."""
    if status.logged_in:
        return
    raise SiteLoginRequiredError(
        f"{status.detail} 登录地址：{status.login_url}。本次任务未执行；登录后请重试。"
    )


def _zhihu_status(html: str) -> tuple[SiteLoginState, str]:
    """Read Zhihu's currentUser identity from its SSR initial-state script."""
    root = parse_rendered_html(html)
    script = first_node(root, "//script[@id='js-initialData']")
    raw = script.text if script is not None and isinstance(script.text, str) else ""
    data = _json_object(raw)
    initial = _object(data.get("initialState"))
    if "currentUser" not in initial:
        return SiteLoginState.UNKNOWN, ""
    current = initial.get("currentUser")
    if isinstance(current, str):
        state = SiteLoginState.LOGGED_IN if current else SiteLoginState.LOGGED_OUT
        return state, current
    current_user = _object(current)
    identity = _string(current_user.get("urlToken")) or _string(current_user.get("id"))
    name = _string(current_user.get("name")) or identity
    state = SiteLoginState.LOGGED_IN if identity else SiteLoginState.LOGGED_OUT
    return state, name


def _xhs_status(html: str) -> tuple[SiteLoginState, str]:
    """Read Xiaohongshu's explicit loggedIn flag and current account metadata."""
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]+?\})\s*;?\s*(?:</script>|\n)",
        html,
    )
    if match is None:
        return SiteLoginState.UNKNOWN, ""
    data = _json_object(re.sub(r":\s*undefined\b", ": null", match.group(1)))
    user = _object(data.get("user"))
    logged_in = user.get("loggedIn")
    info = _object(user.get("userInfo"))
    account = _string(info.get("nickname")) or _string(info.get("userId"))
    if logged_in is True:
        return SiteLoginState.LOGGED_IN, account
    if logged_in is False:
        return SiteLoginState.LOGGED_OUT, ""
    return SiteLoginState.UNKNOWN, ""


def _x_status(html: str, final_url: str) -> tuple[SiteLoginState, str]:
    """Detect X session chrome without reading or returning any cookies."""
    root = parse_rendered_html(html)
    profile = first_node(root, "//*[@data-testid='AppTabBar_Profile_Link']")
    switcher = first_node(root, "//*[@data-testid='SideNav_AccountSwitcher_Button']")
    if profile is not None or switcher is not None:
        path = attribute(profile, "href")
        return SiteLoginState.LOGGED_IN, path.removeprefix("/")
    parsed = urlsplit(final_url)
    login_marker = first_node(
        root,
        "//*[@data-testid='loginButton' or @data-testid='signupButton']"
        " | //a[contains(@href, '/i/flow/login')]",
    )
    if parsed.path.startswith("/i/flow/login") or login_marker is not None:
        return SiteLoginState.LOGGED_OUT, ""
    return SiteLoginState.UNKNOWN, ""


def _reddit_status(html: str, final_url: str) -> tuple[SiteLoginState, str]:
    """Read Reddit's explicit SSR user-logged-in application attribute."""
    root = parse_rendered_html(html)
    app = first_node(root, "//shreddit-app")
    if app is not None:
        if attribute(app, "user-logged-in").lower() == "true":
            return SiteLoginState.LOGGED_IN, ""
        return SiteLoginState.LOGGED_OUT, ""
    if urlsplit(final_url).path.startswith("/login"):
        return SiteLoginState.LOGGED_OUT, ""
    return SiteLoginState.UNKNOWN, ""


def _json_object(raw: str) -> dict[str, Any]:
    """Decode one JSON object while mapping parse failures to an empty object."""
    try:
        value: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _object(value: object) -> dict[str, Any]:
    """Return JSON objects or an empty mapping for absent variants."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    """Return JSON strings without coercing nested values."""
    return value if isinstance(value, str) else ""
