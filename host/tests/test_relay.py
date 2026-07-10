"""LAN relay federation: HMAC framing, discovery, holder view, card emit,
rate limiting, early yield, and the disabled-by-default zero-socket guarantee."""

import asyncio
import json
import socket
import time

import pytest

from buddy_bridge import relay as relay_mod
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.protocol import PROTO_V2, HelloAck
from buddy_bridge.registry import WAIT
from buddy_bridge.relay import (
    FRESH_WINDOW_SECS,
    PEER_TTL_SECS,
    PeerTable,
    RelayManager,
    parse_frame,
    sign_frame,
)

TOKEN = "sekrit-lan-token"


# ---- signed frames ----


def _payload(**extra):
    base = {"bb": 1, "id": "peer01", "name": "bravo", "port": 48901,
            "holder": True, "ts": int(time.time())}
    base.update(extra)
    return base


def test_sign_parse_roundtrip():
    frame = sign_frame(_payload(), TOKEN)
    assert frame.endswith(b"\n")
    out = parse_frame(frame, TOKEN)
    assert out is not None and out["id"] == "peer01" and "mac" not in out


def test_parse_rejects_bad_mac():
    frame = sign_frame(_payload(), TOKEN)
    obj = json.loads(frame)
    obj["mac"] = "0" * 64
    assert parse_frame(json.dumps(obj).encode(), TOKEN) is None


def test_parse_rejects_missing_mac():
    raw = json.dumps(_payload()).encode()
    assert parse_frame(raw, TOKEN) is None


def test_parse_rejects_tampered_payload():
    frame = sign_frame(_payload(), TOKEN)
    obj = json.loads(frame)
    obj["holder"] = False  # flip a field, keep the old mac
    assert parse_frame(json.dumps(obj).encode(), TOKEN) is None


def test_parse_rejects_wrong_token():
    frame = sign_frame(_payload(), TOKEN)
    assert parse_frame(frame, "other-token") is None
    assert parse_frame(frame, "") is None  # empty token can never verify


def test_parse_rejects_stale_or_future_ts():
    now = time.time()
    old = sign_frame(_payload(ts=int(now - FRESH_WINDOW_SECS - 5)), TOKEN)
    assert parse_frame(old, TOKEN, now=now) is None
    future = sign_frame(_payload(ts=int(now + FRESH_WINDOW_SECS + 5)), TOKEN)
    assert parse_frame(future, TOKEN, now=now) is None
    missing = sign_frame({"bb": 1, "id": "x", "port": 1}, TOKEN)
    assert parse_frame(missing, TOKEN) is None  # no ts at all


def test_parse_rejects_garbage_and_oversize():
    assert parse_frame(b"not json", TOKEN) is None
    assert parse_frame(b'"a string"', TOKEN) is None
    assert parse_frame(b"[1,2,3]", TOKEN) is None
    big = sign_frame(_payload(pad="x" * (relay_mod.MAX_FRAME_BYTES + 1)), TOKEN)
    assert parse_frame(big, TOKEN) is None


# ---- peer table ----


def test_peer_table_observe_prune_holder():
    now = [1000.0]
    table = PeerTable("me", now_fn=lambda: now[0])
    assert table.observe(_payload(id="me"), ("10.0.0.5", 1)) is None  # own echo
    assert table.observe(_payload(id="", port=48901), ("10.0.0.5", 1)) is None
    assert table.observe(_payload(port=0), ("10.0.0.5", 1)) is None  # bad port
    assert table.observe({"bb": 2, "id": "x", "port": 1}, ("10.0.0.5", 1)) is None

    peer = table.observe(_payload(id="p1", holder=False), ("10.0.0.5", 60001))
    assert peer is not None and peer.host == "10.0.0.5" and peer.port == 48901
    table.observe(_payload(id="p2", name="charlie", holder=True), ("10.0.0.6", 60002))
    assert [p.id for p in table.peers()] == ["p1", "p2"]
    assert table.holder().id == "p2"

    now[0] += PEER_TTL_SECS + 1  # everyone expires
    assert table.peers() == [] and table.holder() is None


def test_peer_table_holder_freshest_claim_wins():
    now = [1000.0]
    table = PeerTable("me", now_fn=lambda: now[0])
    table.observe(_payload(id="p1", holder=True), ("10.0.0.5", 1))
    now[0] += 5
    table.observe(_payload(id="p2", holder=True), ("10.0.0.6", 1))
    assert table.holder().id == "p2"


# ---- manager: datagrams, announce, sync ----


