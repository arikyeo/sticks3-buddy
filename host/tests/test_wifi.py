"""WiFi provisioning: {"cmd":"wifi",...} -> {"ack":"wifi","ok":...} IPC
round trip, v1-firmware/disconnected refusal, and password redaction
(never argv, never logged, never in audit.jsonl/cards.log)."""

import asyncio
import getpass
import json
import logging

from buddy_bridge import cli
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon

SPEC_ACK = {
    "ack": "hello",
    "ok": True,
    "proto": 2,
    "data": {
        "caps": ["sessions", "ask", "rxack", "cancel"],
        "maxSessions": 6,
        "maxLine": 4608,
        "sel": True,
        "fw": "2.3.1",
    },
}


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def stop(self):
        pass


def _daemon(home):
    d = Daemon(load_config(home))
    d.link = FakeLink()
    return d


# ---- daemon: IPC round trip + ack handling ----


async def test_wifi_ipc_round_trip_with_mocked_ack(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "HomeNet", "pass": "s3cr3t!"})
    )
    await asyncio.sleep(0.02)
    frames = [json.loads(x) for x in daemon.link.sent]
    wifi_frames = [f for f in frames if f.get("cmd") == "wifi"]
    assert wifi_frames == [{"cmd": "wifi", "ssid": "HomeNet", "pass": "s3cr3t!"}]

    await daemon._on_ble_line({"ack": "wifi", "ok": True})
    assert await task == {"ok": True}


async def test_wifi_device_declines(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "HomeNet", "pass": "wrong-pw"})
    )
    await asyncio.sleep(0.02)
    await daemon._on_ble_line({"ack": "wifi", "ok": False})
    assert await task == {"ok": False}


async def test_send_wifi_ack_timeout_returns_none(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    assert await daemon.send_wifi("Net", "pw", timeout=0.05) is None


async def test_wifi_missing_ssid_rejected(home):
    daemon = _daemon(home)
    resp = await daemon._on_ipc({"event": "wifi", "ssid": "", "pass": "pw"})
    assert resp == {"ok": False, "error": "missing ssid"}


# ---- refusal paths: not connected / v1 firmware ----


async def test_wifi_refused_when_not_connected(home):
    daemon = _daemon(home)
    daemon.link = FakeLink(connected=False)
    resp = await daemon._on_ipc({"event": "wifi", "ssid": "Net", "pass": "pw"})
    assert resp == {"ok": False, "error": "device not connected"}
    assert daemon.link.sent == []


async def test_wifi_refused_on_v1_firmware(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line({"ack": "hello", "ok": True, "proto": 1})
    assert not daemon.v2_active
    resp = await daemon._on_ipc({"event": "wifi", "ssid": "Net", "pass": "pw"})
    assert resp == {"ok": False, "error": "device firmware is v1-only (wifi needs protocol v2)"}
    assert not [x for x in daemon.link.sent if '"cmd":"wifi"' in x]


# ---- redaction: password never logged or audited ----


async def test_wifi_password_never_logged_or_audited(home, caplog):
    caplog.set_level(logging.DEBUG)
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    secret = "correct horse battery staple"
    task = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "HomeNet", "pass": secret})
    )
    await asyncio.sleep(0.02)
    await daemon._on_ble_line({"ack": "wifi", "ok": True})
    await task

    for record in caplog.records:
        assert secret not in record.getMessage()
    assert not (home / "audit.jsonl").exists()
    assert not (home / "cards.log").exists()


# ---- CLI: prompt-only password, clear errors, exit codes ----


def test_cli_wifi_without_daemon_never_prompts(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    prompted = []
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: prompted.append(1) or "unused")
    assert cli.main(["wifi", "HomeNet"]) == 1
    assert "not running" in capsys.readouterr().out
    assert not prompted  # nothing to send it to: never even asked


def test_cli_wifi_success_never_prints_password(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "sup3r-secret")
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["wifi", "HomeNet"]) == 0
    out = capsys.readouterr().out
    assert "sup3r-secret" not in out
    # the password DOES flow into the IPC payload (that's the point of the
    # feature) -- it's just never printed, logged, or persisted to disk.
    assert seen == {"event": "wifi", "ssid": "HomeNet", "pass": "sup3r-secret"}


def test_cli_wifi_surfaces_device_error_and_exits_1(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "pw")
    monkeypatch.setattr(
        cli.ipc_client,
        "request",
        lambda *a, **k: {
            "ok": False,
            "error": "device firmware is v1-only (wifi needs protocol v2)",
        },
    )
    assert cli.main(["wifi", "HomeNet"]) == 1
    out = capsys.readouterr().out
    assert "protocol v2" in out
    assert "pw" not in out
