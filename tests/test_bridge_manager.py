"""Async protocol tests for the authenticated extension bridge."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from browser_mcp.bridge.manager import BRIDGE_PATH, BridgeManager
from browser_mcp.config import AppSettings
from browser_mcp.models import BrowserReadRequest
from tests.helpers import allow_public_url_policy, reserve_free_port

CHROME_EXTENSION_ORIGIN = Origin(f"chrome-extension://{'a' * 32}")


async def run_mock_extension(
    manager: BridgeManager,
    connected: asyncio.Event,
) -> None:
    """Authenticate like the MV3 worker and answer bridge ping requests."""
    installed = manager.installed
    if installed is None:
        raise RuntimeError("bridge must be started before the mock extension")
    status = await manager.status()
    if status.bridge_port is None:
        raise RuntimeError("bridge did not bind a port")
    uri = f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}"
    async with connect(uri, origin=CHROME_EXTENSION_ORIGIN) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "token": installed.token,
                    "version": "0.2.0-test",
                    "buildId": installed.build_id,
                    "extensionId": "a" * 32,
                    "userAgent": "Browser MCP test extension",
                    "port": status.bridge_port,
                }
            )
        )
        acknowledgement = json.loads(await websocket.recv())
        assert acknowledgement["type"] == "hello_ack"
        connected.set()
        await answer_pings(websocket)


async def answer_pings(websocket: ClientConnection) -> None:
    """Reply to every JSON ping until the server or test closes the socket."""
    async for raw_message in websocket:
        message = json.loads(raw_message)
        if message.get("type") == "ping":
            await websocket.send(json.dumps({"type": "pong", "id": message.get("id")}))


async def run_mock_fetch_extension(manager: BridgeManager, connected: asyncio.Event) -> None:
    """Emulate Chrome fetch plus an extension-initiated redirect policy callback."""
    installed = manager.installed
    if installed is None:
        raise RuntimeError("bridge must be started before the mock extension")
    status = await manager.status()
    if status.bridge_port is None:
        raise RuntimeError("bridge did not bind a port")
    uri = f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}"
    async with connect(uri, origin=CHROME_EXTENSION_ORIGIN) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "token": installed.token,
                    "version": "0.3.0-test",
                    "buildId": installed.build_id,
                }
            )
        )
        assert json.loads(await websocket.recv())["type"] == "hello_ack"
        connected.set()
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if message.get("type") == "ping":
                await websocket.send(json.dumps({"type": "pong", "id": message.get("id")}))
            elif message.get("type") == "browser.fetch":
                await websocket.send(
                    json.dumps(
                        {
                            "type": "browser.url_check",
                            "id": "redirect-check",
                            "url": "https://redirect.example/final",
                        }
                    )
                )
                approval = json.loads(await websocket.recv())
                assert approval == {
                    "type": "browser.url_check.result",
                    "id": "redirect-check",
                    "allowed": True,
                    "error": None,
                }
                await websocket.send(
                    json.dumps(
                        {
                            "type": "browser.fetch.result",
                            "id": message["id"],
                            "ok": True,
                            "data": {
                                "final_url": "https://redirect.example/final",
                                "html": "<html><title>Redirected</title></html>",
                                "text": "Rendered after redirect",
                                "load_timed_out": False,
                            },
                        }
                    )
                )


async def run_mock_site_extension(manager: BridgeManager, connected: asyncio.Event) -> None:
    """Emulate one namespaced website adapter response over the authenticated bridge."""
    installed = manager.installed
    if installed is None:
        raise RuntimeError("bridge must be started before the mock extension")
    status = await manager.status()
    if status.bridge_port is None:
        raise RuntimeError("bridge did not bind a port")
    uri = f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}"
    async with connect(uri, origin=CHROME_EXTENSION_ORIGIN) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "token": installed.token,
                    "version": "0.4.0-test",
                    "buildId": installed.build_id,
                }
            )
        )
        assert json.loads(await websocket.recv())["type"] == "hello_ack"
        connected.set()
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if message.get("type") == "ping":
                await websocket.send(json.dumps({"type": "pong", "id": message.get("id")}))
            elif message.get("type") == "xhs.fetch":
                assert message.get("action") == "search"
                await websocket.send(
                    json.dumps(
                        {
                            "type": "xhs.fetch.result",
                            "id": message["id"],
                            "ok": True,
                            "data": {"data": {"items": []}},
                        }
                    )
                )
            elif message.get("type") == "xhs.mutate":
                assert message.get("action") == "like"
                await websocket.send(
                    json.dumps(
                        {
                            "type": "xhs.mutate.result",
                            "id": message["id"],
                            "ok": True,
                            "data": {"active": True},
                        }
                    )
                )


async def run_mock_interaction_extension(manager: BridgeManager, connected: asyncio.Event) -> None:
    """Emulate one screenshot-bearing visual interaction response."""
    installed = manager.installed
    if installed is None:
        raise RuntimeError("bridge must be started before the mock extension")
    status = await manager.status()
    if status.bridge_port is None:
        raise RuntimeError("bridge did not bind a port")
    uri = f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}"
    async with connect(uri, origin=CHROME_EXTENSION_ORIGIN) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "token": installed.token,
                    "version": "0.8.0-test",
                    "buildId": installed.build_id,
                }
            )
        )
        assert json.loads(await websocket.recv())["type"] == "hello_ack"
        connected.set()
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if message.get("type") == "ping":
                await websocket.send(json.dumps({"type": "pong", "id": message.get("id")}))
            elif message.get("type") == "browser.interact":
                assert message.get("action") == "snapshot"
                await websocket.send(
                    json.dumps(
                        {
                            "type": "browser.interact.result",
                            "id": message["id"],
                            "ok": True,
                            "data": {
                                "state": {
                                    "action": "snapshot",
                                    "url": "https://example.com/",
                                    "title": "Example",
                                    "screenshot_mime_type": "image/jpeg",
                                    "viewport": {
                                        "width": 1280,
                                        "height": 720,
                                        "device_scale_factor": 2,
                                        "scroll_x": 0,
                                        "scroll_y": 0,
                                        "document_width": 1280,
                                        "document_height": 720,
                                    },
                                    "elements": [],
                                },
                                "screenshot_data": "/9j/2Q==",
                            },
                        }
                    )
                )


@pytest.mark.asyncio
async def test_authenticated_extension_round_trip_and_disconnect(tmp_path: Path) -> None:
    """Only a live ping/pong should produce connected=true."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    connected = asyncio.Event()
    extension_task = asyncio.create_task(run_mock_extension(manager, connected))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        live = await manager.status()

        assert live.state == "connected"
        assert live.connected is True
        assert live.extension_version == "0.2.0-test"
        installed = manager.installed
        assert installed is not None
        assert live.extension_build_id == installed.build_id
        assert live.last_seen_at is not None

        extension_task.cancel()
        await asyncio.gather(extension_task, return_exceptions=True)
        await asyncio.sleep(0.05)
        offline = await manager.status()
        assert offline.state == "disconnected"
        assert offline.connected is False
    finally:
        extension_task.cancel()
        await asyncio.gather(extension_task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_extension_can_reauthenticate_after_disconnect(tmp_path: Path) -> None:
    """A restarted MV3 worker should replace stale state and reconnect cleanly."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    first_connected = asyncio.Event()
    first_task = asyncio.create_task(run_mock_extension(manager, first_connected))
    second_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_connected.wait(), 2)
        assert (await manager.status()).connected is True
        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)

        second_connected = asyncio.Event()
        second_task = asyncio.create_task(run_mock_extension(manager, second_connected))
        await asyncio.wait_for(second_connected.wait(), 2)
        assert (await manager.status()).connected is True
    finally:
        first_task.cancel()
        if second_task is not None:
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_stale_server_does_not_reload_a_newer_shared_extension(tmp_path: Path) -> None:
    """An older live MCP process must not create a cross-process extension reload loop."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    installed = manager.installed
    assert installed is not None
    pairing_path = installed.directory / "pairing.json"
    pairing = json.loads(pairing_path.read_text(encoding="utf-8"))
    pairing["build_id"] = "newer-shared-build"
    pairing_path.write_text(json.dumps(pairing), encoding="utf-8")
    status = await manager.status()
    assert status.bridge_port is not None

    try:
        async with connect(
            f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}",
            origin=CHROME_EXTENSION_ORIGIN,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "token": installed.token,
                        "version": "0.5.0-test",
                        "buildId": "newer-shared-build",
                    }
                )
            )
            assert json.loads(await websocket.recv())["type"] == "hello_ack"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(websocket.recv(), 0.05)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_wrong_websocket_path_is_rejected(tmp_path: Path) -> None:
    """The shared localhost listener must reject clients outside the bridge path."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    status = await manager.status()
    assert status.bridge_port is not None
    try:
        async with connect(
            f"ws://127.0.0.1:{status.bridge_port}/wrong-path",
            origin=CHROME_EXTENSION_ORIGIN,
        ) as websocket:
            with pytest.raises(ConnectionClosed):
                await websocket.recv()
        assert (await manager.status()).connected is False
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_listener_falls_back_when_first_port_is_busy(tmp_path: Path) -> None:
    """A second client process should bind the next port without touching Robin's range."""
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    first_port = occupied.getsockname()[1]
    if first_port == 65_535:
        occupied.close()
        pytest.skip("kernel selected the final TCP port")
    second_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        second_probe.bind(("127.0.0.1", first_port + 1))
    except OSError:
        occupied.close()
        second_probe.close()
        pytest.skip("the adjacent TCP port isn't available on this host")
    second_probe.close()
    occupied.listen()

    settings = AppSettings(
        bridge_port=first_port,
        bridge_port_pool_size=2,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    try:
        await manager.start()
        status = await manager.status()
        assert status.bridge_port == first_port + 1
    finally:
        await manager.close()
        occupied.close()


@pytest.mark.asyncio
async def test_invalid_pairing_token_is_rejected(tmp_path: Path) -> None:
    """A localhost client without the generated token must not become active."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    status = await manager.status()
    assert status.bridge_port is not None
    uri = f"ws://127.0.0.1:{status.bridge_port}{BRIDGE_PATH}"
    try:
        async with connect(uri, origin=CHROME_EXTENSION_ORIGIN) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "token": "wrong-token",
                        "version": "attacker",
                        "buildId": "wrong-build",
                    }
                )
            )
            with pytest.raises(ConnectionClosed):
                await websocket.recv()

        rejected = await manager.status()
        assert rejected.connected is False
        assert rejected.extension_version is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_fetch_request_and_redirect_policy_round_trip(tmp_path: Path) -> None:
    """Fetch commands and extension-initiated redirect checks must share one socket safely."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings, allow_public_url_policy())
    await manager.start()
    connected = asyncio.Event()
    extension_task = asyncio.create_task(run_mock_fetch_extension(manager, connected))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        payload = await manager.fetch(
            BrowserReadRequest.model_validate(
                {"url": "https://example.com/start", "extract": "text"}
            )
        )

        assert payload.final_url == "https://redirect.example/final"
        assert payload.text == "Rendered after redirect"
    finally:
        extension_task.cancel()
        await asyncio.gather(extension_task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_namespaced_site_action_round_trip(tmp_path: Path) -> None:
    """Site actions must retain namespace and action while using normal request correlation."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    connected = asyncio.Event()
    extension_task = asyncio.create_task(run_mock_site_extension(manager, connected))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        payload = await manager.request(
            "xhs.fetch",
            "search",
            {"keyword": "MCP"},
        )
        mutation = await manager.request(
            "xhs.mutate",
            "like",
            {"noteId": "n1", "enabled": True},
        )

        assert payload == {"data": {"items": []}}
        assert mutation == {"active": True}
    finally:
        extension_task.cancel()
        await asyncio.gather(extension_task, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_visual_interaction_round_trip_validates_image_and_page_state(tmp_path: Path) -> None:
    """Interaction commands must retain action and validate the screenshot-bearing reply."""
    settings = AppSettings(
        bridge_port=reserve_free_port(),
        bridge_port_pool_size=1,
        data_dir=tmp_path,
    )
    manager = BridgeManager(settings)
    await manager.start()
    connected = asyncio.Event()
    extension_task = asyncio.create_task(run_mock_interaction_extension(manager, connected))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        result = await manager.interact("snapshot", {"url": "https://example.com/"})

        assert result.state.title == "Example"
        assert result.state.viewport.width == 1280
        assert result.screenshot_data == "/9j/2Q=="
    finally:
        extension_task.cancel()
        await asyncio.gather(extension_task, return_exceptions=True)
        await manager.close()