async def _noop_event(payload):
    pass


def _mgr(*, is_holder=lambda: False, on_peer_event=None, on_holder_view=None, now_fn=None):
    return RelayManager(
        host_id="me",
        host_name="alpha",
        port=48901,
        token=TOKEN,
        is_holder=is_holder,
        on_peer_event=on_peer_event or _noop_event,
        on_holder_view=on_holder_view,
        now_fn=now_fn or time.time,
    )


async def test_datagram_updates_peers_and_holder_view():
    views = []
    mgr = _mgr(on_holder_view=lambda: views.append(True))
    mgr._on_datagram(sign_frame(_payload(id="p1", holder=True), TOKEN), ("10.0.0.5", 48901))
    assert mgr.peers.holder().id == "p1"
    assert views == [True]  # holder appeared
    mgr._on_datagram(sign_frame(_payload(id="p1", holder=True), TOKEN), ("10.0.0.5", 48901))
    assert views == [True]  # unchanged view: no second callback
    mgr._on_datagram(sign_frame(_payload(id="p1", holder=False), TOKEN), ("10.0.0.5", 48901))
    assert mgr.peers.holder() is None
    assert views == [True, True]  # holder released


async def test_datagram_rejects_unsigned_and_own_echo():
    mgr = _mgr()
    mgr._on_datagram(json.dumps(_payload(id="p1")).encode(), ("10.0.0.5", 1))
    mgr._on_datagram(b"garbage", ("10.0.0.5", 1))
    mgr._on_datagram(sign_frame(_payload(id="me"), TOKEN), ("10.0.0.5", 1))
    assert mgr.peers.peers() == []


async def test_tcp_connection_dispatches_verified_events_only():
    events = []

    async def on_event(payload):
        events.append(payload)

    mgr = _mgr(on_peer_event=on_event)

    class FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def feed(raw: bytes):
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        await mgr._on_tcp_connection(reader, FakeWriter())

    good = {"bb": 1, "ev": "attention", "host": "p1", "name": "bravo", "n": 2,
            "ts": int(time.time())}
    await feed(sign_frame(good, TOKEN))
    assert len(events) == 1 and events[0]["ev"] == "attention"

    await feed(json.dumps(good).encode() + b"\n")  # unsigned
    await feed(sign_frame({**good, "host": "me"}, TOKEN))  # own echo
    await feed(sign_frame({"bb": 1, "ts": int(time.time())}, TOKEN))  # no ev
    assert len(events) == 1


async def test_start_binds_stubs_and_announces(monkeypatch):
    sent = []

    class FakeTransport:
        def sendto(self, data, addr):
            sent.append((data, addr))

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
    monkeypatch.setattr(relay_mod, "broadcast_addrs", lambda: ["255.255.255.255"])
    mgr = _mgr(is_holder=lambda: True)
    tasks = await mgr.start()
    try:
        assert mgr.tcp_port == 48901
        (data, addr) = sent[0]
        assert addr == ("255.255.255.255", 48901)
        payload = parse_frame(data, TOKEN)
        assert payload == {"bb": 1, "v": 2, "id": "me", "name": "alpha", "port": 48901,
                           "holder": True, "ts": payload["ts"]}
    finally:
        for task in tasks:
            task.cancel()
        await mgr.stop()


async def test_sync_local_pending_dedupe_resolve():
    mgr = _mgr()
    sent = []

    async def fake_send(event):
        sent.append(event)
        return True

    mgr.send_to_holder = fake_send
    prompts = {"s1": {"agent": "claude", "tool": "", "hint": "x" * 200}}
    await mgr.sync_local(prompts, 1)
    assert len(sent) == 1
    assert sent[0]["ev"] == "prompt_pending" and sent[0]["sid"] == "s1"
    assert len(sent[0]["hint"]) == relay_mod.HINT_MAX_CHARS  # capped
    await mgr.sync_local(prompts, 1)  # unchanged: nothing re-sent
    assert len(sent) == 1
    await mgr.sync_local({}, 0)  # resolved locally
    assert sent[-1] == {"ev": "prompt_resolved", "sid": "s1"} and len(sent) == 2


async def test_sync_local_retries_until_holder_reachable():
    mgr = _mgr()
    outcome = [False]
    sent = []

    async def fake_send(event):
        sent.append(event)
        return outcome[0]

    mgr.send_to_holder = fake_send
    prompts = {"s1": {"agent": "claude", "tool": "Bash", "hint": "hi"}}
    await mgr.sync_local(prompts, 1)  # send fails: not marked relayed
    outcome[0] = True
    await mgr.sync_local(prompts, 1)  # retried now that the holder answers
    assert [e["ev"] for e in sent] == ["prompt_pending", "prompt_pending"]


