"""Pure URL validation and rendered-content parsing for Reddit."""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from browser_mcp.sites.html_utils import (
    attribute,
    clean_block_text,
    clean_text,
    first_node,
    integer_attribute,
    parse_rendered_html,
    xpath_nodes,
)
from browser_mcp.sites.models import (
    RedditComment,
    RedditPostResult,
    RedditSearchItem,
    RedditSearchRequest,
    RedditSearchResult,
)

_POST_PATH = re.compile(r"^/(?:r/[^/]+/)?comments/([A-Za-z0-9]+)(?:/[^/]*)?/?$")


class RedditParseError(ValueError):
    """Raised when a Reddit URL or rendered page violates the supported contract."""


def parse_reddit_post_url(url: str) -> str:
    """Accept canonical Reddit post links and return the base36 post id."""
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
        raise RedditParseError(f"not a supported Reddit host: {hostname or '(missing)'}")
    match = _POST_PATH.fullmatch(parsed.path)
    if match is None:
        raise RedditParseError("expected /r/{subreddit}/comments/{post_id}/{slug}/")
    return match.group(1).lower()


def parse_reddit_search(html: str, request: RedditSearchRequest) -> RedditSearchResult:
    """Normalize rendered Reddit post cards and their embedded tracking metadata."""
    root = parse_rendered_html(html)
    items: list[RedditSearchItem] = []
    seen_ids: set[str] = set()
    for unit in xpath_nodes(root, "//*[@data-testid='search-post-unit']"):
        title_anchor = first_node(unit, ".//a[@data-testid='post-title-text' and @href]")
        url = urljoin("https://www.reddit.com", attribute(title_anchor, "href"))
        try:
            post_id = parse_reddit_post_url(url)
        except RedditParseError:
            continue
        if post_id in seen_ids:
            continue
        context = _tracking_context(unit)
        profile = _object(context.get("profile"))
        subreddit = _object(context.get("subreddit"))
        time_node = first_node(unit, ".//time[@datetime]")
        counters = xpath_nodes(unit, ".//*[@data-testid='search-counter-row']//faceplate-number")
        seen_ids.add(post_id)
        items.append(
            RedditSearchItem(
                index=len(items) + 1,
                post_id=post_id,
                url=url,
                title=attribute(title_anchor, "aria-label") or clean_text(title_anchor),
                subreddit=_string(subreddit.get("name")),
                author=_string(profile.get("name")),
                published_at=attribute(time_node, "datetime"),
                score=integer_attribute(counters[0] if counters else None, "number"),
                comments=integer_attribute(counters[1] if len(counters) > 1 else None, "number"),
            )
        )
        if len(items) >= request.limit:
            break
    if not items:
        raise RedditParseError(
            "Reddit returned no rendered posts; open reddit.com in Chrome and complete verification"
        )
    return RedditSearchResult(
        keyword=request.keyword.strip(),
        sort=request.sort,
        items=tuple(items),
    )


def parse_reddit_post(html: str, post_id: str, max_comments: int) -> RedditPostResult:
    """Normalize one rendered Reddit post and a bounded set of visible comments."""
    root = parse_rendered_html(html)
    post = next(
        (
            node
            for node in xpath_nodes(root, "//shreddit-post")
            if attribute(node, "id").removeprefix("t3_").lower() == post_id
        ),
        None,
    )
    if post is None:
        raise RedditParseError(f"Reddit post {post_id} was not found in the rendered page")
    permalink = attribute(post, "permalink")
    body = clean_block_text(first_node(post, ".//*[@slot='text-body']"))
    comments: list[RedditComment] = []
    comment_nodes = xpath_nodes(root, "//shreddit-comment") if max_comments else []
    for node in comment_nodes:
        text = clean_block_text(first_node(node, ".//*[@slot='comment']"))
        if not text:
            continue
        comment_id = attribute(node, "thingid").removeprefix("t1_")
        comments.append(
            RedditComment(
                comment_id=comment_id,
                url=urljoin("https://www.reddit.com", attribute(node, "permalink")),
                author=attribute(node, "author"),
                published_at=attribute(node, "created"),
                score=integer_attribute(node, "score"),
                depth=integer_attribute(node, "depth"),
                text=text,
            )
        )
        if len(comments) >= max_comments:
            break
    media_url = attribute(post, "content-href") or None
    return RedditPostResult(
        post_id=post_id,
        url=urljoin("https://www.reddit.com", permalink),
        title=attribute(post, "post-title"),
        subreddit=attribute(post, "subreddit-prefixed-name").removeprefix("r/"),
        author=attribute(post, "author"),
        published_at=attribute(post, "created-timestamp"),
        score=integer_attribute(post, "score"),
        comment_count=integer_attribute(post, "comment-count"),
        post_type=attribute(post, "post-type"),
        body=body,
        media_url=media_url,
        comments=tuple(comments),
    )


def _tracking_context(unit: Any) -> dict[str, Any]:
    """Decode the nearest post tracking context embedded by Reddit SSR."""
    tracker = first_node(unit, ".//search-telemetry-tracker[@data-faceplate-tracking-context]")
    raw = attribute(tracker, "data-faceplate-tracking-context")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _object(value: object) -> dict[str, Any]:
    """Return JSON objects or an empty mapping for absent tracking data."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    """Return JSON strings without coercing nested values."""
    return value if isinstance(value, str) else ""
