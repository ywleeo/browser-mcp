"""Tests for X status URL validation and rendered tweet normalization."""

import pytest

from browser_mcp.sites.models import XSearchRequest, XSearchSort
from browser_mcp.sites.x import XParseError, parse_x_post, parse_x_post_url, parse_x_search

X_HTML = """
<html><body>
  <article data-testid="tweet">
    <div data-testid="User-Name"><span>Alice</span><span>@alice</span><span>·</span></div>
    <a href="/alice/status/123"><time datetime="2026-08-12T01:02:03Z">1h</time></a>
    <div data-testid="tweetText">Hello <a href="https://example.com/story">world</a></div>
    <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/photo.jpg"></div>
    <button data-testid="reply" aria-label="2 Replies. Reply"></button>
    <button data-testid="retweet" aria-label="3 reposts. Repost"></button>
    <button data-testid="like" aria-label="1,234 Likes. Like"></button>
    <a href="/alice/status/123/analytics" aria-label="5K views. View post analytics"></a>
  </article>
  <article data-testid="tweet">
    <a href="/alice/status/123"><time datetime="2026-08-12T01:02:03Z">1h</time></a>
    <div data-testid="tweetText">Duplicate status</div>
  </article>
</body></html>
"""


def test_x_search_shapes_posts_metrics_media_and_links() -> None:
    """Rendered X articles should become deduplicated structured search posts."""
    request = XSearchRequest(keyword="hello", sort=XSearchSort.LATEST)

    result = parse_x_search(X_HTML, request)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.url == "https://x.com/alice/status/123"
    assert item.author == "Alice"
    assert item.handle == "@alice"
    assert item.likes == "1,234"
    assert item.views == "5K"
    assert item.media_urls == ("https://pbs.twimg.com/media/photo.jpg",)
    assert item.links == ("https://example.com/story",)


def test_x_post_selects_requested_status() -> None:
    """Post detail parsing should return the exact requested status identity."""
    identity = parse_x_post_url("https://twitter.com/alice/status/123?ref=share")

    result = parse_x_post(X_HTML, identity)

    assert result.post_id == "123"
    assert result.text == "Hello world"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/alice/status/123",
        "https://x.com/home",
        "https://x.com/alice/status/not-numeric",
    ],
)
def test_x_post_url_rejects_unsupported_targets(url: str) -> None:
    """X detail reads should not accept foreign hosts or non-status paths."""
    with pytest.raises(XParseError):
        parse_x_post_url(url)
