"""Daemon-level federation: merged heartbeat, remote decision round-trip,
holder failover resync, v1 fallback, ask relay, TTL display cleanup.
Everything runs on fakes — zero sockets, zero real BLE."""

import asyncio
import json
import time

import pytest

from buddy_bridge import relay as relay_mod
from buddy_bridge.audit import read_audit
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.decision import ALLOW, ASK
from buddy_bridge.federation import STATE_TTL_SECS, Federation
from buddy_bridge.matchers import Matchers
from buddy_bridge.protocol import PROTO_V2, HelloAck
from buddy_bridge.registry import WAIT
from buddy_bridge.relay import EV_PROMPT_RESOLVED, EV_STATE_SYNC, RelayManager, sign_frame

TOKEN = "sekrit-lan-token"
V2_CAPS = ("sessions", "ask", "cancel", "rxack")


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0
        self.drops = 0
        self.wakes = 0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def ensure_connected(self, timeout):
        return self._connected

    async def drop_connection(self):
        self.drops += 1
        self._connected = False

    def wake(self):
        self.wakes += 1

    async def stop(self):
        pass


def _mk_daemon(home, sub, *, connected, host_id=None):
    bh = home / sub
    bh.mkdir()
    cfg = load_config(bh)
    if host_id:
        cfg.host_id = host_id
    cfg.host_name = sub
    cfg.relay_enabled = True
    cfg.relay_token = TOKEN
    cfg.claude_decision = 5.0
    d = Daemon(cfg)
    d.link = FakeLink(connected=connected)
    d.matchers = Matchers(always_ask=("Bash:make *",))
    d.relay = RelayManager(
        host_id=cfg.host_id,
        host_name=sub,
        port=48901,
        token=TOKEN,
        is_holder=lambda: d.link.connected,
        on_peer_event=d._on_peer_event,
        on_holder_view=d._on_relay_holder_view,
    )
    return d


def _go_v2(d, caps=V2_CAPS, max_sessions=8):
    d.negotiated_proto = PROTO_V2
    d.hello_ack = HelloAck(proto=2, caps=tuple(caps), max_sessions=max_sessions)


def _discovery(host_id, *, holder, v=2, name=None):
    payload = {"bb": 1, "id": host_id, "name": name or host_id, "port": 48901,
               "holder": holder, "ts": int(time.time())}
    if v:
        payload["v"] = v
    return payload


@pytest.fixture
def holder(home):
    d = _mk_daemon(home, "alpha", connected=True, host_id="hold01")
    _go_v2(d)
    return d


@pytest.fixture
def origin(home):
    d = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    # a v2 holder peer is on the air
    d.relay.peers.observe(_discovery("hold01", holder=True), ("10.0.0.5", 1))
    return d


def _state_sync(host="peer01", name="bravo", sessions=(), prompt=None, ask=None):
    payload = {"bb": 1, "ev": EV_STATE_SYNC, "host": host, "name": name,
               "sessions": list(sessions)}
    if prompt is not None:
        payload["prompt"] = prompt
    if ask is not None:
        payload["ask"] = ask
    return payload


def _sess(sid="cli:1", state="wait", title="proj"):
    return {"sid": sid, "agent": "claude", "title": title, "state": state,
            "tok": 7, "last": "building"}


def _req(tool="Bash", command="make deploy", sid="s1", tuid="toolu_777"):
    return {
        "event": "permission_request",
        "agent": "claude",
        "session_id": sid,
        "tool_name": tool,
        "tool_use_id": tuid,
        "tool_input": {"command": command},
    }


def _wire(daemon, host, port, sent):
    """Route a relay manager's outbound frames straight into another test
    daemon's peer-event handler (the transport itself is tested separately)."""
    peers = {}

    async def fake_frame(h, p, event):
        sent.append((h, p, event))
        target = peers.get((h, p))
        if target is None:
            return False
        payload = {"bb": 1, "host": daemon.relay.host_id, **event}
        await target._on_peer_event(payload)
        return True

    daemon.relay._send_frame = fake_frame
    return peers


