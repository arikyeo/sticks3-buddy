"""WiFi/TCP device link: hello_link auth (relay signing scheme), newest-wins
replacement, accept rate limiting, daemon BLE-pause/resume arbitration, the
transport-blind holder flag, provisioning (daemon + CLI), and the
disabled-by-default zero-socket guarantee."""

import asyncio
import json
import socket
import time

import pytest

from buddy_bridge import cli
from buddy_bridge import daemon as daemon_mod
from buddy_bridge import link_tcp
from buddy_bridge import relay as relay_mod
from buddy_bridge.config import load_config, save_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.link_tcp import (
    AUTH_FAIL_LIMIT,
    AUTH_FAIL_WINDOW_SECS,
    DeviceLinkServer,
    build_hello_link,
    parse_hello_link,
)
from buddy_bridge.matchers import Matchers
from buddy_bridge.protocol import PROTO_V2, HelloAck
from buddy_bridge.relay import FRESH_WINDOW_SECS

TOKEN = "devlink-test-token"
MAC = "AA:BB:CC:DD:EE:FF"


# ---- hello_link frames (relay signing scheme, mac_sig field) ----


def test_hello_link_roundtrip():
    raw = build_hello_link(MAC, TOKEN)
    assert raw.endswith(b"\n")
    obj = parse_hello_link(raw.strip(), TOKEN)
    assert obj is not None
    assert obj["mac"] == MAC and obj["hello_link"] == 1 and "mac_sig" not in obj


def test_hello_link_rejects_bad_or_missing_signature():
    raw = build_hello_link(MAC, TOKEN).strip()
    obj = json.loads(raw)
    obj["mac_sig"] = "0" * 64
    assert parse_hello_link(json.dumps(obj).encode(), TOKEN) is None
    obj.pop("mac_sig")
    assert parse_hello_link(json.dumps(obj).encode(), TOKEN) is None


def test_hello_link_rejects_tampered_mac():
    raw = build_hello_link(MAC, TOKEN).strip()
    obj = json.loads(raw)
    obj["mac"] = "11:22:33:44:55:66"  # flip a field, keep the old signature
    assert parse_hello_link(json.dumps(obj).encode(), TOKEN) is None


def test_hello_link_rejects_wrong_or_empty_token():
    raw = build_hello_link(MAC, TOKEN).strip()
    assert parse_hello_link(raw, "other-token") is None
    assert parse_hello_link(raw, "") is None  # empty token can never verify


def test_hello_link_rejects_stale_or_future_ts():
    now = time.time()
    old = build_hello_link(MAC, TOKEN, ts=now - FRESH_WINDOW_SECS - 5).strip()
    assert parse_hello_link(old, TOKEN, now=now) is None
    future = build_hello_link(MAC, TOKEN, ts=now + FRESH_WINDOW_SECS + 5).strip()
    assert parse_hello_link(future, TOKEN, now=now) is None
    edge = build_hello_link(MAC, TOKEN, ts=now - FRESH_WINDOW_SECS + 1).strip()
    assert parse_hello_link(edge, TOKEN, now=now) is not None


def test_hello_link_rejects_garbage_shapes():
    assert parse_hello_link(b"not json", TOKEN) is None
    assert parse_hello_link(b"[1,2,3]", TOKEN) is None
    assert parse_hello_link(b'"a string"', TOKEN) is None
    # wrong magic
    payload = {"hello_link": 2, "mac": MAC, "ts": int(time.time())}
    payload["mac_sig"] = relay_mod._mac(payload, TOKEN)
    assert parse_hello_link(json.dumps(payload).encode(), TOKEN) is None
    # signed but MAC-less
    payload = {"hello_link": 1, "ts": int(time.time())}
    payload["mac_sig"] = relay_mod._mac(payload, TOKEN)
    assert parse_hello_link(json.dumps(payload).encode(), TOKEN) is None
    # oversized
    big = build_hello_link("x" * (link_tcp.MAX_LINE_BYTES + 1), TOKEN).strip()
    assert parse_hello_link(big, TOKEN) is None


