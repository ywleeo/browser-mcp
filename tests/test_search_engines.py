"""Tests for rendered Google, Bing, and Sogou result normalization."""

import pytest

from browser_mcp.sites.models import SearchEngine, WebSearchRequest
from browser_mcp.sites.search_engines import (
    SearchEngineParseError,
    parse_bing_search,
    parse_google_search,
    parse_sogou_search,
)


def test_google_search_extracts_h3_results_and_deduplicates_urls() -> None:
    """Google result anchors should produce normalized organic results only once."""
    html = """
    <html><body>
      <div><a href="https://example.com/a"><h3>Example A</h3></a>
        <div class="VwiC3b">First result summary.</div></div>
      <a href="https://example.com/a"><h3>Duplicate</h3></a>
      <a href="/search?q=more"><h3>Google navigation</h3></a>
    </body></html>
    """

    result = parse_google_search(html, WebSearchRequest(keyword="example"))

    assert result.engine is SearchEngine.GOOGLE
    assert len(result.items) == 1
    assert result.items[0].title == "Example A"
    assert result.items[0].snippet == "First result summary."


def test_bing_search_extracts_b_algo_results() -> None:
    """Bing organic list items should expose title, URL, host, and caption."""
    html = """
    <ol id="b_results"><li class="b_algo">
      <h2><a href="https://example.org/docs">Example Docs</a></h2>
      <div class="b_caption"><p>Documentation summary.</p></div>
    </li></ol>
    """

    result = parse_bing_search(html, WebSearchRequest(keyword="docs"))

    assert result.engine is SearchEngine.BING
    assert result.items[0].display_url == "example.org"
    assert result.items[0].snippet == "Documentation summary."


def test_sogou_search_resolves_original_urls_and_filters_ads_and_navigation() -> None:
    """Sogou cards should expose original targets without explicit ads or search chrome."""
    html = """
    <div class="rb">
      <h3 class="pt"><a href="/link?url=opaque"><em>Example</em> Result</a>
        <script>tracking noise</script></h3>
      <div id="cacheresult_summary_0">First result summary.</div>
      <div class="r-sech" data-url="https://example.com/original"></div>
    </div>
    <div class="vrwrap">
      <h3 class="vr-title"><a href="https://docs.example.org/">Example Docs</a></h3>
      <p class="star-wiki">Documentation summary.</p>
    </div>
    <div class="vrwrap">
      <h3><a href="/link?url=advertisement">Promoted Result</a></h3>
      <span>广告</span><div data-url="https://ads.example.com/"></div>
    </div>
    <div class="vrwrap">
      <h3><a href="https://www.sogou.com/sogou?query=example">Related News</a></h3>
      <div data-url="http://www.sogou.com"></div>
    </div>
    """

    result = parse_sogou_search(html, WebSearchRequest(keyword="example"))

    assert result.engine is SearchEngine.SOGOU
    assert [item.title for item in result.items] == ["Example Result", "Example Docs"]
    assert [item.url for item in result.items] == [
        "https://example.com/original",
        "https://docs.example.org/",
    ]
    assert result.items[0].snippet == "First result summary."


def test_search_parser_reports_consent_or_verification_pages() -> None:
    """An unrecognized page should fail explicitly instead of returning a false empty result."""
    with pytest.raises(SearchEngineParseError, match="verification"):
        parse_google_search(
            "<html><body>Before you continue</body></html>",
            WebSearchRequest(keyword="x"),
        )
