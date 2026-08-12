"""Pure rendered-result parsers for Google, Bing, and Sogou search."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from browser_mcp.sites.html_utils import (
    attribute,
    clean_text,
    first_node,
    parse_rendered_html,
    xpath_nodes,
    xpath_strings,
)
from browser_mcp.sites.models import (
    SearchEngine,
    WebSearchItem,
    WebSearchRequest,
    WebSearchResult,
)


class SearchEngineParseError(ValueError):
    """Raised when a search page no longer exposes recognizable organic results."""


def parse_google_search(html: str, request: WebSearchRequest) -> WebSearchResult:
    """Normalize rendered Google anchors whose result title is an h3 element."""
    root = parse_rendered_html(html)
    candidates: list[tuple[str, str, str]] = []
    for anchor in xpath_nodes(root, "//a[.//h3 and @href]"):
        title = clean_text(first_node(anchor, ".//h3"))
        url = _google_target(attribute(anchor, "href"))
        if not title or not _is_external_result(url, SearchEngine.GOOGLE):
            continue
        snippet = _nearest_text(anchor, ("VwiC3b", "yXK7lf", "IsZvec"))
        candidates.append((title, url, snippet))
    return _result(SearchEngine.GOOGLE, request, candidates)


def parse_bing_search(html: str, request: WebSearchRequest) -> WebSearchResult:
    """Normalize rendered Bing organic result list items."""
    root = parse_rendered_html(html)
    candidates: list[tuple[str, str, str]] = []
    result_xpath = "//li[contains(concat(' ', normalize-space(@class), ' '), ' b_algo ')]"
    for container in xpath_nodes(root, result_xpath):
        anchor = first_node(container, ".//h2/a[@href]")
        title = clean_text(anchor)
        url = attribute(anchor, "href")
        if not title or not _is_external_result(url, SearchEngine.BING):
            continue
        snippet_node = first_node(
            container,
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' b_caption ')]//p"
            " | .//p[contains(@class, 'b_lineclamp')]",
        )
        candidates.append((title, url, clean_text(snippet_node)))
    return _result(SearchEngine.BING, request, candidates)


def parse_sogou_search(html: str, request: WebSearchRequest) -> WebSearchResult:
    """Normalize Sogou result cards while resolving their embedded original URLs."""
    root = parse_rendered_html(html)
    candidates: list[tuple[str, str, str]] = []
    for heading in xpath_nodes(root, "//h3[.//a[@href]]"):
        anchor = first_node(heading, ".//a[@href]")
        title = clean_text(anchor)
        card = _sogou_card(heading)
        if not title or card is None or _sogou_is_ad(card):
            continue
        url = _sogou_target(anchor, card)
        if not _is_external_result(url, SearchEngine.SOGOU):
            continue
        snippet_node = first_node(
            card,
            ".//*[starts-with(@id, 'cacheresult_summary_')]"
            " | .//*[contains(concat(' ', normalize-space(@class), ' '), ' star-wiki ')]"
            " | .//*[contains(concat(' ', normalize-space(@class), ' '), ' space-txt ')]",
        )
        candidates.append((title, url, clean_text(snippet_node)))
    return _result(SearchEngine.SOGOU, request, candidates)


def _sogou_card(heading: Any) -> Any | None:
    """Return the nearest ordinary Sogou result-card ancestor."""
    expression = (
        "ancestor::*["
        "contains(concat(' ', normalize-space(@class), ' '), ' vrwrap ') or "
        "contains(concat(' ', normalize-space(@class), ' '), ' rb ')"
        "][1]"
    )
    return first_node(heading, expression)


def _sogou_target(anchor: Any, card: Any) -> str:
    """Prefer direct title links, then Sogou's embedded original result URL."""
    direct = urljoin("https://www.sogou.com", attribute(anchor, "href"))
    if _is_external_result(direct, SearchEngine.SOGOU):
        return direct
    for value in xpath_strings(card, ".//*[@data-url]/@data-url"):
        if _is_external_result(value, SearchEngine.SOGOU):
            return value
    cite = first_node(card, ".//a[contains(@class, 'citeLinkClass') and @href]")
    return urljoin("https://www.sogou.com", attribute(cite, "href"))


def _sogou_is_ad(card: Any) -> bool:
    """Reject cards carrying an explicit advertisement marker."""
    return bool(
        xpath_nodes(
            card,
            ".//*[@posid and starts-with(translate(@posid, 'AD', 'ad'), 'ad')]"
            " | .//*[normalize-space(string(.))='广告']",
        )
    )


def _nearest_text(anchor: Any, class_names: tuple[str, ...]) -> str:
    """Find the first known snippet element in a bounded ancestor walk."""
    current = anchor
    for _level in range(8):
        parent = current.getparent()
        if parent is None:
            break
        current = parent
        for class_name in class_names:
            expression = (
                ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                f"' {class_name} ')]"
            )
            snippet = clean_text(first_node(current, expression))
            if snippet:
                return snippet[:500]
    return ""


def _google_target(raw_url: str) -> str:
    """Unwrap Google's legacy /url redirect while preserving direct result links."""
    parsed = urlsplit(raw_url)
    if parsed.path == "/url":
        values = parse_qs(parsed.query).get("q", [])
        if values:
            return values[0]
    return urljoin("https://www.google.com", raw_url)


def _is_external_result(url: str, engine: SearchEngine) -> bool:
    """Reject malformed and search-engine navigation URLs."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    path = parsed.path.rstrip("/") or "/"
    if engine is SearchEngine.GOOGLE:
        return not (hostname in {"google.com", "www.google.com"} and path == "/search")
    if engine is SearchEngine.BING:
        return not (hostname in {"bing.com", "www.bing.com", "cn.bing.com"} and path == "/search")
    if hostname == "open-sogou":
        return False
    return not (
        hostname in {"sogou.com", "www.sogou.com"}
        and path in {"/", "/web", "/sogou", "/link"}
    )


def _result(
    engine: SearchEngine,
    request: WebSearchRequest,
    candidates: Iterable[tuple[str, str, str]],
) -> WebSearchResult:
    """Deduplicate, bound, and model-validate normalized result candidates."""
    items: list[WebSearchItem] = []
    seen_urls: set[str] = set()
    for title, url, snippet in candidates:
        normalized = url.split("#", maxsplit=1)[0]
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        items.append(
            WebSearchItem(
                index=len(items) + 1,
                title=" ".join(title.split()),
                url=normalized,
                display_url=(urlsplit(normalized).hostname or "").removeprefix("www."),
                snippet=" ".join(snippet.split())[:500],
            )
        )
        if len(items) >= request.limit:
            break
    if not items:
        raise SearchEngineParseError(
            f"{engine.value} returned no recognizable web results; "
            "open the engine in Chrome and complete any consent or verification prompt"
        )
    return WebSearchResult(engine=engine, keyword=request.keyword.strip(), items=tuple(items))
