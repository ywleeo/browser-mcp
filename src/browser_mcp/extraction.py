"""Pure server-side formatting for rendered DOM, visible text, and XHR captures."""

from __future__ import annotations

from html.parser import HTMLParser
from importlib import import_module
from typing import Any, cast

from readability import Document  # type: ignore[import-untyped]

from browser_mcp.models import BrowserFetchPayload, ExtractMode, XhrEntry


class ExtractionError(RuntimeError):
    """Raised when a rendered page cannot satisfy the requested extraction mode."""


class _TextParser(HTMLParser):
    """Convert Readability's article HTML to text without a second parser dependency."""

    _BLOCK_TAGS = {
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "section",
        "tr",
    }

    def __init__(self) -> None:
        """Initialize text chunks and ignored script/style nesting."""
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Insert structural line breaks and ignore non-content elements."""
        del attrs
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close ignored regions or terminate a block with a line break."""
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        """Retain visible text data outside ignored elements."""
        if not self._ignored_depth:
            self.chunks.append(data)


class _TitleParser(HTMLParser):
    """Extract the first rendered HTML title with malformed-markup tolerance."""

    def __init__(self) -> None:
        """Initialize title state."""
        super().__init__(convert_charrefs=True)
        self._inside_title = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Begin collecting the first title element."""
        del attrs
        if tag == "title" and not self.chunks:
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        """Stop collecting after the title closes."""
        if tag == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        """Collect title character data only."""
        if self._inside_title:
            self.chunks.append(data)


def extract_content(payload: BrowserFetchPayload, mode: ExtractMode) -> str:
    """Convert a typed Chrome payload into one complete immutable text snapshot."""
    if mode is ExtractMode.READABILITY:
        return _extract_readability(payload.html, payload.final_url)
    if mode is ExtractMode.TEXT:
        if payload.text is None:
            raise ExtractionError("extension response is missing visible page text")
        return _format_page(_extract_title(payload.html), payload.final_url, payload.text)
    if mode is ExtractMode.RAW:
        return _format_page(_extract_title(payload.html), payload.final_url, payload.html)
    if payload.xhr is None:
        raise ExtractionError("extension response is missing XHR capture data")
    return _format_xhr(payload.final_url, payload.xhr)


def _extract_readability(html: str, final_url: str) -> str:
    """Run Readability and normalize its article text while preserving paragraphs."""
    try:
        document_type = cast(Any, Document)
        document = document_type(_strip_page_chrome(html), url=final_url)
        title = cast(str, document.short_title()).strip()
        summary = cast(str, document.summary(html_partial=True))
        parser = _TextParser()
        parser.feed(summary)
        text = "".join(parser.chunks)
    except Exception as error:
        raise ExtractionError(f"readability extraction failed: {error}") from error
    body = _clean_whitespace(text)
    if not body:
        raise ExtractionError(
            "readability returned no article text; retry with extract='text' for app-like pages"
        )
    return _format_page(title, final_url, body)


def _strip_page_chrome(html: str) -> str:
    """Remove semantic navigation/footer containers before Readability scoring."""
    lxml_html = cast(Any, import_module("lxml.html"))
    root = lxml_html.fromstring(html)
    for element in root.xpath("//nav | //footer | //aside"):
        element.drop_tree()
    return cast(str, lxml_html.tostring(root, encoding="unicode"))


def _format_page(title: str, final_url: str, body: str) -> str:
    """Render a compact title and final-URL envelope around extracted page content."""
    resolved_title = _clean_inline(title) or final_url
    return f"# {resolved_title}\nURL: {final_url}\n\n{body}"


def _format_xhr(final_url: str, entries: tuple[XhrEntry, ...]) -> str:
    """Render every captured textual XHR response into one pageable snapshot."""
    sections = [f"URL: {final_url}\nCaptured {len(entries)} fetch/XHR responses:"]
    for index, entry in enumerate(entries, start=1):
        mime = entry.mime or "?"
        body = entry.body if entry.body is not None else "(body unavailable)"
        sections.append(
            f"[{index}] {entry.kind} {entry.method} {entry.url} ({entry.status}, {mime})\n{body}"
        )
    return "\n\n".join(sections)


def _extract_title(html: str) -> str:
    """Extract and decode the rendered document title without failing malformed HTML."""
    try:
        parser = _TitleParser()
        parser.feed(html)
    except Exception:
        return ""
    return _clean_inline("".join(parser.chunks))


def _clean_inline(value: str) -> str:
    """Collapse all whitespace in a title to one display-safe line."""
    return " ".join(value.split())


def _clean_whitespace(value: str) -> str:
    """Collapse intra-line whitespace and repeated blank lines in article text."""
    output: list[str] = []
    blank = False
    for raw_line in value.splitlines():
        line = " ".join(raw_line.split())
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()