# ---- merged heartbeat on the holder ----


async def test_state_sync_merges_sessions_into_heartbeat(holder):
    holder.registry.set_state("claude", "loc1", WAIT)
    await holder._on_peer_event(_state_sync(sessions=[
        _sess(sid="cli:1", state="wait", title="工程"),
        _sess(sid="cli:2", state="run"),
    ]))
    line = holder.outbound_state_line()
    snap = json.loads(line)
    assert snap["total"] == 3 and snap["waiting"] == 2 and snap["running"] == 1
    assert snap["msg"] == "2 waiting for you"
    sids = [s["sid"] for s in snap["sessions"]]
    assert sids == ["cli:1", "r0:cli:1", "r0:cli:2"]  # local wait, remote wait, remote run
    remote = snap["sessions"][1]
    assert remote["title"].startswith("[bravo]")
    assert len(remote["sid"].encode()) <= 11
    # heartbeat was pushed on the fold, content-deduped afterwards
    assert any('"sessions"' in line for line in holder.link.sent)


async def test_v2_peer_cards_suppressed_v1_peer_still_cards(holder):
    holder.registry.set_state("claude", "busy", WAIT)  # no early yield
    await holder._on_peer_event(_state_sync(host="peer01"))
    # peer01 federates: its v1 prompt_pending is redundant -> no card
    await holder._on_peer_event(
        {"bb": 1, "ev": "prompt_pending", "host": "peer01", "name": "bravo",
         "agent": "claude", "tool": "Bash", "hint": "make x", "sid": "s1"}
    )
    assert holder.cards.queue == []
    # a pure-v1 peer (never sent state_sync) still gets the card path
    await holder._on_peer_event(
        {"bb": 1, "ev": "prompt_pending", "host": "old99", "name": "carol",
         "agent": "claude", "tool": "Bash", "hint": "make y", "sid": "s2"}
    )
    (card,) = holder.cards.queue
    assert card.title == "[carol] approve: Bash"
    # attention from a federated peer: suppressed too
    await holder._on_peer_event(
        {"bb": 1, "ev": "attention", "host": "peer01", "name": "bravo", "n": 2}
    )
    assert len(holder.cards.queue) == 1


async def test_merged_prompt_oldest_and_qn_across_machines(holder):
    # remote prompt arrives first -> it is older than the local one
    await holder._on_peer_event(_state_sync(prompt={
        "id": "p9", "tool": "Bash", "hint": "make deploy", "sid": "cli:1", "qn": 1,
    }))
    reply_task = asyncio.create_task(holder.handle_permission_request(_req(sid="ls1")))
    await asyncio.sleep(0.05)  # local pending created after the remote fold
    obj = holder.pending_prompt_obj()
    assert obj["id"] == "r0.p9"  # oldest across machines wins
    # merged depth: local 1 + remote (1 + qn 1) - 1 shown = 2
    assert obj["qn"] == 2
    # stick answers the remote prompt; the local one becomes the display
    await holder._on_ble_line({"cmd": "permission", "id": "r0.p9", "decision": "once"})
    obj = holder.pending_prompt_obj()
    assert obj["id"] == "toolu_777" and obj["qn"] == 0
    await holder._on_ble_line({"cmd": "permission", "id": "toolu_777", "decision": "deny"})
    assert (await reply_task) == {"decision": "deny"}


# ---- decision round-trip (holder stick -> origin future) ----


