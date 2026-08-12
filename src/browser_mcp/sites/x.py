"""Pure URL validation and rendered-post parsing for X (formerly Twitter)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from browser_mcp.sites.html_utils import (
    attribute,
    clean_block_text,
    first_node,
    parse_rendered_html,
    xpath_nodes,
    xpath_strings,
)
from browser_mcp.sites.models import (
    XPostItem,
    XPostResult,
    XSearchRequest,
    XSearchResult,
)

_STATUS_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/)?$")


class XParseError(ValueError):
    """Raised when an X URL or rendered timeline violates the supported contract."""


@dataclass(frozen=True, slots=True)
class XPostIdentity:
    """Canonical handle and numeric id parsed from an X status URL."""

    handle: str
    post_id: str


def parse_x_post_url(url: str) -> XPostIdentity:
    """Accept one exact x.com or twitter.com status URL."""
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise XParseError(f"not a supported X host: {hostname or '(missing)'}")
    match = _STATUS_PATH.fullmatch(parsed.path)
    if match is None:
        raise XParseError("expected /{handle}/status/{numeric_id}")
    return XPostIdentity(handle=match.group(1), post_id=match.group(2))


def parse_x_search(html: str, request: XSearchRequest) -> XSearchResult:
    """Normalize unique posts from one rendered X search timeline."""
    root = parse_rendered_html(html)
    items: list[XPostItem] = []
    seen_ids: set[str] = set()
    for article in xpath_nodes(root, "//article[@data-testid='tweet']"):
        item = _parse_article(article, len(items) + 1)
        if item is None or item.post_id in seen_ids:
            continue
        seen_ids.add(item.post_id)
        items.append(item)
        if len(items) >= request.limit:
            break
    if not items:
        raise XParseError(
            "X returned no rendered posts; open x.com in Chrome and complete login or verification"
        )
    return XSearchResult(keyword=request.keyword.strip(), sort=request.sort, items=tuple(items))


def parse_x_post(html: str, identity: XPostIdentity) -> XPostResult:
    """Select and normalize the requested status from a rendered X conversation page."""
    root = parse_rendered_html(html)
    for article in xpath_nodes(root, "//article[@data-testid='tweet']"):
        item = _parse_article(article, 1)
        if item is not None and item.post_id == identity.post_id:
            return XPostResult(**item.model_dump(exclude={"index"}))
    raise XParseError(f"X post {identity.post_id} was not found in the rendered page")


def _parse_article(article: Any, index: int) -> XPostItem | None:
    """Normalize one semantic X tweet article while ignoring incomplete placeholders."""
    time_node = first_node(article, ".//a[contains(@href, '/status/')]/time")
    status_anchor = time_node.getparent() if time_node is not None else None
    status_path = attribute(status_anchor, "href").split("?", maxsplit=1)[0]
    match = _STATUS_PATH.fullmatch(status_path)
    if match is None:
        return None
    user = first_node(article, ".//*[@data-testid='User-Name']")
    user_fragments = (
        [" ".join(value.split()) for value in xpath_strings(user, ".//span/text()")]
        if user is not None
        else []
    )
    handle = next((value for value in user_fragments if value.startswith("@")), "")
    author = next(
        (value for value in user_fragments if value and not value.startswith("@") and value != "·"),
        "",
    )
    text_node = first_node(article, ".//*[@data-testid='tweetText']")
    return XPostItem(
        index=index,
        post_id=match.group(2),
        url=f"https://x.com{status_path}",
        author=author,
        handle=handle or f"@{match.group(1)}",
        text=clean_block_text(text_node),
        published_at=attribute(time_node, "datetime"),
        replies=_metric(article, "reply"),
        reposts=_metric(article, "retweet"),
        likes=_metric(article, "like"),
        views=_views(article),
        media_urls=_media_urls(article),
        links=_external_links(article),
    )


def _metric(article: Any, test_id: str) -> str:
    """Preserve the localized counter token from an X action label."""
    node = first_node(article, f".//*[@data-testid='{test_id}']")
    return _leading_counter(attribute(node, "aria-label"))


def _views(article: Any) -> str:
    """Read the view counter from a status analytics link when present."""
    node = first_node(article, ".//a[contains(@href, '/analytics')]")
    return _leading_counter(attribute(node, "aria-label"))


def _leading_counter(label: str) -> str:
    """Return a leading numeric or compact counter token from a localized label."""
    match = re.search(r"(?:^|\s)([\d.,]+(?:[KMB]|万|千)?)", label, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _media_urls(article: Any) -> tuple[str, ...]:
    """Collect unique photo sources and video sources or posters from one post."""
    values: list[str] = []
    for node in xpath_nodes(
        article,
        ".//*[@data-testid='tweetPhoto']//img[@src]"
        " | .//video[@src or @poster]"
        " | .//video/source[@src]",
    ):
        value = attribute(node, "src") or attribute(node, "poster")
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _external_links(article: Any) -> tuple[str, ...]:
    """Collect unique external links explicitly embedded in post text or cards."""
    values: list[str] = []
    for node in xpath_nodes(
        article,
        ".//*[@data-testid='tweetText']//a[@href] | .//*[@data-testid='card.wrapper']//a[@href]",
    ):
        value = urljoin("https://x.com", attribute(node, "href"))
        hostname = (urlsplit(value).hostname or "").lower()
        if hostname and hostname not in {"x.com", "www.x.com"} and value not in values:
            values.append(value)
    return tuple(values)
