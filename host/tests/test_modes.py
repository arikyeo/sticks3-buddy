"""Multi-host BLE modes: exclusive sel:false backoff, ondemand gate flow,
off isolation, live mode switching via IPC + CLI."""

import asyncio
import json
import sys
import types

import pytest

from buddy_bridge import cli
from buddy_bridge.audit import read_audit
from buddy_bridge.ble import link as link_mod
from buddy_bridge.ble.link import SLOW_RETRY_SECS, LinkManager
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.matchers import Matchers

SPEC_ACK = {
    "ack": "hello",
    "ok": True,
    "proto": 2,
    "data": {
        "caps": ["sessions", "ask", "rxack", "cancel"],
        "maxSessions": 6,
        "maxLine": 4608,
        "sel": True,
    },
}


def _sel(value: bool) -> dict:
    ack = json.loads(json.dumps(SPEC_ACK))
    ack["data"]["sel"] = value
    return ack


# ---- LinkManager: off isolation + live switching ----


async def _noop_line(obj):
    pass


class FakeClient:
    def __init__(self):
        self._connected = True
        self.disconnects = 0

    @property
    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self.disconnects += 1
        self._connected = False


async def test_link_off_mode_never_touches_bleak(monkeypatch):
    class Exploding(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError("bleak imported while mode=off")

    monkeypatch.setitem(sys.modules, "bleak", Exploding("bleak"))
    link = LinkManager(_noop_line, mode="off")
    task = asyncio.create_task(link.run())
    await asyncio.sleep(0.1)
    assert not task.done()  # idling, not crashed on the exploding module
    await link.stop()
    await task


async def test_link_set_mode_drop_matrix():
    # -> off: drop
    link = LinkManager(_noop_line, mode="exclusive")
    link._client = client = FakeClient()
    await link.set_mode("off")
    assert client.disconnects == 1 and link.mode == "off"

    # exclusive -> ondemand: drop (no persistent link in ondemand)
    link = LinkManager(_noop_line, mode="exclusive")
    link._client = client = FakeClient()
    await link.set_mode("ondemand")
    assert client.disconnects == 1

    # ondemand -> exclusive with a live connection: keep it
    link = LinkManager(_noop_line, mode="ondemand")
    link._client = client = FakeClient()
    await link.set_mode("exclusive")
    assert client.disconnects == 0 and client.is_connected

    # release(): disarm + drop
    link = LinkManager(_noop_line, mode="ondemand")
    link._want_evt.set()
    link._client = client = FakeClient()
    await link.release()
    assert client.disconnects == 1 and not link._want_evt.is_set()


def _fake_bleak(monkeypatch):
    """Install a fake bleak module: scanner always finds, client connects."""

    class FakeDevice:
        name = "ClaudeBuddy"
        address = "AA:BB:CC:DD:EE:FF"

    class FakeBleakScanner:
        @staticmethod
        async def find_device_by_filter(match, timeout):
            device = FakeDevice()
            adv = types.SimpleNamespace(local_name=device.name)
            return device if match(device, adv) else None

        @staticmethod
        async def find_device_by_address(address, timeout):
            return FakeDevice()

    class FakeBleakClient:
        instances = []

        def __init__(self, device):
            self._connected = False
            self.written = []
            self.mtu_size = 185
            type(self).instances.append(self)

        async def __aenter__(self):
            self._connected = True
            return self

        async def __aexit__(self, *exc):
            self._connected = False
            return False

        @property
        def is_connected(self):
            return self._connected

        async def start_notify(self, uuid, cb):
            self.notify_cb = cb

        async def write_gatt_char(self, uuid, chunk, response=False):
            self.written.append(bytes(chunk))

        async def disconnect(self):
            self._connected = False

    FakeBleakClient.instances = []
    module = types.ModuleType("bleak")
    module.BleakClient = FakeBleakClient
    module.BleakScanner = FakeBleakScanner
    monkeypatch.setitem(sys.modules, "bleak", module)
    return FakeBleakClient


async def test_link_ondemand_connects_on_demand_and_releases(monkeypatch):
    monkeypatch.setattr(link_mod, "CONNECTED_POLL_SECS", 0.02)
    monkeypatch.setattr(link_mod, "BACKOFF_BASE_SECS", 0.02)
    fake_client_cls = _fake_bleak(monkeypatch)
    connects = []

    async def on_connect():
        connects.append(1)

    link = LinkManager(_noop_line, mode="ondemand", on_connect=on_connect)
    task = asyncio.create_task(link.run())
    await asyncio.sleep(0.1)
    assert not link.connected  # idle until armed
    assert not fake_client_cls.instances

    assert await link.ensure_connected(timeout=2.0)
    assert link.connected and connects == [1]
    assert await link.send_line('{"cmd":"status"}')

    await link.release()
    for _ in range(100):
        if not link.connected:
            break
        await asyncio.sleep(0.02)
    assert not link.connected
    await asyncio.sleep(0.1)  # settles back to idle, no reconnect spam
    assert len(fake_client_cls.instances) == 1
    await link.stop()
    await task


# ---- daemon: sel:false host switching ----


class FakeLink:
    def __init__(self, connected=True, mode="exclusive"):
        self._connected = connected
        self.sent = []
        self.dropped = 0
        self.released = 0
        self.mode = mode
        self.backoff_floor = 0.0
        self.ensure_calls = []
        self.ensure_result = True

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def ensure_connected(self, timeout):
        self.ensure_calls.append(timeout)
        if self.ensure_result:
            self._connected = True
        return self.ensure_result

    async def drop_connection(self):
        self.dropped += 1
        self._connected = False

    async def release(self):
        self.released += 1
        self._connected = False

    async def set_mode(self, mode):
        self.mode = mode

    async def run(self):
        await asyncio.Event().wait()  # parks forever until cancelled

    async def stop(self):
        pass


async def test_sel_false_slow_retries_until_sel_flips(home):
    daemon = Daemon(load_config(home))
    daemon.link = FakeLink()
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_sel(False))
    assert daemon.device_sel is False
    assert daemon.link.backoff_floor == SLOW_RETRY_SECS
    assert daemon.link.dropped == 1

    # sel flips back on a later connect: floor cleared, normal cadence
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_sel(True))
    assert daemon.device_sel is True
    assert daemon.link.backoff_floor == 0.0


