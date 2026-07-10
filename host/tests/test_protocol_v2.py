"""Protocol v2 (PROTOCOL_V2.md): hello negotiation, sessions heartbeat,
prompt sid/qn, prompt_cancel, ask relay, rxack dead-link watch."""

import asyncio
import json
import time

from buddy_bridge import protocol
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.protocol import (
    build_ask_cancel,
    build_ask_evt,
    build_hello,
    build_prompt_cancel,
    build_session_entries,
    build_snapshot,
    effective_proto,
    parse_hello_ack,
    v2_line_budget,
)
from buddy_bridge.registry import RUN, WAIT, SessionRegistry


def _spec_ack(**data_overrides):
    """The canonical device ack from PROTOCOL_V2.md."""
    data = {
        "fw": "2.0.0",
        "board": "stickc3",
        "name": "Claude-AB12",
        "caps": ["sessions", "ask", "rxack", "cancel"],
        "maxSessions": 6,
        "maxLine": 4608,
        "sel": True,
    }
    data.update(data_overrides)
    return {"ack": "hello", "ok": True, "proto": 2, "data": data}


# ---- hello / ack ----


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


def test_parse_hello_ack_spec_shape():
    ack = parse_hello_ack(_spec_ack())
    assert ack.proto == 2
    assert ack.max_line == 4608
    assert ack.max_sessions == 6
    assert ack.sel is True
    assert ack.caps == ("sessions", "ask", "rxack", "cancel")
    assert (ack.fw, ack.board, ack.name) == ("2.0.0", "stickc3", "Claude-AB12")


def test_parse_hello_ack_minimal_and_defaults():
    minimal = parse_hello_ack({"ack": "hello"})
    assert minimal.proto == 1
    assert minimal.max_line == protocol.LINE_BUDGET
    assert minimal.max_sessions == 6
    assert minimal.sel is True  # sel:false is the special case; absent = proceed
    assert minimal.caps == ()


def test_parse_hello_ack_tolerant_types():
    ack = parse_hello_ack(
        {
            "ack": "hello",
            "proto": "x",
            "data": {"maxLine": 9, "maxSessions": 99, "sel": "yes", "caps": [1, "rxack", ""]},
        }
    )
    assert ack.proto == 1  # bad type -> default
    assert ack.max_line == 256  # clamped up
    assert ack.max_sessions == 32  # clamped down
    assert ack.sel is True  # non-boolean ignored; only literal false flips it
    assert ack.caps == ("rxack",)  # non-strings and empties dropped


def test_parse_hello_ack_declined_and_non_acks():
    declined = parse_hello_ack({"ack": "hello", "ok": False, "proto": 2})
    assert declined.proto == 1  # ok:false -> stay v1
    assert declined.caps == ()

    assert parse_hello_ack({"cmd": "hello-ack", "proto": 2}) is None  # old stub shape
    assert parse_hello_ack({"ack": "rx", "n": 5}) is None
    assert parse_hello_ack({"total": 1}) is None
    assert parse_hello_ack("nope") is None


def test_effective_proto_and_budget():
    assert effective_proto(2) == 2
    assert effective_proto(1) == 1
    assert effective_proto(7) == 2  # min(ours=2, theirs)
    assert effective_proto(0) == 1  # floored
    assert v2_line_budget(4608) == 4544  # spec maxLine -> 64 bytes headroom
    assert v2_line_budget(100) == 128  # floored


# ---- sessions entries + heartbeat embedding ----


def _populated_registry():
    reg = SessionRegistry()
    reg.set_state("claude", "s-run", RUN, cwd="C:/w/alpha")
    reg.set_state("claude", "s-wait-2", RUN, cwd="C:/w/beta")
    reg.set_state("codex", "c-wait-1", RUN, cwd="C:/w/gamma")
    reg.set_state("codex", "c-wait-1", WAIT)  # waits first
    reg.set_state("claude", "s-wait-2", WAIT)  # waits second
    reg.add_tokens("claude", "s-run", 123)
    return reg


