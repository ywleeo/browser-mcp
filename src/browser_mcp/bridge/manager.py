"""Authenticated localhost WebSocket bridge for the Chrome extension."""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import logging
import os
import signal
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import uuid4

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from browser_mcp.bridge.bundle import ExtensionBundle, InstalledExtension
from browser_mcp.bridge.protocol import ConnectionMetadata, ExtensionHello
from browser_mcp.bridge.registry import PortRegistry
from browser_mcp.config import AppSettings
from browser_mcp.models import (
    BrowserFetchPayload,
    BrowserPageState,
    BrowserReadRequest,
    BrowserStatus,
    BrowserTabsResult,
    BrowserVisualResult,
)
from browser_mcp.process_lifecycle import resolve_owner_pid
from browser_mcp.security import PublicUrlPolicy, UrlPolicyError
from browser_mcp.upgrade import installation_metadata

LOGGER = logging.getLogger(__name__)
BRIDGE_PATH: Final = "/browser-mcp-extension"
HELLO_TIMEOUT_SECONDS: Final = 5.0
PROBE_TIMEOUT_SECONDS: Final = 1.5
KEEPALIVE_SECONDS: Final = 20.0
IDLE_CHECK_MIN_SECONDS: Final = 1.0
FETCH_TIMEOUT_SECONDS: Final = 65.0
INTERACTION_TIMEOUT_SECONDS: Final = 65.0
SHUTDOWN_NOTIFY_TIMEOUT_SECONDS: Final = 1.0
MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024
INTERACTION_ACTIONS: Final = frozenset(
    {"snapshot", "click", "dialog", "scroll", "type", "press", "select", "upload"}
)


class BridgeRequestError(RuntimeError):
    """Raised when the authenticated extension rejects or drops one request."""