# ---- daemon: ondemand gate flow ----


def _req(command="make deploy", tuid="toolu_777"):
    return {
        "event": "permission_request",
        "agent": "claude",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_use_id": tuid,
        "tool_input": {"command": command},
    }


def _ondemand_daemon(home, connected=False):
    cfg = load_config(home)
    cfg.ble_mode = "ondemand"
    cfg.claude_decision = 30.0
    d = Daemon(cfg)
    d.link = FakeLink(connected=connected, mode="ondemand")
    d.matchers = Matchers(always_ask=("Bash:make *",))
    return d


async def test_ondemand_gate_decision_roundtrip(home):
    daemon = _ondemand_daemon(home)

    async def answer_when_prompted():
        for _ in range(100):
            if any('"prompt"' in line for line in daemon.link.sent):
                break
            await asyncio.sleep(0.01)
        # simulate the hello ack arriving right after connect
        await daemon._on_ble_line(SPEC_ACK)
        await daemon.handle_device_permission(
            {"cmd": "permission", "id": "toolu_777", "decision": "once"}
        )

    answering = asyncio.create_task(answer_when_prompted())
    resp = await daemon.handle_permission_request(_req())
    await answering
    assert resp == {"decision": "allow"}
    assert daemon.link.ensure_calls  # connected on demand
    assert daemon.link.released == 1  # and dropped afterwards
    (record,) = read_audit(home)
    assert record["decision"] == "allow"
    assert record["source"] == "device"


async def test_ondemand_connect_failure_fails_open(home):
    daemon = _ondemand_daemon(home)
    daemon.link.ensure_result = False
    resp = await daemon.handle_permission_request(_req())
    assert resp == {"decision": "ask"}
    (record,) = read_audit(home)
    assert record["source"] == "daemon_down"
    assert daemon.link.released == 0  # nothing to tear down