async def test_sync_local_attention_fallback_and_holder_noop():
    mgr = _mgr()
    sent = []

    async def fake_send(event):
        sent.append(event)
        return True

    mgr.send_to_holder = fake_send
    await mgr.sync_local({}, 2)  # waiting but no concrete hints
    assert sent == [{"ev": "attention", "name": "alpha", "n": 2}]
    await mgr.sync_local({}, 2)  # same count: silent
    assert len(sent) == 1
    await mgr.sync_local({}, 0)  # count drop: no zero-spam
    assert len(sent) == 1

    holder_mgr = _mgr(is_holder=lambda: True)
    holder_mgr.send_to_holder = fake_send
    holder_mgr._relayed_prompts = {"s1": {"agent": "claude", "tool": "", "hint": "h"}}
    await holder_mgr.sync_local({"s2": {"agent": "claude", "tool": "", "hint": "h2"}}, 1)
    assert len(sent) == 1  # holders never relay
    assert holder_mgr._relayed_prompts == {}  # mirror state reset


# ---- daemon integration (FakeLink, no sockets anywhere) ----


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

    async def drop_connection(self):
        self.drops += 1
        self._connected = False

    def wake(self):
        self.wakes += 1

    async def stop(self):
        pass


@pytest.fixture
def daemon(home):
    cfg = load_config(home)
    cfg.relay_enabled = True
    cfg.relay_token = TOKEN
    d = Daemon(cfg)
    d.link = FakeLink(connected=True)
    d.relay = RelayManager(
        host_id=cfg.host_id,
        host_name="alpha",
        port=48901,
        token=TOKEN,
        is_holder=lambda: d.link.connected,
        on_peer_event=d._on_peer_event,
        on_holder_view=d._on_relay_holder_view,
    )
    return d


def _pending_event(sid="s9", tool="Bash", hint="make deploy", host="peer01", name="bravo"):
    return {"bb": 1, "ev": "prompt_pending", "host": host, "name": name,
            "agent": "claude", "tool": tool, "hint": hint, "sid": sid}


async def test_peer_prompt_emits_remote_card(daemon):
    daemon.registry.set_state("claude", "local", WAIT)  # busy: no early yield
    await daemon._on_peer_event(_pending_event())
    (card,) = daemon.cards.queue
    assert card.kind == "remote"
    assert card.title == "[bravo] approve: Bash"
    assert card.body == "make deploy"
    assert ("peer01", "s9") in daemon._remote_pending


async def test_peer_cards_rate_limited_and_deduped(daemon):
    daemon.registry.set_state("claude", "local", WAIT)
    await daemon._on_peer_event(_pending_event(sid="s1"))
    await daemon._on_peer_event(_pending_event(sid="s1"))  # dupe: same content
    await daemon._on_peer_event(_pending_event(sid="s2"))  # rate-limited (<10s)
    assert len(daemon.cards.queue) == 1
    assert len(daemon._remote_pending) == 2  # state still tracked
    # window elapsed: next distinct prompt cards again
    daemon._remote_card_ts["peer01"] -= 11.0
    await daemon._on_peer_event(_pending_event(sid="s3", hint="other thing"))
    assert len(daemon.cards.queue) == 2


async def test_prompt_resolved_retracts_undelivered_card(daemon):
    daemon.registry.set_state("claude", "local", WAIT)
    await daemon._on_peer_event(_pending_event())
    assert len(daemon.cards.queue) == 1
    await daemon._on_peer_event(
        {"bb": 1, "ev": "prompt_resolved", "host": "peer01", "sid": "s9"}
    )
    assert daemon.cards.queue == []
    assert daemon._remote_pending == {} and daemon._remote_cards == {}


async def test_attention_event_cards_only_positive_counts(daemon):
    daemon.registry.set_state("claude", "local", WAIT)
    await daemon._on_peer_event(
        {"bb": 1, "ev": "attention", "host": "peer01", "name": "bravo", "n": 3}
    )
    (card,) = daemon.cards.queue
    assert card.title == "[bravo] 3 waiting"
    daemon._remote_card_ts["peer01"] -= 11.0
    await daemon._on_peer_event(
        {"bb": 1, "ev": "attention", "host": "peer01", "name": "bravo", "n": 0}
    )
    assert len(daemon.cards.queue) == 1  # n=0 never cards