# ---- DeviceLinkServer over in-memory streams (no sockets anywhere) ----


class FakeWriter:
    def __init__(self, ip="192.168.1.50"):
        self.ip = ip
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name):
        return (self.ip, 55555) if name == "peername" else None

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def lines(self):
        return [json.loads(line) for line in bytes(self.data).split(b"\n") if line]


def _server(*, token=TOKEN, port=48902, now_fn=None, on_line=None, on_up=None, on_down=None):
    async def _noop(obj):
        pass

    return DeviceLinkServer(
        port=port,
        token=token,
        on_line=on_line or _noop,
        on_up=on_up,
        on_down=on_down,
        now_fn=now_fn or time.time,
    )


async def _connect(server, *, raw=None, ip="192.168.1.50", ts=None, token=TOKEN, settle=0.02):
    """Drive one inbound connection; returns (reader, writer, task). The task
    keeps running (read loop) while the connection stays authenticated."""
    reader = asyncio.StreamReader()
    writer = FakeWriter(ip)
    reader.feed_data(raw if raw is not None else build_hello_link(MAC, token, ts=ts))
    task = asyncio.create_task(server._on_connection(reader, writer))
    await asyncio.sleep(settle)
    return reader, writer, task


async def _finish(reader, task):
    reader.feed_eof()
    await task


async def test_auth_accept_acks_and_fires_on_up():
    ups = []

    async def on_up():
        ups.append(1)

    server = _server(on_up=on_up)
    reader, writer, task = await _connect(server)
    assert server.connected is True
    assert writer.lines()[0] == {"ack": "hello_link", "ok": True}
    assert server.status() == {
        "active": True, "port": 48902, "connected": True, "device_mac": MAC,
    }
    assert ups == [1]
    await _finish(reader, task)
    assert server.connected is False


async def test_auth_reject_closes_without_reply():
    ups = []

    async def on_up():
        ups.append(1)

    server = _server(on_up=on_up)
    for raw in (
        build_hello_link(MAC, "wrong-token"),
        build_hello_link(MAC, TOKEN, ts=time.time() - FRESH_WINDOW_SECS - 10),
        b"garbage\n",
        json.dumps({"hello_link": 1, "mac": MAC, "ts": int(time.time())}).encode() + b"\n",
    ):
        reader, writer, task = await _connect(server, raw=raw)
        await task  # rejected connections end immediately
        assert writer.closed is True
        assert writer.data == bytearray()  # no ack, no oracle
    assert server.connected is False and ups == []


async def test_line_pipe_dispatches_inbound_json():
    seen = []

    async def on_line(obj):
        seen.append(obj)

    server = _server(on_line=on_line)
    reader, writer, task = await _connect(server)
    reader.feed_data(b'{"ack":"rx","n":3}\n')
    reader.feed_data(b"not json\n\n")  # dropped quietly, pipe survives
    reader.feed_data(b'{"cmd":"permission","id":"p1","decision":"once"}\n')
    await asyncio.sleep(0.02)
    assert seen == [
        {"ack": "rx", "n": 3},
        {"cmd": "permission", "id": "p1", "decision": "once"},
    ]
    assert await server.send_line('{"x":1}')
    assert writer.lines()[-1] == {"x": 1}
    await _finish(reader, task)
    assert await server.send_line('{"x":2}') is False  # nobody connected


async def test_newest_wins_replaces_previous_connection():
    ups, downs = [], []

    async def on_up():
        ups.append(1)

    async def on_down():
        downs.append(1)

    server = _server(on_up=on_up, on_down=on_down)
    reader_a, writer_a, task_a = await _connect(server)
    reader_b, writer_b, task_b = await _connect(server)
    assert writer_a.closed is True  # old closed with the newest taking over
    assert server.connected is True
    assert ups == [1, 1]  # hello renegotiation runs per-connection
    await _finish(reader_a, task_a)
    assert downs == []  # replaced != dropped: the link never went down
    assert await server.send_line('{"to":"b"}')
    assert writer_b.lines()[-1] == {"to": "b"}
    await _finish(reader_b, task_b)
    assert downs == [1] and server.connected is False


