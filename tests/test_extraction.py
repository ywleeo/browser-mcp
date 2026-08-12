"""Offline fixtures for webpage extraction and XHR formatting."""

from browser_mcp.extraction import extract_content
from browser_mcp.models import BrowserFetchPayload, ExtractMode, XhrEntry


def test_readability_removes_navigation_and_preserves_article() -> None:
    """Article mode should retain prose while stripping obvious page chrome."""
    html = """
    <html><head><title>Fixture News</title></head><body>
      <nav>Account Pricing Products Support</nav>
      <article><h1>A useful headline</h1>
      <p>This is a sufficiently detailed first paragraph about a useful technical subject.</p>
      <p>The second paragraph adds context, evidence, examples, and a clear conclusion.</p>
      </article><footer>Footer links and copyright</footer>
    </body></html>
    """
    output = extract_content(
        BrowserFetchPayload(final_url="https://example.com/news", html=html),
        ExtractMode.READABILITY,
    )

    assert "A useful headline" in output
    assert "second paragraph" in output
    assert "Account Pricing Products" not in output


def test_xhr_formatter_preserves_all_captured_entries() -> None:
    """XHR mode should retain request metadata, JSON bodies, and unavailable-body markers."""
    payload = BrowserFetchPayload(
        final_url="https://app.example/dashboard",
        html="<title>Dashboard</title>",
        xhr=(
            XhrEntry(
                url="https://app.example/api/data",
                method="POST",
                status=200,
                mime="application/json",
                type="Fetch",
                body='{"value":42}',
            ),
            XhrEntry(url="https://app.example/api/empty", status=204, body=None),
        ),
    )

    output = extract_content(payload, ExtractMode.XHR)

    assert "Captured 2 fetch/XHR responses" in output
    assert "POST https://app.example/api/data" in output
    assert '"value":42' in output
    assert "(body unavailable)" in output