async def test_remote_decision_full_round_trip(monkeypatch, home):
    monkeypatch.setattr(relay_mod, "DECISION_RETRY_DELAY_SECS", 0.0)
    holder = _mk_daemon(home, "alpha", connected=True, host_id="hold01")
    _go_v2(holder)
    origin = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    origin.relay.peers.observe(_discovery("hold01", holder=True), ("10.0.0.5", 1))
    holder.relay.peers.observe(_discovery("orig01", holder=False), ("10.0.0.6", 1))
    o_sent, h_sent = [], []
    _wire(origin, "", 0, o_sent)[("10.0.0.5", 48901)] = holder
    _wire(holder, "", 0, h_sent)[("10.0.0.6", 48901)] = origin

    gate = asyncio.create_task(origin.handle_permission_request(_req()))
    await asyncio.sleep(0.05)
    # the origin pushed state_sync; the holder's heartbeat offers the prompt
    assert any(e.get("ev") == EV_STATE_SYNC for *_, e in o_sent)
    obj = holder.pending_prompt_obj()
    assert obj["id"] == "r0.toolu_777"
    assert holder.federation.route("r0.toolu_777") == ("orig01", "toolu_777")
    # stick button on the holder
    await holder._on_ble_line({"cmd": "permission", "id": "r0.toolu_777",
                               "decision": "once"})
    result = await asyncio.wait_for(gate, 2)
    assert result == {"decision": ALLOW}
    # origin audited it as a device decision (exactly like a local button)
    (entry,) = [e for e in read_audit(origin.cfg.home) if e["decision"] == "allow"]
    assert entry["source"] == "device" and entry["tool"] == "Bash"
    # origin's exit cleanup told the holder the prompt is gone
    assert any(e.get("ev") == EV_PROMPT_RESOLVED and e.get("pid") == "toolu_777"
               for *_, e in o_sent)
    assert holder.federation.oldest_remote_prompt() is None
    # decision left the holder exactly once (first attempt succeeded)
    assert [e for *_, e in h_sent if e.get("ev") == "decision"] == [
        {"ev": "decision", "id": "toolu_777", "decision": "allow"}
    ]


async def test_remote_decision_retries_on_dead_link(monkeypatch, home):
    monkeypatch.setattr(relay_mod, "DECISION_RETRY_DELAY_SECS", 0.0)
    holder = _mk_daemon(home, "alpha", connected=True, host_id="hold01")
    _go_v2(holder)
    holder.relay.peers.observe(_discovery("orig01", holder=False), ("10.0.0.6", 1))
    attempts = []

    async def fake_frame(h, p, event):
        attempts.append(event)
        return len(attempts) >= 3  # two failures, then delivery

    holder.relay._send_frame = fake_frame
    await holder._on_peer_event(_state_sync(host="orig01", prompt={
        "id": "p1", "tool": "Bash", "hint": "x",
    }))
    await holder._on_ble_line({"cmd": "permission", "id": "r0.p1", "decision": "deny"})
    await asyncio.gather(*holder._fed_tasks)  # background relay task drains
    assert len(attempts) == 3
    assert attempts[-1] == {"ev": "decision", "id": "p1", "decision": "deny"}
    # the merged view advanced immediately, not after the retries
    assert holder.federation.oldest_remote_prompt() is None


async def test_origin_timeout_fails_open_and_clears_holder(origin):
    origin.cfg.claude_decision = 0.05
    sent = []
    _wire(origin, "", 0, sent)  # holder unreachable: sends fail quietly
    result = await origin.handle_permission_request(_req())
    assert result == {"decision": ASK}
    (entry,) = read_audit(origin.cfg.home)
    assert entry["source"] == "timeout_fallback"
    # exit path still tried to clear the holder display (pid attached)
    assert any(e.get("ev") == EV_PROMPT_RESOLVED and e.get("pid") == "toolu_777"
               for *_, e in sent)


async def test_origin_without_v2_holder_asks_immediately(home):
    d = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    legacy = _discovery("hold01", holder=True, v=0)  # v1 holder: no state_sync
    d.relay.peers.observe(legacy, ("10.0.0.5", 1))
    result = await d.handle_permission_request(_req())
    assert result == {"decision": ASK}
    (entry,) = read_audit(d.cfg.home)
    assert entry["source"] == "daemon_down"  # unchanged v1 fail-open