async def test_drop_connection_fires_on_down_stop_does_not():
    downs = []

    async def on_down():
        downs.append(1)

    server = _server(on_down=on_down)
    reader, writer, task = await _connect(server)
    await server.drop_connection()
    assert writer.closed is True
    await _finish(reader, task)
    assert downs == [1] and server.connected is False

    server2 = _server(on_down=on_down)
    reader2, writer2, task2 = await _connect(server2)
    await server2.stop()  # daemon shutdown: no resume callback wanted
    await _finish(reader2, task2)
    assert downs == [1]


async def test_auth_failures_rate_limit_that_ip_only():
    now = [1000.0]
    server = _server(now_fn=lambda: now[0])
    bad = build_hello_link(MAC, "wrong-token", ts=now[0])
    for _ in range(AUTH_FAIL_LIMIT):
        reader, writer, task = await _connect(server, raw=bad, ts=now[0])
        await task
    # 4th attempt from the same IP is shunned before auth, even with a VALID line
    reader, writer, task = await _connect(server, ts=now[0])
    await task
    assert writer.data == bytearray() and server.connected is False
    # a different IP is untouched
    reader2, writer2, task2 = await _connect(server, ts=now[0], ip="192.168.1.99")
    assert server.connected is True
    assert writer2.lines()[0] == {"ack": "hello_link", "ok": True}
    await _finish(reader2, task2)
    # window expiry forgives the shunned IP
    now[0] += AUTH_FAIL_WINDOW_SECS + 1
    reader3, writer3, task3 = await _connect(server, ts=now[0])
    assert server.connected is True
    await _finish(reader3, task3)


async def test_successful_auth_clears_failure_history():
    now = [1000.0]
    server = _server(now_fn=lambda: now[0])
    bad = build_hello_link(MAC, "wrong-token", ts=now[0])
    for _ in range(AUTH_FAIL_LIMIT - 1):
        reader, writer, task = await _connect(server, raw=bad, ts=now[0])
        await task
    reader, writer, task = await _connect(server, ts=now[0])  # valid: resets the slate
    await _finish(reader, task)
    reader2, writer2, task2 = await _connect(server, raw=bad, ts=now[0])
    await task2  # one new failure would hit the limit if history survived
    reader3, writer3, task3 = await _connect(server, ts=now[0])
    assert server.connected is True  # not shunned: history was cleared
    await _finish(reader3, task3)


# ---- daemon integration (FakeBleLink + in-memory device link) ----


class FakeBleLink:
    def __init__(self, connected=False):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0
        self.paused = False
        self.drops = 0
        self.wakes = 0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    def set_paused(self, paused):
        self.paused = bool(paused)

    async def drop_connection(self):
        self.drops += 1
        self._connected = False

    def wake(self):
        self.wakes += 1

    async def ensure_connected(self, timeout):
        return self._connected

    async def release(self):
        self._connected = False

    async def stop(self):
        pass


@pytest.fixture
def daemon(home):
    cfg = load_config(home)
    cfg.claude_decision = 0.3
    d = Daemon(cfg)
    d.link = FakeBleLink(connected=False)
    d.matchers = Matchers(always_ask=("Bash:make *",))
    return d


async def _wifi_connect(d, *, token=TOKEN, port=48902):
    """Attach a device-link server to the daemon and authenticate one
    connection through it (in-memory streams; nothing binds)."""
    server = DeviceLinkServer(
        port=port,
        token=token,
        on_line=d._on_ble_line,
        on_up=d._on_devlink_up,
        on_down=d._on_devlink_down,
    )
    d.devlink = server
    reader, writer, task = await _connect(server, token=token)
    return reader, writer, task


