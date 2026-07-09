import asyncio
import json
import os

from buddy_bridge.ipc import client as ipc_client
from buddy_bridge.ipc.endpoint import write_endpoint
from buddy_bridge.ipc.server import IpcServer

TOKEN = "sekrit-token"


async def _start(handler, home):
    server = IpcServer(TOKEN, handler)
    port = await server.start()
    write_endpoint(home, port, TOKEN, os.getpid())
    return server


async def test_request_response(home):
    async def handler(msg):
        return {"ok": True, "echo": msg}

    server = await _start(handler, home)
    try:
        resp = await asyncio.to_thread(
            ipc_client.request, {"event": "status", "x": 1}, 2.0, home
        )
        assert resp == {"ok": True, "echo": {"event": "status", "x": 1}}
    finally:
        await server.stop()


async def test_fire_and_forget_event(home):
    received = []
    got = asyncio.Event()

    async def handler(msg):
        received.append(msg)
        got.set()
        return None

    server = await _start(handler, home)
    try:
        ok = await asyncio.to_thread(ipc_client.send_event, {"event": "hook", "n": 7}, home)
        assert ok is True
        await asyncio.wait_for(got.wait(), 2.0)
        assert received == [{"event": "hook", "n": 7}]
    finally:
        await server.stop()


async def test_bad_token_rejected(home):
    received = []

    async def handler(msg):
        received.append(msg)
        return {"ok": True}

    server = await _start(handler, home)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(json.dumps({"token": "WRONG", "event": "status"}).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 2.0)
        assert data == b""  # closed without a reply
        writer.close()
        await writer.wait_closed()
        assert received == []
    finally:
        await server.stop()


async def test_missing_token_rejected(home):
    received = []

    async def handler(msg):
        received.append(msg)
        return {"ok": True}

    server = await _start(handler, home)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b'{"event": "status"}\n')
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 2.0)
        assert data == b""
        writer.close()
        await writer.wait_closed()
        assert received == []
    finally:
        await server.stop()


async def test_token_serialized_first_on_wire():
    encoded = ipc_client._encode("tok123", {"event": "x"})
    assert encoded.startswith(b'{"token":"tok123",')


async def test_client_silent_noop_without_daemon(home):
    # no endpoint.json at all
    assert await asyncio.to_thread(ipc_client.send_event, {"event": "x"}, home) is False
    assert await asyncio.to_thread(ipc_client.request, {"event": "x"}, 0.2, home) is None
    # endpoint pointing at a closed port
    write_endpoint(home, 1, TOKEN, os.getpid())
    assert await asyncio.to_thread(ipc_client.send_event, {"event": "x"}, home) is False
    assert await asyncio.to_thread(ipc_client.request, {"event": "x"}, 0.2, home) is None


async def test_request_timeout_returns_none(home):
    async def handler(msg):
        await asyncio.sleep(5)
        return {"too": "late"}

    server = await _start(handler, home)
    try:
        resp = await asyncio.to_thread(ipc_client.request, {"event": "slow"}, 0.3, home)
        assert resp is None
    finally:
        await server.stop()