class BridgeManager:
    """Own the extension bundle, listener, authenticated connection, and probes."""

    def __init__(self, settings: AppSettings, url_policy: PublicUrlPolicy | None = None) -> None:
        """Create a stopped bridge manager from validated application settings."""
        self._settings = settings
        self._bundle = ExtensionBundle(
            settings.data_dir,
            settings.extension_dir,
            settings.bridge_port,
            settings.bridge_port_pool_size,
            BRIDGE_PATH,
        )
        self._registry = PortRegistry(settings.data_dir)
        self._installed: InstalledExtension | None = None
        self._server: Server | None = None
        self._port: int | None = None
        self._connection: ServerConnection | None = None
        self._connection_metadata: ConnectionMetadata | None = None
        self._connection_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending_pings: dict[str, asyncio.Future[bool]] = {}
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._url_policy = url_policy or PublicUrlPolicy()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._installation = installation_metadata()

    @property
    def installed(self) -> InstalledExtension | None:
        """Return installation metadata after the first start."""
        return self._installed

    async def start(self) -> None:
        """Install the extension and bind one pooled port, reclaiming an abandoned one if needed."""
        async with self._start_lock:
            if self._server is not None:
                return
            self._installed = self._bundle.ensure_installed()
            bound = await self._bind_pooled_port()
            if bound is None:
                released = await asyncio.to_thread(
                    self._registry.reclaim,
                    self._settings.bridge_ports,
                    min_idle_seconds=self._settings.reclaim_idle_seconds,
                )
                if released:
                    LOGGER.info("bridge.reclaimed ports=%s", ",".join(str(p) for p in released))
                    bound = await self._bind_pooled_port()
            if bound is None:
                raise RuntimeError(self._pool_exhausted_message())
            server, port = bound
            self._server = server
            self._port = port
            self._registry.claim(
                port,
                owner_pid=resolve_owner_pid(),
                server_version=self._installation.server_version,
            )
            self._background_tasks = [
                asyncio.create_task(self._keepalive_loop(), name="browser-mcp-bridge-keepalive"),
                asyncio.create_task(self._idle_loop(), name="browser-mcp-bridge-idle"),
            ]
            LOGGER.info("bridge.listen port=%s", port)

    async def _bind_pooled_port(self) -> tuple[Server, int] | None:
        """Bind the first free pooled port, or report that every one of them is taken."""
        for port in self._settings.bridge_ports:
            try:
                server = await serve(
                    self._handle_connection,
                    "127.0.0.1",
                    port,
                    origins=[None, self._chrome_extension_origin_pattern()],
                    compression=None,
                    ping_interval=None,
                    max_size=MAX_MESSAGE_BYTES,
                    server_header=None,
                )
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                continue
            return server, port
        return None

    def _pool_exhausted_message(self) -> str:
        """Name the processes holding the pool so the failure is actionable without stderr."""
        first, last = self._settings.bridge_port_range
        return (
            f"all Browser MCP bridge ports are in use: {first}-{last}\n"
            f"{self._registry.describe(self._settings.bridge_ports)}\n"
            "Every port is held by a server that was used too recently to reclaim. Quit one of "
            "the MCP hosts listed above, or lower BROWSER_MCP_RECLAIM_IDLE_SECONDS."
        )

    async def close(self) -> None:
        """Close the active extension and listener, failing all pending probes."""
        tasks = self._background_tasks
        self._background_tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        async with self._connection_lock:
            connection = self._connection
            self._connection = None
            self._connection_metadata = None
        if connection is not None:
            try:
                async with self._send_lock:
                    await asyncio.wait_for(
                        connection.send(json.dumps({"type": "bridge.shutdown"})),
                        timeout=SHUTDOWN_NOTIFY_TIMEOUT_SECONDS,
                    )
            except (ConnectionClosed, TimeoutError):
                pass
            await connection.close(code=1001, reason="Browser MCP server stopping")
        self._fail_pending_pings()
        self._fail_pending_requests("Browser MCP server stopped before extension reply")

        server = self._server
        self._server = None
        self._port = None
        if server is not None:
            server.close()
            await server.wait_closed()
        self._registry.release()

    async def status(self) -> BrowserStatus:
        """Start the listener, round-trip probe Chrome, and return diagnostics."""
        await self.start()
        self._registry.touch()
        connected = await self._probe()
        metadata = self._connection_metadata
        installed = self._require_installed()
        if connected and metadata is not None:
            detail = f"Extension {metadata.version} connected and responded to ping."
            state = "connected"
        else:
            detail = (
                "Bridge is listening; load or reload the unpacked Chrome extension, "
                "then call browser_status again."
            )
            state = "disconnected"
        installation = self._installation
        return BrowserStatus(
            state=state,
            connected=connected,
            bridge_port=self._port,
            bridge_port_pool=self._settings.bridge_port_range,
            extension_dir=str(installed.directory),
            extension_version=metadata.version if metadata else None,
            extension_build_id=metadata.build_id if metadata else None,
            server_version=installation.server_version,
            install_mode=installation.install_mode,
            project_root=installation.project_root,
            source_commit=installation.source_commit,
            upgrade_check_command=installation.upgrade_check_command,
            upgrade_apply_command=installation.upgrade_apply_command,
            restart_instruction=installation.restart_instruction,
            last_seen_at=metadata.last_seen_at if metadata else None,
            detail=detail,
        )

    async def fetch(self, request: BrowserReadRequest) -> BrowserFetchPayload:
        """Load one URL through Chrome and validate the typed extension response."""
        data = await self._request(
            "browser.fetch",
            {
                "url": str(request.url),
                "extract": request.extract.value,
                "wait_ms": request.wait_ms,
            },
            timeout_seconds=FETCH_TIMEOUT_SECONDS,
        )
        return BrowserFetchPayload.model_validate(data)

    async def request(
        self,
        message_type: str,
        action: str,
        args: dict[str, object],
        *,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Send one allowlisted site adapter action through the authenticated extension."""
        if message_type not in {
            "bilibili.fetch",
            "douyin.fetch",
            "douyin.mutate",
            "zhihu.fetch",
            "xhs.fetch",
            "xhs.mutate",
        }:
            raise ValueError(f"unsupported extension action namespace: {message_type}")
        return await self._request(
            message_type,
            args,
            action=action,
            timeout_seconds=timeout_seconds,
        )

    async def list_tabs(self) -> BrowserTabsResult:
        """Return every open webpage tab the paired Chrome profile is showing."""
        data = await self._request("browser.tabs", {}, timeout_seconds=PROBE_TIMEOUT_SECONDS * 8)
        return BrowserTabsResult.model_validate(data)

    async def interact(self, action: str, args: dict[str, object]) -> BrowserVisualResult:
        """Execute one bounded action in the extension-managed interactive tab."""
        if action not in INTERACTION_ACTIONS:
            raise ValueError(f"unsupported browser interaction action: {action}")
        data = await self._request(
            "browser.interact",
            args,
            action=action,
            timeout_seconds=INTERACTION_TIMEOUT_SECONDS,
        )
        screenshot_data = data.get("screenshot_data")
        state = data.get("state")
        if not isinstance(screenshot_data, str) or not isinstance(state, dict):
            raise BridgeRequestError(
                "browser interaction reply is missing state or screenshot data"
            )
        return BrowserVisualResult(
            state=BrowserPageState.model_validate(state),
            screenshot_data=screenshot_data,
        )

    async def _handle_connection(self, connection: ServerConnection) -> None:
        """Authenticate one WebSocket and process its messages until disconnect."""
        request = connection.request
        if request is None or request.path != BRIDGE_PATH:
            await connection.close(code=1008, reason="invalid bridge path")
            return
        try:
            raw_hello = await asyncio.wait_for(connection.recv(), HELLO_TIMEOUT_SECONDS)
            hello = self._parse_hello(raw_hello)
            installed = self._require_installed()
            if not hmac.compare_digest(hello.token, installed.token):
                await connection.close(code=1008, reason="authentication failed")
                LOGGER.warning("bridge.auth_failed")
                return

            now = datetime.now(UTC)
            metadata = ConnectionMetadata(
                connected_at=now,
                last_seen_at=now,
                version=hello.version,
                build_id=hello.build_id,
                extension_id=hello.extension_id,
                user_agent=hello.user_agent,
            )
            previous = await self._replace_connection(connection, metadata)
            if previous is not None and previous is not connection:
                await previous.close(code=1000, reason="new extension connection")
            await connection.send(json.dumps({"type": "hello_ack", "port": self._port}))
            if hello.build_id != installed.build_id and self._owns_current_bundle(installed):
                await connection.send(json.dumps({"type": "reload"}))
            LOGGER.info("bridge.authenticated version=%s", hello.version)

            async for raw_message in connection:
                await self._handle_message(raw_message, connection)
        except (TimeoutError, ValidationError, ValueError, TypeError) as error:
            LOGGER.warning("bridge.invalid_handshake error=%s", error)
            await connection.close(code=1008, reason="invalid handshake")
        except ConnectionClosed:
            pass
        finally:
            await self._clear_connection(connection)

    async def _handle_message(self, raw_message: str | bytes, connection: ServerConnection) -> None:
        """Route authenticated extension messages to their pending request."""
        if isinstance(raw_message, bytes):
            return
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        typed_message = cast(dict[str, object], message)
        await self._touch_connection(connection)
        message_type = typed_message.get("type")
        if message_type == "pong":
            request_id = typed_message.get("id")
            if isinstance(request_id, str):
                future = self._pending_pings.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(True)
        elif message_type in {
            "bilibili.fetch.result",
            "browser.fetch.result",
            "browser.interact.result",
            "browser.tabs.result",
            "douyin.fetch.result",
            "douyin.mutate.result",
            "zhihu.fetch.result",
            "xhs.fetch.result",
            "xhs.mutate.result",
        }:
            self._resolve_request(typed_message)
        elif message_type == "browser.url_check":
            await self._answer_url_check(typed_message, connection)

    async def _request(
        self,
        message_type: str,
        args: dict[str, object],
        *,
        action: str | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Send one id-correlated command and fail promptly on timeout or disconnect."""
        await self.start()
        self._registry.touch()
        async with self._connection_lock:
            connection = self._connection
        if connection is None:
            raise BridgeRequestError(
                "Chrome extension is not connected; call browser_status and reload the extension"
            )
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        payload = {
            "type": message_type,
            "id": request_id,
            "tab_id": "default",
            "args": args,
        }
        if action is not None:
            payload["action"] = action
        try:
            async with self._send_lock:
                await connection.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout_seconds)
        except ConnectionClosed as error:
            raise BridgeRequestError("extension connection closed while sending request") from error
        except TimeoutError as error:
            raise BridgeRequestError(
                f"timed out after {timeout_seconds:g}s waiting for extension reply"
            ) from error
        finally:
            self._pending_requests.pop(request_id, None)

    def _resolve_request(self, message: dict[str, object]) -> None:
        """Resolve exactly one pending command from a typed extension result message."""
        request_id = message.get("id")
        if not isinstance(request_id, str):
            return
        future = self._pending_requests.pop(request_id, None)
        if future is None or future.done():
            return
        if message.get("ok") is not True:
            error = message.get("error")
            future.set_exception(
                BridgeRequestError(error if isinstance(error, str) else "unknown extension error")
            )
            return
        data = message.get("data")
        if not isinstance(data, dict):
            future.set_exception(BridgeRequestError("extension reply is missing object data"))
            return
        future.set_result(cast(dict[str, Any], data))

    async def _answer_url_check(
        self, message: dict[str, object], connection: ServerConnection
    ) -> None:
        """Approve or block each top-level Chrome navigation, including redirects."""
        request_id = message.get("id")
        url = message.get("url")
        if not isinstance(request_id, str):
            return
        allowed = False
        error: str | None = None
        if not isinstance(url, str):
            error = "URL check is missing a URL"
        else:
            try:
                await self._url_policy.validate(url)
                allowed = True
            except UrlPolicyError as policy_error:
                error = str(policy_error)
                LOGGER.warning("bridge.url_blocked reason=%s", policy_error)
        response = {
            "type": "browser.url_check.result",
            "id": request_id,
            "allowed": allowed,
            "error": error,
        }
        try:
            async with self._send_lock:
                await connection.send(json.dumps(response))
        except ConnectionClosed:
            pass

    async def _probe(self) -> bool:
        """Require a live ping/pong round trip instead of trusting socket presence."""
        async with self._connection_lock:
            connection = self._connection
        if connection is None:
            return False
        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_pings[request_id] = future
        try:
            async with self._send_lock:
                await connection.send(json.dumps({"type": "ping", "id": request_id}))
            return await asyncio.wait_for(future, PROBE_TIMEOUT_SECONDS)
        except (ConnectionClosed, TimeoutError):
            return False
        finally:
            self._pending_pings.pop(request_id, None)

    async def _keepalive_loop(self) -> None:
        """Keep MV3 service workers awake and detect stale local sockets."""
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                await self._probe()
        except asyncio.CancelledError:
            raise

    async def _idle_loop(self) -> None:
        """Poll for disuse on its own cadence so a short idle window still retires promptly."""
        timeout = self._settings.idle_timeout_seconds
        if timeout <= 0:
            return
        interval = min(KEEPALIVE_SECONDS, max(IDLE_CHECK_MIN_SECONDS, timeout / 4))
        try:
            while True:
                await asyncio.sleep(interval)
                self._retire_when_idle()
        except asyncio.CancelledError:
            raise

    def _retire_when_idle(self) -> None:
        """Return the pooled port to the pool once no MCP request has arrived for long enough.

        The extension reconnects to every pooled port on its own, so socket presence
        says nothing about whether this server is still wanted; only real MCP traffic
        does. Retiring stops at SIGTERM rather than closing the listener, because the
        stdio transport must end with the process for the host to notice.
        """
        timeout = self._settings.idle_timeout_seconds
        if timeout <= 0:
            return
        idle = self._registry.idle_seconds()
        if idle is None or idle < timeout:
            return
        LOGGER.warning(
            "bridge.idle_retire port=%s idle=%.0fs; releasing the pooled port",
            self._port,
            idle,
        )
        # SIGTERM ends the process outright, so the lifespan teardown that normally
        # returns the lease never runs. Hand the port back first and leave nothing
        # for the next server to clean up.
        self._registry.release()
        os.kill(os.getpid(), signal.SIGTERM)

    async def _replace_connection(
        self, connection: ServerConnection, metadata: ConnectionMetadata
    ) -> ServerConnection | None:
        """Atomically make a newly authenticated socket the active connection."""
        async with self._connection_lock:
            previous = self._connection
            self._connection = connection
            self._connection_metadata = metadata
            if previous is not None and previous is not connection:
                self._fail_pending_requests("extension connection was replaced before reply")
            return previous

    async def _clear_connection(self, connection: ServerConnection) -> None:
        """Clear state only when the disconnecting socket is still active."""
        async with self._connection_lock:
            if self._connection is not connection:
                return
            self._connection = None
            self._connection_metadata = None
        self._fail_pending_pings()
        self._fail_pending_requests("extension connection closed before reply")
        LOGGER.info("bridge.disconnected")

    async def _touch_connection(self, connection: ServerConnection) -> None:
        """Record activity without allowing an obsolete socket to update state."""
        async with self._connection_lock:
            metadata = self._connection_metadata
            if self._connection is not connection or metadata is None:
                return
            self._connection_metadata = ConnectionMetadata(
                connected_at=metadata.connected_at,
                last_seen_at=datetime.now(UTC),
                version=metadata.version,
                build_id=metadata.build_id,
                extension_id=metadata.extension_id,
                user_agent=metadata.user_agent,
            )

    def _fail_pending_pings(self) -> None:
        """Resolve all pending probes false when the active socket disappears."""
        pending = list(self._pending_pings.values())
        self._pending_pings.clear()
        for future in pending:
            if not future.done():
                future.set_result(False)

    def _fail_pending_requests(self, reason: str) -> None:
        """Fail every command awaiting a response from a socket that is no longer usable."""
        pending = list(self._pending_requests.values())
        self._pending_requests.clear()
        for future in pending:
            if not future.done():
                future.set_exception(BridgeRequestError(reason))

    def _require_installed(self) -> InstalledExtension:
        """Return installed bundle metadata or fail on an internal lifecycle bug."""
        if self._installed is None:
            raise RuntimeError("extension bundle is not installed")
        return self._installed

    @staticmethod
    def _owns_current_bundle(installed: InstalledExtension) -> bool:
        """Avoid reload storms when older MCP processes share one unpacked directory."""
        try:
            value: Any = json.loads(
                (installed.directory / "pairing.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        if not isinstance(value, dict):
            return False
        pairing = cast(dict[str, object], value)
        return pairing.get("build_id") == installed.build_id

    @staticmethod
    def _parse_hello(raw_hello: str | bytes) -> ExtensionHello:
        """Parse and validate the mandatory first client message."""
        if isinstance(raw_hello, bytes):
            raise TypeError("binary hello messages are not supported")
        value: Any = json.loads(raw_hello)
        hello = ExtensionHello.model_validate(value)
        if hello.type != "hello":
            raise ValueError("first message must be hello")
        return hello

    @staticmethod
    def _chrome_extension_origin_pattern() -> Any:
        """Return a compiled pattern accepted by websockets' origin policy."""
        import re

        return re.compile(r"chrome-extension://[a-p]{32}")