async def test_devlink_up_pauses_ble_and_hellos_over_wifi(daemon):
    daemon.link = FakeBleLink(connected=True)
    reader, writer, task = await _wifi_connect(daemon)
    assert daemon.link.paused is True
    assert daemon.link.drops == 1  # BLE connection released, radio freed
    assert daemon.link_transport == "wifi"
    kinds = writer.lines()
    assert kinds[0] == {"ack": "hello_link", "ok": True}
    assert kinds[1]["cmd"] == "hello"  # negotiation re-ran over the TCP pipe
    assert "time" in kinds[2]
    assert "total" in kinds[-1]  # first heartbeat
    assert not daemon.link.sent  # nothing leaked out over BLE
    await _finish(reader, task)


async def test_devlink_down_resumes_ble_reconnect(daemon):
    reader, writer, task = await _wifi_connect(daemon)
    assert daemon.link.paused is True
    await _finish(reader, task)
    assert daemon.link.paused is False
    assert daemon.link.wakes == 1
    assert daemon.link_transport == "none"


async def test_status_reports_transport_and_fw(daemon):
    assert daemon.status_payload()["link_transport"] == "none"
    assert daemon.status_payload()["device_link"] == {"active": False}
    daemon.link = FakeBleLink(connected=True)
    assert daemon.status_payload()["link_transport"] == "ble"
    reader, writer, task = await _wifi_connect(daemon)
    daemon.hello_ack = HelloAck(proto=PROTO_V2, fw="v9.9")
    payload = daemon.status_payload()
    assert payload["link_transport"] == "wifi"
    assert payload["device_fw"] == "v9.9"
    assert payload["device_link"] == {
        "active": True, "port": 48902, "connected": True, "device_mac": MAC,
    }
    await _finish(reader, task)


async def test_gate_round_trip_over_wifi(daemon, home):
    """The full session pipeline through the TCP pipe: prompt heartbeat out,
    stick decision in — BLE disconnected the whole time (even mode=off)."""
    from buddy_bridge.audit import read_audit

    daemon.cfg.ble_mode = "off"
    reader, writer, task = await _wifi_connect(daemon)
    gate = asyncio.create_task(
        daemon.handle_permission_request(
            {
                "event": "permission_request",
                "agent": "claude",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_use_id": "toolu_9",
                "tool_input": {"command": "make deploy"},
            }
        )
    )
    await asyncio.sleep(0.05)
    snap = [line for line in writer.lines() if "prompt" in line][-1]
    assert snap["prompt"]["id"] == "toolu_9" and snap["prompt"]["tool"] == "Bash"
    reader.feed_data(b'{"cmd":"permission","id":"toolu_9","decision":"once"}\n')
    assert (await gate) == {"decision": "allow"}
    (record,) = read_audit(home)
    assert record["decision"] == "allow" and record["source"] == "device"
    await _finish(reader, task)


async def test_holder_flag_follows_wifi_link(daemon, monkeypatch):
    """relay is_holder keys off the transport-blind connected state, so a
    wifi-held stick federates exactly like a BLE-held one."""

    class FakeTransport:
        def sendto(self, data, addr):
            pass

        def close(self):
            pass

    class FakeSock:
        def getsockname(self):
            return ("0.0.0.0", 48901)

    class FakeServer:
        sockets = [FakeSock()]

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_udp(mgr, port):
        return FakeTransport()

    async def fake_tcp(mgr, port):
        return FakeServer()

    monkeypatch.setattr(relay_mod, "_open_udp", fake_udp)
    monkeypatch.setattr(relay_mod, "_open_tcp", fake_tcp)
    daemon.cfg.relay_enabled = True
    daemon.cfg.relay_token = "relay-token"
    daemon.cfg.claude_enabled = False
    daemon.cfg.codex_enabled = False
    tasks = await daemon.start_background()
    try:
        assert daemon.relay is not None
        assert daemon.relay._is_holder() is False
        assert daemon.relay._discovery_payload()["holder"] is False
        reader, writer, task = await _wifi_connect(daemon)
        assert daemon.relay._is_holder() is True
        assert daemon.relay._discovery_payload()["holder"] is True
        await _finish(reader, task)
        assert daemon.relay._is_holder() is False
    finally:
        for t in tasks:
            t.cancel()
        await daemon.relay.stop()


