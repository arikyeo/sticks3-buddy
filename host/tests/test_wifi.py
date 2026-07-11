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


# ---- daemon: remove / clear / list ops ----


def _wifi_frames(daemon):
    return [json.loads(x) for x in daemon.link.sent if json.loads(x).get("cmd") == "wifi"]


async def test_wifi_remove_ipc_round_trip(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "HomeNet", "remove": True})
    )
    await asyncio.sleep(0.02)
    assert _wifi_frames(daemon) == [{"cmd": "wifi", "ssid": "HomeNet", "remove": True}]

    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 2})
    assert await task == {"ok": True, "n": 2}


async def test_wifi_remove_missing_ssid_rejected(home):
    daemon = _daemon(home)
    resp = await daemon._on_ipc({"event": "wifi", "remove": True})
    assert resp == {"ok": False, "error": "missing ssid"}


async def test_wifi_clear_ipc_round_trip(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(daemon._on_ipc({"event": "wifi", "clear": True}))
    await asyncio.sleep(0.02)
    assert _wifi_frames(daemon) == [{"cmd": "wifi", "clear": True}]

    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 0})
    assert await task == {"ok": True, "n": 0}


async def test_wifi_clear_and_list_do_not_require_ssid(home):
    # Exempted from "missing ssid": a disconnected link falls through to the
    # *next* check instead, proving neither op even looks for an ssid.
    daemon = _daemon(home)
    daemon.link = FakeLink(connected=False)
    assert await daemon._on_ipc({"event": "wifi", "clear": True}) == {
        "ok": False,
        "error": "device not connected",
    }
    assert await daemon._on_ipc({"event": "wifi", "list": True}) == {
        "ok": False,
        "error": "device not connected",
    }


async def test_wifi_list_ipc_round_trip(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(daemon._on_ipc({"event": "wifi", "list": True}))
    await asyncio.sleep(0.02)
    assert _wifi_frames(daemon) == [{"cmd": "wifi", "list": True}]

    await daemon._on_ble_line(
        {"ack": "wifi", "ok": True, "ssids": ["HomeNet", "Office"], "n": 2}
    )
    assert await task == {"ok": True, "ssids": ["HomeNet", "Office"], "n": 2}


# ---- daemon: ack passthrough (n, ssids, "full" error) ----


async def test_wifi_upsert_ack_n_propagates_to_ipc_response(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(daemon._on_ipc({"event": "wifi", "ssid": "HomeNet", "pass": "pw"}))
    await asyncio.sleep(0.02)
    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 5})
    assert await task == {"ok": True, "n": 5}


async def test_wifi_device_full_error_propagates(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "OneTooMany", "pass": "pw"})
    )
    await asyncio.sleep(0.02)
    await daemon._on_ble_line({"ack": "wifi", "ok": False, "error": "full", "n": 256})
    assert await task == {"ok": False, "error": "full", "n": 256}


# ---- daemon: _wifi_lock serializes ops (bulk-import safety) ----


async def test_wifi_lock_serializes_concurrent_ops(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(SPEC_ACK)
    task1 = asyncio.create_task(daemon._on_ipc({"event": "wifi", "ssid": "First", "pass": "pw1"}))
    await asyncio.sleep(0.02)
    task2 = asyncio.create_task(
        daemon._on_ipc({"event": "wifi", "ssid": "Second", "pass": "pw2"})
    )
    await asyncio.sleep(0.02)
    # Second call is blocked behind _wifi_lock: only the first frame went out.
    sent = _wifi_frames(daemon)
    assert len(sent) == 1
    assert sent[0]["ssid"] == "First"

    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 1})
    assert await task1 == {"ok": True, "n": 1}
    await asyncio.sleep(0.02)
    sent = _wifi_frames(daemon)
    assert len(sent) == 2
    assert sent[1]["ssid"] == "Second"

    await daemon._on_ble_line({"ack": "wifi", "ok": True, "n": 2})
    assert await task2 == {"ok": True, "n": 2}


# ---- CLI: --remove ----


def test_cli_wifi_remove_never_prompts_for_password(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    prompted = []
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: prompted.append(1) or "unused")
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True, "n": 1}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["wifi", "HomeNet", "--remove"]) == 0
    assert not prompted
    assert seen == {"event": "wifi", "ssid": "HomeNet", "remove": True}
    assert "removed 'HomeNet'" in capsys.readouterr().out


def test_cli_wifi_remove_requires_ssid(home, capsys):
    assert cli.main(["wifi", "--remove"]) == 1
    assert "ssid required" in capsys.readouterr().out


