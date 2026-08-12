"""Tests for Reddit URL validation, search cards, posts, and comments."""

import html as html_module
import json

import pytest

from browser_mcp.sites.models import RedditSearchRequest
from browser_mcp.sites.reddit import (
    RedditParseError,
    parse_reddit_post,
    parse_reddit_post_url,
    parse_reddit_search,
)


def test_reddit_search_normalizes_ssr_post_cards() -> None:
    """Reddit search cards should expose tracking metadata and visible counters."""
    context = html_module.escape(
        json.dumps(
            {
                "profile": {"name": "alice"},
                "subreddit": {"name": "python"},
            }
        ),
        quote=True,
    )
    html = f"""
    <div data-testid="search-post-unit">
      <search-telemetry-tracker
        data-faceplate-tracking-context="{context}"></search-telemetry-tracker>
      <a data-testid="post-title-text" aria-label="A useful post"
         href="/r/python/comments/abc123/a_useful_post/">A useful post</a>
      <time datetime="2026-08-12T02:03:04Z"></time>
      <div data-testid="search-counter-row">
        <faceplate-number number="42"></faceplate-number>
        <faceplate-number number="7"></faceplate-number>
      </div>
    </div>
    """

    result = parse_reddit_search(html, RedditSearchRequest(keyword="useful"))

    item = result.items[0]
    assert item.post_id == "abc123"
    assert item.subreddit == "python"
    assert item.author == "alice"
    assert item.score == 42
    assert item.comments == 7


def test_reddit_post_includes_body_media_and_bounded_comments() -> None:
    """Reddit post parsing should include metadata and honor the visible comment budget."""
    html = """
    <shreddit-post id="t3_abc123" permalink="/r/python/comments/abc123/post/"
      post-title="Post title" subreddit-prefixed-name="r/python" author="alice"
      created-timestamp="2026-08-12T02:03:04Z" score="42" comment-count="2"
      post-type="text" content-href="https://example.com/media">
      <div slot="text-body"><p>First paragraph.</p><p>Second paragraph.</p></div>
    </shreddit-post>
    <shreddit-comment thingid="t1_c1" permalink="/r/python/comments/abc123/comment/c1/"
      author="bob" created="2026-08-12T03:00:00Z" score="9" depth="0">
      <div slot="comment"><p>First comment.</p></div>
    </shreddit-comment>
    <shreddit-comment thingid="t1_c2" permalink="/r/python/comments/abc123/comment/c2/"
      author="carol" created="2026-08-12T04:00:00Z" score="3" depth="1">
      <div slot="comment"><p>Second comment.</p></div>
    </shreddit-comment>
    """

    result = parse_reddit_post(html, "abc123", max_comments=1)

    assert result.title == "Post title"
    assert result.subreddit == "python"
    assert result.body == "First paragraph.Second paragraph."
    assert result.media_url == "https://example.com/media"
    assert len(result.comments) == 1
    assert result.comments[0].text == "First comment."


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/r/python/comments/abc123/post/",
        "https://www.reddit.com/r/python/",
        "https://www.reddit.com/user/alice/",
    ],
)
def test_reddit_post_url_rejects_unsupported_targets(url: str) -> None:
    """Reddit detail reads should remain constrained to post URLs."""
    with pytest.raises(RedditParseError):
        parse_reddit_post_url(url)
