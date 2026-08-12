"""Small typed helpers shared by rendered-HTML site adapters."""

from __future__ import annotations

from importlib import import_module
from typing import Any, cast


def parse_rendered_html(source: str) -> Any:
    """Parse a rendered document with the lxml dependency already used by extraction."""
    lxml_html = cast(Any, import_module("lxml.html"))
    return lxml_html.fromstring(source)


def xpath_nodes(node: Any, expression: str) -> list[Any]:
    """Return only element-like values from one XPath evaluation."""
    values = cast(list[object], node.xpath(expression))
    return [value for value in values if hasattr(value, "xpath")]


def xpath_strings(node: Any, expression: str) -> list[str]:
    """Return string values from one XPath evaluation without object coercion."""
    values = cast(list[object], node.xpath(expression))
    return [value for value in values if isinstance(value, str)]


def first_node(node: Any, expression: str) -> Any | None:
    """Return the first element matching an XPath expression."""
    values = xpath_nodes(node, expression)
    return values[0] if values else None


def clean_text(node: Any | None) -> str:
    """Collapse rendered element text to one readable line."""
    if node is None:
        return ""
    return " ".join(str(node.text_content()).split())


def clean_block_text(node: Any | None) -> str:
    """Preserve paragraph boundaries while normalizing rendered element text."""
    if node is None:
        return ""
    raw = str(node.text_content())
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def attribute(node: Any | None, name: str) -> str:
    """Return one string HTML attribute or an empty value."""
    if node is None:
        return ""
    value = node.get(name)
    return value if isinstance(value, str) else ""


def integer_attribute(node: Any | None, name: str) -> int:
    """Parse one non-negative integer HTML attribute with a zero fallback."""
    try:
        return max(0, int(attribute(node, name)))
    except ValueError:
        return 0