def test_cli_wifi_remove_without_daemon(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    assert cli.main(["wifi", "HomeNet", "--remove"]) == 1
    assert "not running" in capsys.readouterr().out


# ---- CLI: --list ----


def test_cli_wifi_list_never_prompts(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    prompted = []
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: prompted.append(1) or "unused")
    monkeypatch.setattr(
        cli.ipc_client, "request",
        lambda *a, **k: {"ok": True, "ssids": ["HomeNet", "Office"], "n": 2},
    )
    assert cli.main(["wifi", "--list"]) == 0
    assert not prompted
    out = capsys.readouterr().out
    assert "HomeNet" in out
    assert "Office" in out


def test_cli_wifi_list_empty(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda *a, **k: {"ok": True, "ssids": [], "n": 0}
    )
    assert cli.main(["wifi", "--list"]) == 0
    assert "no networks" in capsys.readouterr().out


def test_cli_wifi_list_ssid_conflict(home, capsys):
    assert cli.main(["wifi", "HomeNet", "--list"]) == 1
    assert "not used with" in capsys.readouterr().out


def test_cli_wifi_list_and_clear_mutually_exclusive(home, capsys):
    assert cli.main(["wifi", "--list", "--clear"]) == 1
    assert "mutually exclusive" in capsys.readouterr().out


def test_cli_wifi_no_ssid_no_flags_is_an_error(home, capsys):
    assert cli.main(["wifi"]) == 1
    assert "ssid required" in capsys.readouterr().out


# ---- CLI: --clear ----


def test_cli_wifi_clear_declined_sends_nothing(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    called = []
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda *a, **k: called.append(1) or {"ok": True}
    )
    assert cli.main(["wifi", "--clear"]) == 0
    assert not called
    assert "cancelled" in capsys.readouterr().out


def test_cli_wifi_clear_confirmed_sends_clear_frame(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    seen = {}

    def fake_request(payload, timeout, home=None):
        seen.update(payload)
        return {"ok": True, "n": 0}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["wifi", "--clear"]) == 0
    assert seen == {"event": "wifi", "clear": True}
    assert "cleared" in capsys.readouterr().out


def test_cli_wifi_clear_without_daemon_never_confirms(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    prompted = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: prompted.append(1) or "y")
    assert cli.main(["wifi", "--clear"]) == 1
    assert not prompted
    assert "not running" in capsys.readouterr().out


# ---- CLI: --import ----


def _write_json_import(tmp_path, entries, name="wifi-import.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps([{"ssid": s, "pass": p} for s, p in entries]), encoding="utf-8"
    )
    return path


def test_fmt_ssid_list_caps_long_lists():
    ssids = [f"Net{i}" for i in range(25)]
    formatted = cli._fmt_ssid_list(ssids)
    assert "'Net0'" in formatted
    assert "'Net19'" in formatted
    assert "Net24" not in formatted
    assert "+5 more" in formatted


def test_cli_wifi_import_sequential_order_and_success_summary(home, monkeypatch, capsys, tmp_path):
    path = _write_json_import(tmp_path, [("A", "pwA"), ("B", "pwB"), ("C", "pwC")])
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = []

    def fake_request(payload, timeout, home=None):
        seen.append(dict(payload))
        return {"ok": True, "n": len(seen)}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["wifi", "--import", str(path)]) == 0
    assert [p["ssid"] for p in seen] == ["A", "B", "C"]
    assert all(p["event"] == "wifi" for p in seen)
    out = capsys.readouterr().out
    assert "imported 3/3 networks" in out
    assert "device now has 3" in out