async def test_prompt_resolved_pid_clears_fed_prompt(holder):
    await holder._on_peer_event(_state_sync(host="peer01", prompt={
        "id": "p1", "tool": "Bash", "hint": "x",
    }))
    assert holder.federation.oldest_remote_prompt() is not None
    await holder._on_peer_event(
        {"bb": 1, "ev": EV_PROMPT_RESOLVED, "host": "peer01", "sid": "s1", "pid": "p1"}
    )
    assert holder.federation.oldest_remote_prompt() is None


# ---- origin mirroring: v2 stream vs v1 fallback ----


async def test_origin_streams_state_sync_to_v2_holder(origin):
    sent = []
    _wire(origin, "", 0, sent)
    session = origin.registry.set_state("claude", "s1", WAIT)
    session.prompts.append("needs you")
    origin._last_relay_sync = 0.0
    await origin._relay_sync(force=True)
    ev = [e for *_, e in sent if e.get("ev") == EV_STATE_SYNC][-1]
    assert ev["name"] == "bravo"
    assert ev["sessions"][0]["sid"] == "cli:1" and ev["sessions"][0]["state"] == "wait"
    assert "prompt" not in ev  # a wait without a gate decision is not a prompt


async def test_origin_falls_back_to_v1_cards_for_v1_holder(home):
    d = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    legacy = _discovery("hold01", holder=True, v=0)
    d.relay.peers.observe(legacy, ("10.0.0.5", 1))
    sent = []
    _wire(d, "", 0, sent)
    session = d.registry.set_state("claude", "s1", WAIT)
    session.prompts.append("Claude needs your permission to use Bash")
    await d._relay_sync(force=True)
    events = [e.get("ev") for *_, e in sent]
    assert "prompt_pending" in events and EV_STATE_SYNC not in events


async def test_holder_failover_retargets_and_resyncs(home):
    d = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    d.relay.peers.observe(_discovery("hold01", holder=True), ("10.0.0.5", 1))
    sent = []
    _wire(d, "", 0, sent)
    d.registry.set_state("claude", "s1", WAIT)
    await d._relay_sync(force=True)
    assert sent[-1][0] == "10.0.0.5"
    n = len(sent)
    await d._relay_sync(force=False)  # unchanged + within keepalive: quiet
    assert len(sent) == n
    # holder moves: hold01 released, hold02 claims (datagrams via _on_datagram
    # so the on_holder_view callback fires exactly like production)
    d.relay._on_datagram(
        sign_frame(_discovery("hold01", holder=False), TOKEN), ("10.0.0.5", 1))
    d.relay._on_datagram(
        sign_frame(_discovery("hold02", holder=True), TOKEN), ("10.0.0.6", 1))
    assert d._relay_force_sync is True  # holder change scheduled a full resync
    await d.tick()  # consumes the flag
    assert sent[-1][0] == "10.0.0.6"  # same state, retargeted + re-pushed
    assert sent[-1][2]["ev"] == EV_STATE_SYNC


# ---- ask relay ----


async def test_remote_ask_forwarded_and_cancelled(holder):
    ask = {"id": "toolu_ask1", "sid": "cli:1", "multiSelect": False,
           "questions": [{"header": "Pick", "text": "Which?", "options": [
               {"label": "A", "desc": ""}, {"label": "B", "desc": ""}]}]}
    await holder._on_peer_event(_state_sync(ask=ask))
    evt = [line for line in holder.link.sent if '"evt":"ask"' in line][-1]
    parsed = json.loads(evt)
    assert parsed["id"] == "r0.toolu_ask1"
    assert parsed["sid"] == "r0:cli:1"
    assert parsed["questions"][0]["header"].startswith("[bravo]")
    # ask vanishes from the stream -> ask_cancel to the stick
    await holder._on_peer_event(_state_sync())
    cancel = [line for line in holder.link.sent if '"evt":"ask_cancel"' in line][-1]
    assert json.loads(cancel)["id"] == "r0.toolu_ask1"
    assert holder._remote_live_asks == set()