def test_session_entries_waiting_first_and_shape():
    reg = _populated_registry()
    reg.note_activity("codex", "c-wait-1", "running tests…")
    entries = build_session_entries(reg.sessions_sorted(), wire_sid=reg.wire_sid)
    assert [e["state"] for e in entries] == ["wait", "wait", "run"]
    assert entries[0]["agent"] == "codex"  # FIFO: codex waited first
    assert entries[0]["sid"] == "cdx:1"
    assert entries[1]["sid"] == "cli:1"
    assert entries[2]["sid"] == "cli:2"
    assert entries[0]["title"] == "gamma"
    assert entries[0]["last"] == "running tests..."  # sanitized
    assert entries[2]["tok"] == 123
    assert all(isinstance(e["last"], str) for e in entries)


def test_wire_sids_stable_across_state_changes_and_reappearance():
    reg = _populated_registry()
    first = {
        (e["agent"], e["sid"])
        for e in build_session_entries(reg.sessions_sorted(), wire_sid=reg.wire_sid)
    }
    # flip states around: pinned sids must not move
    reg.set_state("codex", "c-wait-1", RUN)
    reg.set_state("claude", "s-run", WAIT)
    second = {
        (e["agent"], e["sid"])
        for e in build_session_entries(reg.sessions_sorted(), wire_sid=reg.wire_sid)
    }
    assert first == second
    # a dropped session that reappears keeps its sid; a NEW session gets a
    # fresh one (never a reused number)
    old_sid = reg.wire_sid("claude", "s-run")
    reg.drop("claude", "s-run")
    reg.set_state("claude", "brand-new", RUN)
    assert reg.wire_sid("claude", "s-run") == old_sid
    assert reg.wire_sid("claude", "brand-new") not in (old_sid, "cli:1", "cli:2")


def test_session_entries_title_capped_and_sanitized():
    reg = SessionRegistry()
    reg.set_state("claude", "s1", WAIT, cwd="C:/w/" + "很长的项目名字" * 10)
    (entry,) = build_session_entries(reg.sessions_sorted(), wire_sid=reg.wire_sid)
    assert len(entry["title"].encode()) <= 24
    assert all(ord(c) < 128 for c in entry["title"])


def test_session_entries_cap_keeps_waiting():
    reg = SessionRegistry()
    for i in range(8):
        reg.set_state("claude", f"r{i}", RUN)
    reg.set_state("codex", "w1", WAIT)
    entries = build_session_entries(
        reg.sessions_sorted(), wire_sid=reg.wire_sid, max_sessions=3
    )
    assert len(entries) == 3
    assert entries[0]["state"] == "wait"  # the cut can never drop a waiter


def test_snapshot_carries_sessions_alongside_aggregates():
    reg = _populated_registry()
    entries = build_session_entries(reg.sessions_sorted(), wire_sid=reg.wire_sid)
    snap = json.loads(
        build_snapshot(
            total=3, running=1, waiting=2, msg="m", tokens=1, tokens_today=1,
            sessions=entries, budget=4544,
        )
    )
    # v1 aggregates stay authoritative; sessions ride alongside
    assert (snap["total"], snap["running"], snap["waiting"]) == (3, 1, 2)
    assert [s["sid"] for s in snap["sessions"]] == ["cdx:1", "cli:1", "cli:2"]


def test_snapshot_budget_trims_sessions_tail_before_prompt():
    reg = SessionRegistry()
    for i in range(12):
        reg.set_state("claude", f"s{i}", WAIT, cwd=f"C:/w/project-{i}-" + "x" * 40)
    entries = build_session_entries(
        reg.sessions_sorted(), wire_sid=reg.wire_sid, max_sessions=12
    )
    line = build_snapshot(
        total=12, running=0, waiting=12, msg="m",
        prompt={"id": "req_1", "tool": "Bash", "hint": "h"},
        sessions=entries, budget=500,
    )
    snap = json.loads(line)
    assert len(line.encode()) <= 500
    assert snap["prompt"]["id"] == "req_1"  # prompt survives
    kept = snap["sessions"]
    assert 0 < len(kept) < 12
    assert kept[0]["sid"] == "cli:1"  # trimmed from the tail only