async def test_peer_prompt_triggers_early_yield_when_idle(daemon):
    # ntfy-capable v2 link so the queued remote card is flushed pre-yield
    daemon.negotiated_proto = PROTO_V2
    daemon.hello_ack = HelloAck(proto=2, caps=("ntfy",))
    await daemon._on_peer_event(_pending_event())
    assert daemon.yielding is True
    assert daemon.link.drops == 1
    assert daemon.link.backoff_floor > 0
    assert any('"ntfy"' in line for line in daemon.link.sent)  # card went out first


async def test_peer_prompt_no_yield_while_busy(daemon):
    daemon.registry.set_state("claude", "local", WAIT)
    await daemon._on_peer_event(_pending_event())
    assert daemon.yielding is False and daemon.link.drops == 0


async def test_holder_view_change_wakes_busy_nonholder(daemon):
    daemon.link._connected = False
    daemon.registry.set_state("claude", "local", WAIT)
    daemon._on_relay_holder_view()  # no holder known + local demand -> wake
    assert daemon.link.wakes == 1
    # a known holder means the stick is taken: stay patient
    daemon.relay.peers.observe(_payload(id="p1", holder=True), ("10.0.0.5", 1))
    daemon._on_relay_holder_view()
    assert daemon.link.wakes == 1


async def test_relay_sync_mirrors_waiting_sessions(daemon):
    calls = []

    async def fake_sync(prompts, waiting):
        calls.append((prompts, waiting))

    daemon.relay.sync_local = fake_sync
    session = daemon.registry.set_state("claude", "s1", WAIT)
    session.prompts.append("Claude needs your permission to use Bash")
    await daemon._relay_sync()
    (prompts, waiting) = calls[0]
    assert waiting == 1
    assert prompts["s1"]["hint"] == "Claude needs your permission to use Bash"
    await daemon._relay_sync()  # throttled: one sync per RELAY_SYNC_SECS
    assert len(calls) == 1


async def test_relay_sync_expires_stale_remote_pending(daemon):
    async def fake_sync(prompts, waiting):
        pass

    daemon.relay.sync_local = fake_sync
    now = time.monotonic()
    daemon._remote_pending[("dead", "s1")] = ("Bash", "x", now - 301.0)
    daemon._remote_pending[("p1", "s2")] = ("Bash", "y", now)
    daemon.relay.peers.observe(_payload(id="p1", holder=True), ("10.0.0.5", 1))
    await daemon._relay_sync()
    # expired entry gone; p1's entry gone too (it holds the stick now)
    assert daemon._remote_pending == {}


# ---- disabled by default: zero sockets ----


async def test_relay_disabled_by_default_binds_nothing(home, monkeypatch):
    cfg = load_config(home)
    assert cfg.relay_enabled is False and cfg.relay_active is False
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)

    def explode(*args, **kwargs):
        raise AssertionError("socket touched while relay disabled")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(asyncio, "start_server", explode)
    tasks = await d.start_background()
    assert tasks == [] and d.relay is None


async def test_relay_requires_nonempty_token(home, monkeypatch):
    cfg = load_config(home)
    cfg.relay_enabled = True
    cfg.relay_token = "   "
    assert cfg.relay_active is False
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)

    def explode(*args, **kwargs):
        raise AssertionError("socket touched without a token")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(asyncio, "start_server", explode)
    tasks = await d.start_background()
    assert tasks == [] and d.relay is None


async def test_relay_bind_failure_disables_cleanly(home, monkeypatch):
    async def boom(mgr, port):
        raise OSError("address in use")

    monkeypatch.setattr(relay_mod, "_open_udp", boom)
    cfg = load_config(home)
    cfg.relay_enabled = True
    cfg.relay_token = TOKEN
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    d = Daemon(cfg)
    tasks = await d.start_background()
    assert tasks == [] and d.relay is None


def test_config_relay_parse_and_roundtrip(home):
    from buddy_bridge.config import save_config, to_toml

    cfg = load_config(home)
    cfg.relay_enabled = True
    cfg.relay_port = 50123
    cfg.relay_token = "abc"
    save_config(cfg)
    again = load_config(home)
    assert again.relay_enabled and again.relay_port == 50123 and again.relay_token == "abc"
    assert again.relay_active is True
    assert "[relay]" in to_toml(again)
    # invalid port falls back to the default
    (home / "config.toml").write_text("[relay]\nport = 99999\n", encoding="utf-8")
    assert load_config(home, create=False).relay_port == 48901
