"""Transport-independent request and result models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class ExtractMode(StrEnum):
    """Supported strategies for extracting a rendered webpage."""

    READABILITY = "readability"
    TEXT = "text"
    RAW = "raw"
    XHR = "xhr"


class BrowserStatus(BaseModel):
    """Connection and installation diagnostics returned to MCP clients."""

    state: Literal["not_started", "disconnected", "connected"]
    connected: bool
    bridge_port: int | None
    bridge_port_pool: tuple[int, int]
    extension_dir: str | None
    extension_version: str | None = None
    extension_build_id: str | None = None
    last_seen_at: datetime | None = None
    detail: str


class BrowserReadRequest(BaseModel):
    """Validated input for loading and extracting one public webpage."""

    url: HttpUrl
    extract: ExtractMode = ExtractMode.READABILITY
    wait_ms: int = Field(default=800, ge=0, le=30_000)
    max_chars: int = Field(default=10_000, ge=1, le=100_000)


class XhrEntry(BaseModel):
    """One text response captured from Chrome's Network domain."""

    model_config = ConfigDict(populate_by_name=True)

    url: str
    method: str = "GET"
    status: int = Field(default=0, ge=0, le=65_535)
    mime: str = ""
    kind: str = Field(default="XHR", alias="type")
    body: str | None = None


class BrowserFetchPayload(BaseModel):
    """Typed browser-side extraction returned before server-side formatting."""

    final_url: str
    html: str
    load_timed_out: bool = False
    text: str | None = None
    xhr: tuple[XhrEntry, ...] | None = None
    warnings: tuple[str, ...] = ()


class SnapshotPageRequest(BaseModel):
    """Validated input for reading one page from an immutable snapshot."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=10_000, ge=1, le=100_000)


class BrowserReadResult(BaseModel):
    """Stable result envelope shared by initial fetches and snapshot pages."""

    snapshot_id: str
    url: str
    final_url: str
    extract_mode: ExtractMode
    total_chars: int
    range_start: int
    range_end: int
    complete: bool
    next_offset: int | None
    load_timed_out: bool
    warnings: tuple[str, ...] = ()
    content: str


class BrowserElement(BaseModel):
    """One visible interactive element addressable by a short-lived page reference."""

    element_id: str
    tag: str
    role: str
    name: str
    input_type: str | None = None
    value: str | None = None
    href: str | None = None
    disabled: bool = False
    checked: bool | None = None
    selected: bool | None = None
    x: float
    y: float
    width: float
    height: float


class BrowserViewport(BaseModel):
    """Current CSS-pixel viewport and document scroll position."""

    width: int = Field(ge=1)
    height: int = Field(ge=1)
    device_scale_factor: float = Field(gt=0)
    scroll_x: int = Field(ge=0)
    scroll_y: int = Field(ge=0)
    document_width: int = Field(ge=1)
    document_height: int = Field(ge=1)


class BrowserPageState(BaseModel):
    """Agent-facing visual page state returned after snapshots and actions."""

    action: str
    url: str
    title: str
    screenshot_mime_type: Literal["image/jpeg", "image/png"]
    viewport: BrowserViewport
    elements: tuple[BrowserElement, ...] = ()
    visible_text: str = ""
    warnings: tuple[str, ...] = ()


class BrowserVisualResult(BaseModel):
    """Internal visual result containing public state plus MCP image payload data."""

    state: BrowserPageState
    screenshot_data: str = Field(repr=False, min_length=1)


class BrowserSnapshotRequest(BaseModel):
    """Validated request for opening or observing one interactive browser tab."""

    url: HttpUrl | None = None
    wait_ms: int = Field(default=500, ge=0, le=30_000)


class BrowserClickRequest(BaseModel):
    """Validated element-reference or viewport-coordinate click request."""

    element_id: str | None = Field(default=None, min_length=1, max_length=32)
    x: float | None = Field(default=None, ge=0)
    y: float | None = Field(default=None, ge=0)
    wait_ms: int = Field(default=500, ge=0, le=30_000)

    @model_validator(mode="after")
    def validate_target(self) -> BrowserClickRequest:
        """Require exactly one complete targeting strategy."""
        has_element = self.element_id is not None
        has_any_coordinate = self.x is not None or self.y is not None
        has_coordinates = self.x is not None and self.y is not None
        if has_element == has_coordinates and not (has_any_coordinate and not has_coordinates):
            raise ValueError("provide either element_id or both x and y")
        if has_any_coordinate and not has_coordinates:
            raise ValueError("x and y must be provided together")
        return self


class BrowserScrollDirection(StrEnum):
    """Supported relative page-scroll directions."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class BrowserScrollRequest(BaseModel):
    """Validated relative scroll or scroll-to-element request."""

    direction: BrowserScrollDirection = BrowserScrollDirection.DOWN
    amount: int = Field(default=600, ge=1, le=10_000)
    element_id: str | None = Field(default=None, min_length=1, max_length=32)
    wait_ms: int = Field(default=300, ge=0, le=30_000)


class BrowserTypeRequest(BaseModel):
    """Validated text entry request for an editable referenced element."""

    element_id: str = Field(min_length=1, max_length=32)
    text: str = Field(max_length=100_000)
    clear: bool = True
    submit: bool = False
    wait_ms: int = Field(default=300, ge=0, le=30_000)


class BrowserPressKey(StrEnum):
    """Bounded set of non-text keyboard keys exposed to agents."""

    ENTER = "Enter"
    ESCAPE = "Escape"
    TAB = "Tab"
    ARROW_UP = "ArrowUp"
    ARROW_DOWN = "ArrowDown"
    ARROW_LEFT = "ArrowLeft"
    ARROW_RIGHT = "ArrowRight"
    PAGE_UP = "PageUp"
    PAGE_DOWN = "PageDown"
    HOME = "Home"
    END = "End"
    BACKSPACE = "Backspace"
    DELETE = "Delete"
    SPACE = " "


class BrowserPressRequest(BaseModel):
    """Validated keyboard request optionally focused on one referenced element."""

    key: BrowserPressKey
    element_id: str | None = Field(default=None, min_length=1, max_length=32)
    wait_ms: int = Field(default=300, ge=0, le=30_000)


class BrowserSelectRequest(BaseModel):
    """Validated native-select request matched by option value or visible label."""

    element_id: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=10_000)
    wait_ms: int = Field(default=300, ge=0, le=30_000)
