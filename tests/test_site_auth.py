"""Tests for platform login detection and actionable task gates."""

import pytest

from browser_mcp.sites.auth import (
    SiteLoginRequiredError,
    parse_site_login_status,
    require_site_login,
)
from browser_mcp.sites.models import SiteLoginState, SitePlatform


@pytest.mark.parametrize(
    ("platform", "html", "final_url", "account"),
    [
        (
            SitePlatform.ZHIHU,
            '<script id="js-initialData">'
            '{"initialState":{"currentUser":"zhihu-user"}}</script>',
            "https://www.zhihu.com/",
            "zhihu-user",
        ),
        (
            SitePlatform.XHS,
            '<script>window.__INITIAL_STATE__ = {"user":{"loggedIn":true,'
            '"userInfo":{"nickname":"小红书用户"}}}</script>',
            "https://www.xiaohongshu.com/explore",
            "小红书用户",
        ),
        (
            SitePlatform.X,
            '<a data-testid="AppTabBar_Profile_Link" href="/x_user"></a>',
            "https://x.com/home",
            "x_user",
        ),
        (
            SitePlatform.REDDIT,
            '<shreddit-app user-logged-in="true"></shreddit-app>',
            "https://www.reddit.com/",
            "",
        ),
    ],
)
def test_detects_logged_in_platform_sessions(
    platform: SitePlatform,
    html: str,
    final_url: str,
    account: str,
) -> None:
    """Each supported platform should expose a positive marker without reading cookies."""
    status = parse_site_login_status(platform, html, final_url)

    assert status.state is SiteLoginState.LOGGED_IN
    assert status.logged_in is True
    assert status.account_label == account


@pytest.mark.parametrize(
    ("platform", "html", "final_url"),
    [
        (
            SitePlatform.ZHIHU,
            '<script id="js-initialData">{"initialState":{"currentUser":null}}</script>',
            "https://www.zhihu.com/",
        ),
        (
            SitePlatform.XHS,
            '<script>window.__INITIAL_STATE__ = {"user":{"loggedIn":false}}</script>',
            "https://www.xiaohongshu.com/explore",
        ),
        (
            SitePlatform.X,
            '<a data-testid="loginButton" href="/i/flow/login"></a>',
            "https://x.com/i/flow/login",
        ),
        (
            SitePlatform.REDDIT,
            '<shreddit-app user-logged-in="false"></shreddit-app>',
            "https://www.reddit.com/",
        ),
    ],
)
def test_detects_logged_out_platform_sessions(
    platform: SitePlatform,
    html: str,
    final_url: str,
) -> None:
    """Explicit logged-out markers should return an actionable login URL."""
    status = parse_site_login_status(platform, html, final_url)

    assert status.state is SiteLoginState.LOGGED_OUT
    assert status.logged_in is False
    assert status.login_url.startswith("https://")
    with pytest.raises(SiteLoginRequiredError, match="本次任务未执行"):
        require_site_login(status)


def test_unknown_login_state_is_fail_closed() -> None:
    """Page drift or verification screens should block tasks until login is confirmed."""
    status = parse_site_login_status(
        SitePlatform.X,
        "<html><body>verification</body></html>",
        "https://x.com/home",
    )

    assert status.state is SiteLoginState.UNKNOWN
    with pytest.raises(SiteLoginRequiredError, match="无法确认"):
        require_site_login(status)