async def test_ondemand_gate_skips_ble_while_wifi_up(daemon):
    """ondemand mode with the wifi link up gates over the persistent TCP
    pipe instead of arming a BLE connect."""
    daemon.cfg.ble_mode = "ondemand"
    reader, writer, task = await _wifi_connect(daemon)
    gate = asyncio.create_task(
        daemon.handle_permission_request(
            {
                "agent": "claude",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_use_id": "toolu_od",
                "tool_input": {"command": "make it"},
            }
        )
    )
    await asyncio.sleep(0.05)
    assert any("prompt" in line for line in writer.lines())
    reader.feed_data(b'{"cmd":"permission","id":"toolu_od","decision":"deny"}\n')
    assert (await gate) == {"decision": "deny"}
    await _finish(reader, task)


# ---- disabled by default: zero sockets ----


async def test_device_link_disabled_by_default_binds_nothing(home, monkeypatch):
    cfg = load_config(home)
    assert cfg.device_link_enabled is False and cfg.device_link_active is False
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)

    def explode(*args, **kwargs):
        raise AssertionError("socket touched while device_link disabled")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(asyncio, "start_server", explode)
    tasks = await d.start_background()
    assert tasks == [] and d.devlink is None


async def test_device_link_requires_nonempty_token(home, monkeypatch):
    cfg = load_config(home)
    cfg.device_link_enabled = True
    cfg.device_link_token = "   "
    assert cfg.device_link_active is False
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)

    def explode(*args, **kwargs):
        raise AssertionError("socket touched without a token")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(asyncio, "start_server", explode)
    tasks = await d.start_background()
    assert tasks == [] and d.devlink is None


async def test_device_link_bind_failure_disables_cleanly(home, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("address in use")

    monkeypatch.setattr(asyncio, "start_server", boom)
    cfg = load_config(home)
    cfg.device_link_enabled = True
    cfg.device_link_token = TOKEN
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)
    tasks = await d.start_background()
    assert tasks == [] and d.devlink is None


async def test_device_link_active_starts_listener(home, monkeypatch):
    started = []

    class FakeServer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_start_server(cb, host=None, port=None, limit=None):
        started.append((host, port))
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    cfg = load_config(home)
    cfg.device_link_enabled = True
    cfg.device_link_token = TOKEN
    cfg.device_link_port = 48123
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)
    tasks = await d.start_background()
    assert tasks == []
    assert d.devlink is not None and d.devlink.port == 48123 and d.devlink.token == TOKEN
    assert started == [("0.0.0.0", 48123)]


# ---- config ----


def test_config_device_link_parse_and_roundtrip(home):
    from buddy_bridge.config import to_toml

    cfg = load_config(home)
    assert cfg.device_link_enabled is False
    assert cfg.device_link_port == 48902
    assert cfg.device_link_token == ""
    cfg.device_link_enabled = True
    cfg.device_link_port = 50321
    cfg.device_link_token = "abc"
    save_config(cfg)
    again = load_config(home)
    assert again.device_link_enabled and again.device_link_port == 50321
    assert again.device_link_token == "abc" and again.device_link_active is True
    assert "[device_link]" in to_toml(again)
    # invalid port falls back to the default
    (home / "config.toml").write_text("[device_link]\nport = 99999\n", encoding="utf-8")
    assert load_config(home, create=False).device_link_port == 48902


# ---- daemon provisioning handler ----


async def test_handle_link_requires_connection_and_v2(daemon):
    resp = await daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    assert resp == {"ok": False, "error": "device not connected"}
    daemon.link = FakeBleLink(connected=True)  # connected but v1-only
    resp = await daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    assert resp["ok"] is False and "v1-only" in resp["error"]
    resp = await daemon.handle_link({"event": "link"})  # missing everything
    assert resp == {"ok": False, "error": "missing host/port/token"}


