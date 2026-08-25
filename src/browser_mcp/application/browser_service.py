"""Stage-gated application service for browser use cases."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from browser_mcp.bridge import BridgeManager
from browser_mcp.config import AppSettings
from browser_mcp.extraction import extract_content
from browser_mcp.models import (
    BrowserClickRequest,
    BrowserDialogRequest,
    BrowserFetchPayload,
    BrowserPressRequest,
    BrowserReadRequest,
    BrowserReadResult,
    BrowserScrollRequest,
    BrowserSelectRequest,
    BrowserSnapshotRequest,
    BrowserStatus,
    BrowserTypeRequest,
    BrowserVisualResult,
    SnapshotPageRequest,
)
from browser_mcp.security import PublicUrlPolicy
from browser_mcp.snapshot import SnapshotStore


class BrowserBridge(Protocol):
    """Application-facing lifecycle and status boundary for a browser bridge."""

    async def start(self) -> None:
        """Start bridge resources."""
        ...

    async def close(self) -> None:
        """Close bridge resources."""
        ...

    async def status(self) -> BrowserStatus:
        """Return live bridge diagnostics."""
        ...

    async def fetch(self, request: BrowserReadRequest) -> BrowserFetchPayload:
        """Return one raw rendered Chrome extraction."""
        ...

    async def request(
        self,
        message_type: str,
        action: str,
        args: dict[str, object],
        *,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Execute one allowlisted namespaced extension adapter action."""
        ...

    async def interact(self, action: str, args: dict[str, object]) -> BrowserVisualResult:
        """Execute one visual browser interaction and return the resulting page state."""
        ...


class BrowserService:
    """Own browser use cases without depending on the MCP transport layer."""

    def __init__(
        self,
        settings: AppSettings,
        bridge: BrowserBridge | None = None,
        url_policy: PublicUrlPolicy | None = None,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        """Create the service from validated process settings."""
        self._url_policy = url_policy or PublicUrlPolicy()
        self._bridge = bridge or BridgeManager(settings, self._url_policy)
        self._snapshots = snapshots or SnapshotStore()

    @property
    def gateway(self) -> BrowserBridge:
        """Expose the application browser port to isolated site adapters."""
        return self._bridge

    async def start(self) -> None:
        """Start the local bridge during the MCP server lifespan."""
        await self._bridge.start()

    async def close(self) -> None:
        """Release listener and connection resources during MCP shutdown."""
        await self._bridge.close()

    async def status(self) -> BrowserStatus:
        """Return live extension bridge installation and connection diagnostics."""
        return await self._bridge.status()

    async def read(self, request: BrowserReadRequest) -> BrowserReadResult:
        """Validate, fetch, extract, snapshot, and return the first bounded page."""
        original_url = str(request.url)
        payload = await self.fetch_payload(request)
        content = await asyncio.to_thread(extract_content, payload, request.extract)
        warnings = payload.warnings
        if payload.load_timed_out:
            warnings = (
                "Page did not reach complete within 30000ms; "
                "content is a rendered timeout snapshot.",
                *warnings,
            )
        return await self._snapshots.create(
            url=original_url,
            final_url=payload.final_url,
            extract_mode=request.extract,
            load_timed_out=payload.load_timed_out,
            warnings=warnings,
            content=content,
            max_chars=request.max_chars,
        )

    async def fetch_payload(self, request: BrowserReadRequest) -> BrowserFetchPayload:
        """Return a safety-validated raw payload for a site adapter without snapshot formatting."""
        await self._url_policy.validate(str(request.url))
        payload = await self._bridge.fetch(request)
        await self._url_policy.validate(payload.final_url)
        return payload

    async def read_page(self, request: SnapshotPageRequest) -> BrowserReadResult:
        """Return a later page from the same immutable extraction without network access."""
        return await self._snapshots.read(
            request.snapshot_id,
            request.offset,
            request.max_chars,
        )

    async def visual_snapshot(self, request: BrowserSnapshotRequest) -> BrowserVisualResult:
        """Open or observe the managed tab and return an agent-visible screenshot and elements."""
        args: dict[str, object] = {"wait_ms": request.wait_ms}
        if request.url is not None:
            url = str(request.url)
            await self._url_policy.validate(url)
            args["url"] = url
        return await self._interact("snapshot", args)

    async def click(self, request: BrowserClickRequest) -> BrowserVisualResult:
        """Click one referenced element or visual coordinate and return the new page state."""
        return await self._interact(
            "click",
            request.model_dump(exclude_none=True, mode="json"),
        )

    async def handle_dialog(self, request: BrowserDialogRequest) -> BrowserVisualResult:
        """Accept or dismiss one Chrome-native dialog and return a fresh visual state."""
        return await self._interact(
            "dialog",
            request.model_dump(exclude_none=True, mode="json"),
        )

    async def scroll(self, request: BrowserScrollRequest) -> BrowserVisualResult:
        """Scroll the managed tab relatively or to one referenced element."""
        return await self._interact("scroll", request.model_dump(exclude_none=True, mode="json"))

    async def type_text(self, request: BrowserTypeRequest) -> BrowserVisualResult:
        """Enter text into one referenced editable element and return the new page state."""
        return await self._interact("type", request.model_dump())

    async def press(self, request: BrowserPressRequest) -> BrowserVisualResult:
        """Press one bounded keyboard key in the managed tab."""
        return await self._interact("press", request.model_dump(exclude_none=True, mode="json"))

    async def select(self, request: BrowserSelectRequest) -> BrowserVisualResult:
        """Choose one native select option by value or label."""
        return await self._interact("select", request.model_dump())

    async def _interact(self, action: str, args: dict[str, object]) -> BrowserVisualResult:
        """Dispatch one interaction and reject any non-public resulting page URL."""
        result = await self._bridge.interact(action, args)
        await self._url_policy.validate(result.state.url)
        return result