async def test_ondemand_timeout_cancels_prompt_and_releases(home):
    daemon = _ondemand_daemon(home)
    daemon.cfg.claude_decision = 10.05  # -> 0.05s device window
    # pre-negotiate v2 so the cancel cap is active for this gate
    daemon.link._connected = True
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    resp = await daemon.handle_permission_request(_req())
    assert resp == {"decision": "ask"}
    cancels = [json.loads(x) for x in daemon.link.sent if "prompt_cancel" in x]
    assert cancels == [{"cmd": "prompt_cancel", "id": "toolu_777"}]
    assert daemon.link.released == 1
    (record,) = read_audit(home)
    assert record["source"] == "timeout_fallback"


async def test_ondemand_unselected_device_fails_open(home):
    daemon = _ondemand_daemon(home)
    daemon.link._connected = True
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_sel(False))
    resp = await daemon.handle_permission_request(_req())
    assert resp == {"decision": "ask"}
    assert daemon.link.released >= 1
    (record,) = read_audit(home)
    assert record["source"] == "daemon_down"


async def test_ondemand_gates_serialized(home):
    daemon = _ondemand_daemon(home)
    daemon.cfg.claude_decision = 10.05
    order = []
    original = daemon.link.ensure_connected

    async def tracking_ensure(timeout):
        order.append("connect")
        return await original(timeout)

    daemon.link.ensure_connected = tracking_ensure
    r1, r2 = await asyncio.gather(
        daemon.handle_permission_request(_req(tuid="toolu_a")),
        daemon.handle_permission_request(_req(tuid="toolu_b")),
    )
    assert r1 == r2 == {"decision": "ask"}  # both timed out (nobody answered)
    assert order == ["connect", "connect"]  # strictly one gate at a time
    assert daemon.link.released == 2


# ---- daemon: off mode + live switching ----


async def test_off_mode_gate_fails_open_and_no_ble_task(home):
    cfg = load_config(home)
    cfg.ble_mode = "off"
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    daemon = Daemon(cfg)
    daemon.link = FakeLink(connected=False, mode="off")
    daemon.matchers = Matchers(always_ask=("Bash:make *",))

    run_task = asyncio.create_task(daemon.run())
    await asyncio.sleep(0.2)
    assert daemon._ble_task is None  # BLE tasks never start in off mode

    resp = await daemon.handle_permission_request(_req())
    assert resp == {"decision": "ask"}
    (record,) = read_audit(home)
    assert record["source"] == "daemon_down"

    # live switch via IPC: the ble task appears and the link mode flips
    reply = await daemon._on_ipc({"event": "set_mode", "mode": "exclusive"})
    assert reply == {"ok": True, "mode": "exclusive"}
    assert daemon.cfg.ble_mode == "exclusive"
    assert daemon.link.mode == "exclusive"
    assert daemon._ble_task is not None and not daemon._ble_task.done()

    daemon.request_stop()
    await run_task


async def test_set_mode_ipc_validation_and_switch(home):
    daemon = Daemon(load_config(home))
    daemon.link = FakeLink()
    assert (await daemon._on_ipc({"event": "set_mode", "mode": "warpspeed"}))["ok"] is False
    reply = await daemon._on_ipc({"event": "set_mode", "mode": "ondemand"})
    assert reply == {"ok": True, "mode": "ondemand"}
    assert daemon.cfg.ble_mode == "ondemand"
    assert daemon.link.mode == "ondemand"
    # cleanup: _apply_mode spawned a link task off the fake link
    if daemon._ble_task is not None:
        daemon._ble_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon._ble_task


# ---- CLI: mode persists + signals the live daemon ----


def test_cli_mode_applies_live(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True, "mode": payload["mode"]}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["mode", "ondemand"]) == 0
    assert seen == {"event": "set_mode", "mode": "ondemand"}
    assert "applied to the running daemon" in capsys.readouterr().out
    assert load_config(home).ble_mode == "ondemand"  # persisted too


def test_cli_mode_without_daemon(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    assert cli.main(["mode", "off"]) == 0
    assert "applies on next start" in capsys.readouterr().out
    assert load_config(home).ble_mode == "off"