def test_cli_wifi_import_warns_when_file_exceeds_device_max(home, monkeypatch, capsys, tmp_path):
    from buddy_bridge.protocol import WIFI_MAX_ENTRIES

    entries = [(f"Net{i}", "pw") for i in range(WIFI_MAX_ENTRIES + 3)]
    path = _write_json_import(tmp_path, entries)
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda payload, timeout, home=None: {"ok": True, "n": 1}
    )
    assert cli.main(["wifi", "--import", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"file has {WIFI_MAX_ENTRIES + 3} networks" in out
    assert "at most 256" in out
    # the 3 entries past the device max (indices 256, 257, 258)
    assert "'Net256'" in out and "'Net257'" in out and "'Net258'" in out


def test_cli_wifi_import_stops_on_device_full(home, monkeypatch, capsys, tmp_path):
    path = _write_json_import(
        tmp_path, [("A", "pwA"), ("B", "pwB"), ("C", "pwC"), ("D", "pwD"), ("E", "pwE")]
    )
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    seen = []

    def fake_request(payload, timeout, home=None):
        seen.append(dict(payload))
        if len(seen) <= 2:
            return {"ok": True, "n": len(seen)}
        return {"ok": False, "error": "full", "n": 2}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    assert cli.main(["wifi", "--import", str(path)]) == 1
    # never hammered D or E once the device reported full
    assert [p["ssid"] for p in seen] == ["A", "B", "C"]
    out = capsys.readouterr().out
    assert "device full at 2 networks, 3 not imported" in out
    assert "'C'" in out and "'D'" in out and "'E'" in out
    assert "imported 2/5 networks" in out


def test_cli_wifi_import_daemon_down_mid_import(home, monkeypatch, capsys, tmp_path):
    path = _write_json_import(tmp_path, [("A", "pwA"), ("B", "pwB"), ("C", "pwC")])
    calls = {"n": 0}

    def fake_live(_home):
        calls["n"] += 1
        return {"port": 1, "token": "t"} if calls["n"] <= 2 else None

    monkeypatch.setattr(cli, "load_live_endpoint", fake_live)
    seen = []
    monkeypatch.setattr(
        cli.ipc_client,
        "request",
        lambda payload, timeout, home=None: seen.append(dict(payload)) or {"ok": True, "n": 1},
    )
    assert cli.main(["wifi", "--import", str(path)]) == 1
    assert [p["ssid"] for p in seen] == ["A"]  # B's request never reaches ipc_client
    out = capsys.readouterr().out
    assert "daemon stopped responding" in out
    assert "imported 1/3 networks" in out


def test_cli_wifi_import_no_daemon_never_starts_sending(home, monkeypatch, capsys, tmp_path):
    path = _write_json_import(tmp_path, [("A", "pwA")])
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: None)
    called = []
    monkeypatch.setattr(cli.ipc_client, "request", lambda *a, **k: called.append(1) or {"ok": True})
    assert cli.main(["wifi", "--import", str(path)]) == 1
    assert not called
    assert "not running" in capsys.readouterr().out


def test_cli_wifi_import_missing_file_is_clean_error(home, capsys, tmp_path):
    assert cli.main(["wifi", "--import", str(tmp_path / "nope.json")]) == 1
    assert "not found" in capsys.readouterr().out


def test_cli_wifi_import_empty_file_is_clean_error(home, capsys, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    assert cli.main(["wifi", "--import", str(path)]) == 1
    assert "empty" in capsys.readouterr().out


def test_cli_wifi_import_malformed_json_shape_is_clean_error(home, capsys, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("42", encoding="utf-8")
    assert cli.main(["wifi", "--import", str(path)]) == 1
    assert "JSON file must be" in capsys.readouterr().out


def test_cli_wifi_import_reminds_to_delete_file(home, monkeypatch, capsys, tmp_path):
    path = _write_json_import(tmp_path, [("A", "pwA")])
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})
    monkeypatch.setattr(
        cli.ipc_client, "request", lambda payload, timeout, home=None: {"ok": True, "n": 1}
    )
    assert cli.main(["wifi", "--import", str(path)]) == 0
    out = capsys.readouterr().out
    assert str(path) in out
    assert "plain text" in out


def test_cli_wifi_import_remove_flag_conflict(home, capsys, tmp_path):
    path = _write_json_import(tmp_path, [("A", "pwA")])
    assert cli.main(["wifi", "--import", str(path), "--remove"]) == 1
    assert "not used with" in capsys.readouterr().out


# ---- redaction: --import never prints/logs a password ----


def test_cli_wifi_import_never_prints_passwords(home, monkeypatch, capsys, tmp_path):
    secret_a, secret_b, secret_fail = "correct horse battery staple", "tr0ub4dor&3", "hunter2000!!"
    path = _write_json_import(tmp_path, [("A", secret_a), ("B", secret_b), ("C", secret_fail)])
    monkeypatch.setattr(cli, "load_live_endpoint", lambda _home: {"port": 1, "token": "t"})

    def fake_request(payload, timeout, home=None):
        if payload["ssid"] == "C":
            return {"ok": False, "error": "device rejected the network"}
        return {"ok": True, "n": 1}

    monkeypatch.setattr(cli.ipc_client, "request", fake_request)
    cli.main(["wifi", "--import", str(path)])

    out = capsys.readouterr().out
    for secret in (secret_a, secret_b, secret_fail):
        assert secret not in out