# ---- prompt cancel + ask ----


def test_build_prompt_cancel():
    assert json.loads(build_prompt_cancel("req_abc")) == {
        "cmd": "prompt_cancel", "id": "req_abc",
    }


def test_build_ask_evt_spec_shape():
    line = build_ask_evt(
        "ask_xyz789",
        [
            {
                "header": "Which approach?",
                "text": "Pick one:",
                "options": [
                    {"label": "Option A", "desc": "fast"},
                    {"label": "Option B", "desc": "safe"},
                ],
            }
        ],
        sid="cli:1",
    )
    evt = json.loads(line)
    assert evt["evt"] == "ask"
    assert evt["id"] == "ask_xyz789"
    assert evt["sid"] == "cli:1"
    assert evt["multiSelect"] is False
    assert evt["questions"][0]["header"] == "Which approach?"
    assert evt["questions"][0]["options"][0] == {"label": "Option A", "desc": "fast"}


def test_build_ask_evt_budget_degrades():
    questions = [
        {
            "header": f"Q{i}",
            "text": "t" * 90,
            "options": [{"label": f"opt{j}", "desc": "d" * 60} for j in range(6)],
        }
        for i in range(4)
    ]
    line = build_ask_evt("ask_1", questions, budget=300)
    assert len(line.encode()) <= 300
    evt = json.loads(line)
    assert evt["questions"]  # something survived
    assert all("desc" not in o for q in evt["questions"] for o in q["options"])


def test_build_ask_cancel():
    assert json.loads(build_ask_cancel("ask_1")) == {"evt": "ask_cancel", "id": "ask_1"}


# ---- daemon negotiation ----


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.dropped = 0
        self.mode = "exclusive"
        self.backoff_floor = 0.0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def ensure_connected(self, timeout):
        return self._connected

    async def drop_connection(self):
        self.dropped += 1

    async def stop(self):
        pass


def _daemon(home):
    d = Daemon(load_config(home))
    d.link = FakeLink()
    return d


async def test_connect_sends_hello_then_v1_oneshots(home):
    daemon = _daemon(home)
    daemon.cfg.owner = "Arik"
    await daemon._on_ble_connect()
    frames = [json.loads(line) for line in daemon.link.sent]
    assert frames[0]["cmd"] == "hello"  # nothing before hello
    assert "time" in frames[1]
    assert frames[2] == {"owner": "Arik"}
    assert "total" in frames[3]
    assert daemon._hello_deadline > 0


async def test_ack_v2_flips_to_v2_heartbeat(home):
    daemon = _daemon(home)
    daemon.registry.set_state("claude", "s1", WAIT, cwd="C:/w/thing")
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    assert daemon.negotiated_proto == 2
    assert daemon.hello_ack.max_line == 4608
    assert daemon.line_budget == 4544
    assert daemon.device_sel is True
    assert daemon.cap_active("sessions") and daemon.cap_active("cancel")
    assert not daemon.cap_active("ntfy")  # device didn't advertise it
    snap = json.loads(daemon.outbound_state_line())
    assert snap["total"] == 1  # v1 aggregates unchanged
    assert snap["sessions"][0]["sid"] == "cli:1"
    assert snap["sessions"][0]["state"] == "wait"