async def test_handle_link_provision_rides_ble_only(daemon):
    """The token must never traverse the plaintext TCP pipe: provisioning
    with only the wifi link up is refused."""
    daemon.negotiated_proto = PROTO_V2
    reader, writer, task = await _wifi_connect(daemon)
    resp = await daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    assert resp["ok"] is False and "encrypted BLE link" in resp["error"]
    assert TOKEN not in bytes(writer.data).decode()  # secret never hit the TCP pipe
    await _finish(reader, task)


async def test_handle_link_provisions_and_starts_listener(daemon, monkeypatch):
    class FakeServer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_start_server(cb, host=None, port=None, limit=None):
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    daemon.link = FakeBleLink(connected=True)
    daemon.negotiated_proto = PROTO_V2
    op = asyncio.create_task(
        daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    )
    await asyncio.sleep(0.02)
    frame = json.loads(daemon.link.sent[-1])  # went out over BLE
    assert frame == {"cmd": "link", "host": "10.0.0.5", "port": 48902, "token": TOKEN}
    await daemon._on_ble_line({"ack": "link", "ok": True})
    assert (await op) == {"ok": True}
    # listener live for exactly the provisioned endpoint + cfg mirrored
    assert daemon.devlink is not None and daemon.devlink.port == 48902
    assert daemon.devlink.token == TOKEN
    assert daemon.cfg.device_link_active is True
    # a second provision with the same endpoint reuses the listener
    first = daemon.devlink
    op2 = asyncio.create_task(
        daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    )
    await asyncio.sleep(0.02)
    await daemon._on_ble_line({"ack": "link", "ok": True})
    assert (await op2) == {"ok": True}
    assert daemon.devlink is first


async def test_handle_link_provision_pinned_to_ble_when_both_transports_live(daemon):
    """Defense in depth: even in a transient BLE+wifi both-connected state
    the token-carrying frame goes out over BLE, never the TCP pipe."""
    reader, writer, task = await _wifi_connect(daemon)
    daemon.link = FakeBleLink(connected=True)  # transient: BLE back while wifi up
    daemon.negotiated_proto = PROTO_V2
    op = asyncio.create_task(
        daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    )
    await asyncio.sleep(0.02)
    assert any('"cmd": "link"' in s or '"cmd":"link"' in s for s in daemon.link.sent)
    assert TOKEN not in bytes(writer.data).decode()  # nothing on the TCP pipe
    await daemon._on_ble_line({"ack": "link", "ok": True})
    assert (await op) == {"ok": True}
    await _finish(reader, task)


async def test_wifi_credentials_pinned_to_ble_when_both_transports_live(daemon):
    """WiFi passwords never traverse the device link: even with BLE and
    wifi transiently both live, the wifi frame goes out over BLE."""
    reader, writer, task = await _wifi_connect(daemon)
    daemon.link = FakeBleLink(connected=True)
    daemon.negotiated_proto = PROTO_V2
    op = asyncio.create_task(
        daemon.handle_wifi({"event": "wifi", "ssid": "HomeNet", "pass": "s3cret-pw"})
    )
    await asyncio.sleep(0.02)
    assert any("HomeNet" in s for s in daemon.link.sent)  # rode BLE
    assert "s3cret-pw" not in bytes(writer.data).decode()  # never the TCP pipe
    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 1})
    assert (await op) == {"ok": True, "n": 1}
    await _finish(reader, task)


async def test_handle_link_ack_timeout_fails_closed(daemon, monkeypatch):
    class FakeServer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_start_server(cb, host=None, port=None, limit=None):
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(daemon_mod, "LINK_ACK_TIMEOUT_SECS", 0.05)
    daemon.link = FakeBleLink(connected=True)
    daemon.negotiated_proto = PROTO_V2
    resp = await daemon.handle_link({"host": "10.0.0.5", "port": 48902, "token": TOKEN})
    assert resp == {"ok": False, "error": "device did not acknowledge within timeout"}


