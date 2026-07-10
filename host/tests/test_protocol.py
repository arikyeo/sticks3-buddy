import json

import pytest

from buddy_bridge import protocol
from buddy_bridge.protocol import (
    LINE_BUDGET,
    build_owner_frame,
    build_snapshot,
    build_status_poll,
    build_time_frame,
    format_entry,
    sanitize,
    truncate_utf8_bytes,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("em—dash", "em-dash"),
        ("en–dash", "en-dash"),
        ("‘curly’", "'curly'"),
        ("“double”", '"double"'),
        ("wait…", "wait..."),
        ("nb sp", "nb sp"),
        ("日本語", "???"),  # CJK -> ?
        ("café", "café"),  # latin-1 preserved
        ("a\nb\tc", "a b c"),  # newlines/tabs -> space
        ("bell\x07", "bell"),  # other control chars dropped
        ("x → y", "x -> y"),
        (123, "123"),  # non-str coerced
    ],
)
def test_sanitize_table(raw, expected):
    assert sanitize(raw) == expected


def test_truncate_utf8_bytes_ascii():
    assert truncate_utf8_bytes("hello", 10) == "hello"
    assert truncate_utf8_bytes("hello", 3) == "hel"
    assert truncate_utf8_bytes("hello", 0) == ""


def test_truncate_utf8_bytes_multibyte_boundary():
    s = "cafés"  # e-acute = 2 bytes -> "café" is 5 bytes
    assert truncate_utf8_bytes(s, 4) == "caf"  # cannot split the é
    assert truncate_utf8_bytes(s, 5) == "café"
    cjk = "日本語"  # 3 bytes each
    assert truncate_utf8_bytes(cjk, 7) == "日本"
    assert truncate_utf8_bytes(cjk, 2) == ""


def test_snapshot_basic_shape():
    line = build_snapshot(
        total=3, running=1, waiting=2, msg="hello",
        entries=["10:00 first", "10:01 second"],
        tokens=1234, tokens_today=99,
        prompt={"id": "req_abc123", "tool": "Bash", "hint": "git status"},
    )
    snap = json.loads(line)
    assert snap == {
        "total": 3, "running": 1, "waiting": 2, "msg": "hello",
        "entries": ["10:00 first", "10:01 second"],
        "tokens": 1234, "tokens_today": 99,
        "prompt": {"id": "req_abc123", "tool": "Bash", "hint": "git status"},
    }
    assert len(line.encode("utf-8")) <= LINE_BUDGET
    assert line.encode("ascii")  # pure-ASCII wire


def test_snapshot_omits_prompt_when_none():
    snap = json.loads(build_snapshot(total=0, running=0, waiting=0))
    assert "prompt" not in snap


def test_cascade_drops_oldest_entries_first():
    entries = [f"10:{i:02d} entry-{i} " + "x" * 70 for i in range(16)]
    line = build_snapshot(
        total=16, running=16, waiting=0, msg="m", entries=entries,
        tokens=0, tokens_today=0,
    )
    snap = json.loads(line)
    assert len(line.encode()) <= LINE_BUDGET
    kept = snap["entries"]
    assert 0 < len(kept) < 16
    # newest entries survive, oldest are gone
    assert kept == entries[16 - len(kept):]
    assert snap["msg"] == "m"  # msg untouched while entry-dropping suffices


def test_cascade_shrinks_prompt_after_entries_gone():
    # no entries -> the prompt-shrink stage is exercised directly
    line = build_snapshot(
        total=1, running=0, waiting=1, msg="m", entries=[],
        tokens=10, tokens_today=10,
        prompt={"id": "p1", "tool": "Bash", "hint": "P" * 600}, budget=400,
    )
    snap = json.loads(line)
    assert len(line.encode()) <= 400
    assert len(snap["prompt"]["hint"]) == 96  # first shrink step suffices
    assert snap["prompt"]["id"] == "p1"  # id survives shrinking


def test_cascade_drops_prompt_then_shrinks_msg():
    # msg so big the prompt cannot be saved: prompt dropped, msg shrunk
    line = build_snapshot(
        total=1, running=0, waiting=1, msg="x" * 800, entries=[],
        tokens=0, tokens_today=0,
        prompt={"id": "p1", "tool": "Bash", "hint": "q" * 2000}, budget=200,
    )
    snap = json.loads(line)
    assert len(line.encode()) <= 200
    assert "prompt" not in snap
    assert 0 < len(snap["msg"]) <= 96


def test_cascade_fuzz_16_cjk_sessions_fits_budget():
    entries = [
        f"12:{i:02d} " + "会話のタイトル長い" * 10
        for i in range(16)
    ]
    line = build_snapshot(
        total=16, running=5, waiting=11,
        msg="中文状態消息" * 8,
        entries=entries,
        tokens=123456789, tokens_today=987654,
        prompt={"id": "req_1", "tool": "Bash", "hint": "危険なコマンド rm -rf /" * 6},
    )
    assert len(line.encode("utf-8")) <= LINE_BUDGET
    snap = json.loads(line)
    assert snap["total"] == 16
    assert snap["waiting"] == 11
    assert snap["tokens"] == 123456789
    line.encode("ascii")  # sanitized to ASCII-safe wire


def test_time_frame():
    line = build_time_frame(ts=1_700_000_000)
    frame = json.loads(line)
    assert frame["time"][0] == 1_700_000_000
    assert isinstance(frame["time"][1], int)
    assert -14 * 3600 <= frame["time"][1] <= 14 * 3600


def test_owner_frame():
    assert json.loads(build_owner_frame("Ar—ik"))["owner"] == "Ar-ik"
    long_owner = json.loads(build_owner_frame("O" * 100))["owner"]
    assert len(long_owner.encode()) <= 32


def test_status_poll():
    assert json.loads(build_status_poll()) == {"cmd": "status"}


def test_format_entry():
    assert format_entry("09:05", "did a thing") == "09:05 did a thing"
    long = format_entry("09:05", "y" * 200)
    assert len(long) <= 6 + 80
    assert long.endswith("...")


def test_nus_uuids():
    assert protocol.NUS_SERVICE_UUID == "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    assert protocol.NUS_RX_UUID == "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
    assert protocol.NUS_TX_UUID == "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