async def test_ack_v1_or_declined_keeps_v1(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line({"ack": "hello", "ok": True, "proto": 1})
    assert daemon.negotiated_proto == 1
    assert "sessions" not in json.loads(daemon.outbound_state_line())

    daemon2 = _daemon(home)
    await daemon2._on_ble_connect()
    await daemon2._on_ble_line({"ack": "hello", "ok": False, "proto": 2})
    assert daemon2.negotiated_proto == 1


async def test_min_proto_wins(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack() | {"proto": 9})
    assert daemon.negotiated_proto == 2  # min(ours=2, theirs=9)


async def test_no_ack_timeout_stays_v1(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    assert daemon._hello_deadline > 0
    daemon._hello_deadline = time.monotonic() - 0.01  # simulate the 2s lapsing
    await daemon.tick()
    assert daemon._hello_deadline == 0.0
    assert daemon.negotiated_proto == 1
    assert daemon.hello_ack is None
    assert "sessions" not in json.loads(daemon.outbound_state_line())


async def test_ack_sel_false_remembered(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack(sel=False))
    assert daemon.device_sel is False
    assert daemon.negotiated_proto == 2


async def test_sessions_gated_on_cap(home):
    daemon = _daemon(home)
    daemon.registry.set_state("claude", "s1", RUN)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack(caps=["rxack"]))  # no "sessions"
    assert daemon.negotiated_proto == 2
    assert "sessions" not in json.loads(daemon.outbound_state_line())


# ---- prompt sid/qn + prompt_cancel ----


async def _gate(daemon, sid="s1", tuid="toolu_777", command="make deploy"):
    return asyncio.create_task(
        daemon.handle_permission_request(
            {
                "event": "permission_request",
                "agent": "claude",
                "session_id": sid,
                "tool_name": "Bash",
                "tool_use_id": tuid,
                "tool_input": {"command": command},
            }
        )
    )


def _gating_daemon(home):
    from buddy_bridge.matchers import Matchers

    cfg = load_config(home)
    cfg.claude_decision = 0.15
    d = Daemon(cfg)
    d.link = FakeLink()
    d.matchers = Matchers(always_ask=("Bash:make *",))
    return d


async def test_v2_prompt_carries_sid_and_qn(home):
    daemon = _gating_daemon(home)
    daemon.registry.set_state("claude", "s1", WAIT, cwd="C:/w/alpha")
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    task = await _gate(daemon)
    await asyncio.sleep(0.02)
    snap = json.loads(daemon.link.sent[-1])
    assert snap["prompt"]["id"] == "toolu_777"
    assert snap["prompt"]["tool"] == "Bash"
    assert snap["prompt"]["hint"] == "make deploy"
    assert snap["prompt"]["sid"] == daemon.registry.wire_sid("claude", "s1")
    assert snap["prompt"]["qn"] == 0
    await daemon.handle_device_permission(
        {"cmd": "permission", "id": "toolu_777", "decision": "once"}
    )
    assert (await task) == {"decision": "allow"}  # wire "once" approves


async def test_qn_counts_queued_prompts_capped(home):
    daemon = _gating_daemon(home)
    daemon.cfg.claude_decision = 0.5
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    tasks = [await _gate(daemon, tuid=f"toolu_{i}") for i in range(5)]
    await asyncio.sleep(0.05)
    obj = daemon.pending_prompt_obj()
    assert obj["id"] == "toolu_0"  # oldest shown
    assert obj["qn"] == 3  # 4 queued behind, capped at 3
    for i in range(5):
        await daemon.handle_device_permission(
            {"cmd": "permission", "id": f"toolu_{i}", "decision": "deny"}
        )
    for t in tasks:
        await t


async def test_v1_prompt_object_has_no_sid_qn(home):
    daemon = _gating_daemon(home)
    task = await _gate(daemon)
    await asyncio.sleep(0.02)
    snap = json.loads(daemon.link.sent[-1])
    assert snap["prompt"] == {"id": "toolu_777", "tool": "Bash", "hint": "make deploy"}
    await daemon.handle_device_permission(
        {"cmd": "permission", "id": "toolu_777", "decision": "deny"}
    )
    await task


async def test_prompt_cancel_sent_on_timeout_when_cap(home):
    daemon = _gating_daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    resp = await (await _gate(daemon))  # times out at 0.15s
    assert resp == {"decision": "ask"}
    cancels = [json.loads(x) for x in daemon.link.sent if "prompt_cancel" in x]
    assert cancels == [{"cmd": "prompt_cancel", "id": "toolu_777"}]


async def test_prompt_cancel_not_sent_without_cap_or_device_answer(home):
    # no cancel cap -> no cancel frame even on timeout
    daemon = _gating_daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack(caps=["sessions"]))
    resp = await (await _gate(daemon))
    assert resp == {"decision": "ask"}
    assert not [x for x in daemon.link.sent if "prompt_cancel" in x]

    # device answered -> nothing to cancel
    daemon2 = _gating_daemon(home)
    await daemon2._on_ble_connect()
    await daemon2._on_ble_line(_spec_ack())
    task = await _gate(daemon2)
    await asyncio.sleep(0.02)
    await daemon2.handle_device_permission(
        {"cmd": "permission", "id": "toolu_777", "decision": "deny"}
    )
    await task
    assert not [x for x in daemon2.link.sent if "prompt_cancel" in x]