async def test_origin_ask_rides_state_sync_and_clears(origin):
    sent = []
    _wire(origin, "", 0, sent)
    await origin.handle_ask_pending({
        "agent": "claude", "session_id": "s1", "tool_use_id": "toolu_q1",
        "questions": [{"question": "Deploy?", "header": "Deploy",
                       "options": [{"label": "Yes"}, {"label": "No"}]}],
    })
    ev = [e for *_, e in sent if e.get("ev") == EV_STATE_SYNC][-1]
    assert ev["ask"]["id"] == "toolu_q1"
    assert ev["ask"]["questions"][0]["text"] == "Deploy?"
    assert origin._fed_ask is not None
    # the user answered in the CLI (PostToolUse with the same tool_use_id)
    await origin._on_hook_followups(
        {"agent": "claude", "name": "PostToolUse", "session_id": "s1",
         "tool_use_id": "toolu_q1"})
    assert origin._fed_ask is None and origin._relay_force_sync is True
    origin._relay_force_sync = False
    await origin._relay_sync(force=True)
    ev = [e for *_, e in sent if e.get("ev") == EV_STATE_SYNC][-1]
    assert "ask" not in ev


# ---- TTL expiry cleans the stick displays ----


async def test_peer_expiry_cleans_sessions_prompt_and_ask(holder):
    clock = [1000.0]
    holder.federation = Federation(holder.router, now_fn=lambda: clock[0])
    ask = {"id": "q1", "sid": "cli:1", "multiSelect": False,
           "questions": [{"header": "H", "text": "T", "options": []}]}
    await holder._on_peer_event(_state_sync(
        sessions=[_sess()], prompt={"id": "p1", "tool": "Bash", "hint": "x"}, ask=ask))
    assert holder.pending_prompt_obj()["id"] == "r0.p1"
    assert '"prompt"' in holder.link.sent[-1]  # offered in a heartbeat
    assert holder._remote_live_asks == {"r0.q1"}
    clock[0] += STATE_TTL_SECS + 1
    holder._last_relay_sync = 0.0
    await holder._relay_sync()
    assert holder.federation.peer_count() == 0
    assert holder.pending_prompt_obj() is None
    assert holder.router.remote_route_count() == 0
    # the stick got an explicit retraction for both displays
    assert any('"cmd":"prompt_cancel"' in line and "r0.p1" in line
               for line in holder.link.sent)
    assert any('"evt":"ask_cancel"' in line and "r0.q1" in line
               for line in holder.link.sent)
    snap = json.loads(holder.outbound_state_line())
    assert snap["total"] == 0 and "prompt" not in snap


# ---- HMAC on the new event types ----


async def test_unsigned_or_tampered_decision_frames_rejected(home):
    d = _mk_daemon(home, "bravo", connected=False, host_id="orig01")
    seen = []

    async def spy(payload):
        seen.append(payload)

    d.relay._on_peer_event = spy

    class FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def feed(raw):
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        await d.relay._on_tcp_connection(reader, FakeWriter())

    good = {"bb": 1, "ev": "decision", "host": "hold01", "id": "p1",
            "decision": "allow", "ts": int(time.time())}
    await feed(json.dumps(good).encode() + b"\n")  # unsigned: dropped
    assert seen == []
    tampered = sign_frame({**good, "decision": "deny"}, TOKEN)
    obj = json.loads(tampered)
    obj["decision"] = "allow"  # flip after signing
    await feed(json.dumps(obj).encode() + b"\n")
    assert seen == []
    stale = sign_frame({**good, "ts": int(time.time()) - 4000}, TOKEN)
    await feed(stale)
    assert seen == []
    await feed(sign_frame(good, TOKEN))  # the real thing passes
    assert len(seen) == 1 and seen[0]["decision"] == "allow"


async def test_forged_decision_cannot_resolve_without_pending(origin):
    # even a *signed* decision for an unknown id is a no-op at the origin
    await origin._on_peer_event(
        {"bb": 1, "ev": "decision", "host": "hold01", "id": "nope",
         "decision": "allow"})
    assert origin.router.pending_count() == 0  # nothing resolved, nothing crashed
