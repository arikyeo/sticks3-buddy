"""Protocol v2 stub: hello/ack negotiation + sessions composer, both modes."""

import json

from buddy_bridge import protocol
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.protocol import build_hello, build_sessions_frame, parse_hello_ack
from buddy_bridge.registry import RUN, WAIT, SessionRegistry


def test_build_hello_canonical_shape(home):
    cfg = load_config(home)
    cfg.host_name = "arik—box"  # glyph-sanitized on the wire
    frame = json.loads(build_hello(cfg))
    assert frame["cmd"] == "hello"
    assert frame["proto"] == 2
    assert frame["host"]["id"] == cfg.host_id
    assert frame["host"]["name"] == "arik-box"
    assert frame["host"]["app"] == "buddy-bridge"
    assert frame["host"]["ver"]
    assert frame["caps"] == ["sessions", "ask", "rxack", "cancel"]


def test_parse_hello_ack():
    ack = parse_hello_ack(
        {"cmd": "hello-ack", "proto": 2, "maxLine": 512, "maxSessions": 4, "sel": True}
    )
    assert (ack.proto, ack.max_line, ack.max_sessions, ack.sel) == (2, 512, 4, True)

    minimal = parse_hello_ack({"cmd": "hello-ack"})
    assert (minimal.proto, minimal.max_line, minimal.max_sessions, minimal.sel) == (
        1, protocol.LINE_BUDGET, 6, False,
    )

    tolerant = parse_hello_ack(
        {"cmd": "helloAck", "proto": "x", "maxLine": 9, "maxSessions": 99, "sel": "yes"}
    )
    assert tolerant.proto == 1  # bad type -> default
    assert tolerant.max_line == 128  # clamped up
    assert tolerant.max_sessions == 16  # clamped down
    assert tolerant.sel is False  # only literal true counts

    assert parse_hello_ack({"cmd": "status"}) is None
    assert parse_hello_ack({"total": 1}) is None
    assert parse_hello_ack("nope") is None


def _populated_registry():
    reg = SessionRegistry()
    reg.set_state("claude", "s-run", RUN, cwd="C:/w/alpha")
    reg.set_state("claude", "s-wait-2", RUN, cwd="C:/w/beta")
    reg.set_state("codex", "c-wait-1", RUN, cwd="C:/w/gamma")
    reg.set_state("codex", "c-wait-1", WAIT)  # waits first
    reg.set_state("claude", "s-wait-2", WAIT)  # waits second
    reg.add_tokens("claude", "s-run", 123)
    return reg


def test_sessions_frame_waiting_first_and_shape():
    reg = _populated_registry()
    frame = json.loads(build_sessions_frame(reg.sessions_sorted()))
    assert frame["cmd"] == "sessions"
    sessions = frame["sessions"]
    assert [s["state"] for s in sessions] == ["wait", "wait", "run"]
    assert sessions[0]["agent"] == "codex"  # FIFO: codex waited first
    assert sessions[0]["sid"] == "cdx:1"
    assert sessions[1]["sid"] == "cli:1"
    assert sessions[2]["sid"] == "cli:2"
    assert sessions[0]["title"] == "gamma"
    assert sessions[2]["tok"] == 123
    assert all(isinstance(s["last"], int) for s in sessions)


def test_sessions_frame_title_capped_and_sanitized():
    reg = SessionRegistry()
    reg.set_state("claude", "s1", WAIT, cwd="C:/w/" + "很长的项目名字" * 10)
    frame = json.loads(build_sessions_frame(reg.sessions_sorted()))
    title = frame["sessions"][0]["title"]
    assert len(title.encode()) <= 24
    assert all(ord(c) < 128 for c in title)


def test_sessions_frame_cap_keeps_waiting():
    reg = SessionRegistry()
    for i in range(8):
        reg.set_state("claude", f"r{i}", RUN)
    reg.set_state("codex", "w1", WAIT)
    frame = json.loads(build_sessions_frame(reg.sessions_sorted(), max_sessions=3))
    sessions = frame["sessions"]
    assert len(sessions) == 3
    assert sessions[0]["state"] == "wait"  # the cut can never drop a waiter


def test_sessions_frame_respects_budget():
    reg = SessionRegistry()
    for i in range(16):
        reg.set_state("claude", f"s{i}", WAIT, cwd=f"C:/w/project-{i}-" + "x" * 60)
    line = build_sessions_frame(reg.sessions_sorted(), max_sessions=16, budget=400)
    assert len(line.encode()) <= 400
    assert json.loads(line)["sessions"]  # still a valid non-empty frame


# ---- gating through the daemon (both modes) ----


async def test_daemon_default_stays_v1(home):
    daemon = Daemon(load_config(home))
    line = json.loads(daemon.outbound_state_line())
    assert "total" in line and "cmd" not in line  # v1 snapshot
    # even a v2 ack must not flip anything while the stub is disabled
    await daemon._on_ble_line({"cmd": "hello-ack", "proto": 2})
    assert daemon.negotiated_proto == 1
    line = json.loads(daemon.outbound_state_line())
    assert "total" in line and "cmd" not in line


async def test_daemon_v2_after_ack(home):
    daemon = Daemon(load_config(home))
    daemon.proto2_enabled = True
    daemon.registry.set_state("claude", "s1", WAIT, cwd="C:/w/thing")
    await daemon._on_ble_line({"cmd": "hello-ack", "proto": 2, "maxSessions": 4})
    assert daemon.negotiated_proto == 2
    frame = json.loads(daemon.outbound_state_line())
    assert frame["cmd"] == "sessions"
    assert frame["sessions"][0]["sid"] == "cli:1"


async def test_daemon_v1_ack_keeps_v1(home):
    daemon = Daemon(load_config(home))
    daemon.proto2_enabled = True
    await daemon._on_ble_line({"cmd": "hello-ack", "proto": 1})
    assert daemon.negotiated_proto == 1
    assert "total" in json.loads(daemon.outbound_state_line())