async def test_handle_link_clear_rides_active_transport(daemon):
    resp = await daemon.handle_link({"clear": True})
    assert resp == {"ok": False, "error": "device not connected"}
    reader, writer, task = await _wifi_connect(daemon)
    daemon.negotiated_proto = PROTO_V2
    op = asyncio.create_task(daemon.handle_link({"clear": True}))
    await asyncio.sleep(0.02)
    assert writer.lines()[-1] == {"cmd": "link", "clear": True}  # over the wifi pipe
    reader.feed_data(b'{"ack":"link","ok":true}\n')
    assert (await op) == {"ok": True}
    await _finish(reader, task)


# ---- CLI: link-provision ----


def test_cli_link_provision_without_daemon(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    assert cli.main(["link-provision"]) == 1
    assert "not running" in capsys.readouterr().out


def test_cli_link_provision_generates_persists_and_never_prints_token(
    home, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["link-provision", "--host", "10.0.0.5"]) == 0
    cfg = load_config(home, create=False)
    assert cfg.device_link_enabled is True  # persisted
    assert len(cfg.device_link_token) >= 32  # token_urlsafe(32)
    assert seen == {
        "event": "link", "host": "10.0.0.5", "port": 48902,
        "token": cfg.device_link_token,
    }
    out = capsys.readouterr().out
    assert cfg.device_link_token not in out  # config file only, never stdout
    assert "10.0.0.5:48902" in out
    assert "stick will use WiFi" in out


def test_cli_link_provision_keeps_existing_token(home, monkeypatch, capsys):
    cfg = load_config(home)
    cfg.device_link_token = "pre-existing-token"
    save_config(cfg)
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["link-provision", "--host", "10.0.0.5", "--port", "50000"]) == 0
    assert seen["token"] == "pre-existing-token"  # no regeneration
    assert seen["port"] == 50000
    again = load_config(home, create=False)
    assert again.device_link_token == "pre-existing-token"
    assert again.device_link_port == 50000
    assert "pre-existing-token" not in capsys.readouterr().out


def test_cli_link_provision_autodetects_host(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(link_tcp, "detect_host_ip", lambda: "192.168.7.7")
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["link-provision"]) == 0
    assert seen["host"] == "192.168.7.7"


def test_cli_link_provision_requires_host_when_autodetect_fails(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(link_tcp, "detect_host_ip", lambda: "")
    called = []
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda *a, **k: called.append(1) or {"ok": True}
    )
    assert cli.main(["link-provision"]) == 1
    assert not called  # nothing was sent
    assert "--host" in capsys.readouterr().out


def test_cli_link_provision_surfaces_daemon_error(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(
        cli.ipc_client, "request",
        lambda *a, **k: {"ok": False, "error": "device not connected"},
    )
    assert cli.main(["link-provision", "--host", "10.0.0.5"]) == 1
    assert "device not connected" in capsys.readouterr().out


def test_cli_link_provision_rejects_bad_port(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    called = []
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda *a, **k: called.append(1) or {"ok": True}
    )
    assert cli.main(["link-provision", "--host", "h", "--port", "99999"]) == 1
    assert not called
    assert "invalid --port" in capsys.readouterr().out


def test_cli_link_provision_clear(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["link-provision", "--clear"]) == 0
    assert seen == {"event": "link", "clear": True}
    assert "cleared" in capsys.readouterr().out
    assert cli.main(["link-provision", "--clear", "--host", "x"]) == 1  # exclusive


# ---- BLE LinkManager pause (arbitration primitive) ----


async def test_ble_link_manager_pause_parks_loop(monkeypatch):
    from buddy_bridge.ble.link import LinkManager

    async def noop(obj):
        pass

    lm = LinkManager(noop, mode="exclusive")

    async def explode(self):
        raise AssertionError("scan attempted while paused")

    monkeypatch.setattr(LinkManager, "_find_device", explode)
    lm.set_paused(True)
    task = asyncio.create_task(lm.run())
    await asyncio.sleep(0.05)
    assert not task.done()  # parked without touching the radio
    assert await lm.ensure_connected(0.01) is False  # ondemand arming refused
    await lm.stop()
    await task