async def test_prompt_cancel_on_session_end(home):
    daemon = _gating_daemon(home)
    daemon.cfg.claude_decision = 5.0
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    task = await _gate(daemon)
    await asyncio.sleep(0.02)
    daemon.handle_hook_mirror(
        {"event": "hook", "agent": "claude", "name": "SessionEnd", "session_id": "s1"}
    )
    assert (await task) == {"decision": "ask"}
    assert [json.loads(x)["id"] for x in daemon.link.sent if "prompt_cancel" in x] == [
        "toolu_777"
    ]


# ---- ask relay send path ----


async def test_send_ask_gated_on_cap(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    daemon.registry.set_state("claude", "s1", WAIT)
    ok = await daemon.send_ask(
        "ask_1",
        [{"header": "H", "text": "T", "options": [{"label": "A", "desc": "d"}]}],
        agent="claude",
        sid="s1",
    )
    assert ok
    evt = json.loads(daemon.link.sent[-1])
    assert evt["evt"] == "ask"
    assert evt["sid"] == daemon.registry.wire_sid("claude", "s1")
    assert await daemon.cancel_ask("ask_1")
    assert json.loads(daemon.link.sent[-1]) == {"evt": "ask_cancel", "id": "ask_1"}


async def test_send_ask_refused_without_cap(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack(caps=["sessions"]))
    sent_before = len(daemon.link.sent)
    assert not await daemon.send_ask("ask_1", [{"header": "H", "text": "T", "options": []}])
    assert not await daemon.cancel_ask("ask_1")
    assert len(daemon.link.sent) == sent_before


# ---- rxack dead-link watch ----


async def test_rxack_dead_link_triggers_reconnect(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    await daemon._send_snapshot(force=True)  # bytes went out
    assert daemon._tx_since_rx
    daemon._rx_last_ts = time.monotonic() - (protocol.RXACK_DEAD_SECS + 1)
    await daemon.tick()
    assert daemon.link.dropped == 1


async def test_rx_ack_frames_keep_link_alive(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack())
    await daemon._send_snapshot(force=True)
    await daemon._on_ble_line({"ack": "rx", "n": 1024})  # device is listening
    daemon._rx_last_ts = time.monotonic()  # fresh traffic
    await daemon.tick()
    assert daemon.link.dropped == 0
    assert not daemon._tx_since_rx


async def test_rxack_watch_inactive_without_cap(home):
    daemon = _daemon(home)
    await daemon._on_ble_connect()
    await daemon._on_ble_line(_spec_ack(caps=["sessions"]))  # no rxack
    await daemon._send_snapshot(force=True)
    daemon._rx_last_ts = time.monotonic() - (protocol.RXACK_DEAD_SECS + 10)
    await daemon.tick()
    assert daemon.link.dropped == 0
