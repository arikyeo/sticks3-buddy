"""Relay v2 transport: discovery versioning, static-peer unicast (overlay
networks), TCP-exchange liveness, decision retries, and state_sync cadence."""

import asyncio
import time

from buddy_bridge import relay as relay_mod
from buddy_bridge.config import load_config, save_config, to_toml
from buddy_bridge.relay import (
    EV_STATE_SYNC,
    PEER_TTL_SECS,
    STATE_SYNC_KEEPALIVE_SECS,
    PeerTable,
    RelayManager,
    parse_frame,
    parse_static_peers,
    sign_frame,
)

TOKEN = "sekrit-lan-token"


def _payload(**extra):
    base = {"bb": 1, "v": 2, "id": "peer01", "name": "bravo", "port": 48901,
            "holder": False, "ts": int(time.time())}
    base.update(extra)
    return base


async def _noop_event(payload):
    pass


def _mgr(*, is_holder=lambda: False, static_peers=(), now_fn=None, on_peer_event=None):
    return RelayManager(
        host_id="me",
        host_name="alpha",
        port=48901,
        token=TOKEN,
        is_holder=is_holder,
        on_peer_event=on_peer_event or _noop_event,
        static_peers=static_peers,
        now_fn=now_fn or time.time,
    )


# ---- discovery version ----


def test_discovery_payload_carries_v2():
    assert _mgr()._discovery_payload()["v"] == 2


def test_observe_records_peer_version_default_v1():
    table = PeerTable("me")
    v2 = table.observe(_payload(), ("10.0.0.5", 1))
    assert v2.ver == 2
    legacy = dict(_payload(id="old01"))
    del legacy["v"]
    v1 = table.observe(legacy, ("10.0.0.6", 1))
    assert v1.ver == 1
    bogus = table.observe(_payload(id="odd01", v="nope"), ("10.0.0.7", 1))
    assert bogus.ver == 1


# ---- static peers (overlay networks) ----


def test_parse_static_peers_forms():
    assert parse_static_peers(["10.0.0.7", "mbp.ts.net:50123", " ", ""], 48901) == [
        ("10.0.0.7", 48901),
        ("mbp.ts.net", 50123),
    ]
    # bad port digits -> whole entry treated as a hostname (resolution will
    # quietly fail); non-numeric tail is a name with a colon-less port
    assert parse_static_peers(["x:99999"], 48901) == [("x:99999", 48901)]


async def test_announce_unicasts_to_static_peers(monkeypatch):
    sent = []

    class FakeTransport:
        def sendto(self, data, addr):
            sent.append((data, addr))

    async def fake_resolve(host):
        return {"desk.ts.net": "100.64.0.7", "10.0.0.9": "10.0.0.9"}.get(host)

    monkeypatch.setattr(relay_mod, "_resolve_host", fake_resolve)
    monkeypatch.setattr(relay_mod, "broadcast_addrs", lambda: ["255.255.255.255"])
    mgr = _mgr(static_peers=("10.0.0.9:50000", "desk.ts.net", "offline.ts.net"))
    mgr._udp_transport = FakeTransport()
    await mgr.announce()
    addrs = [a for _, a in sent]
    assert addrs == [
        ("255.255.255.255", 48901),
        ("10.0.0.9", 50000),
        ("100.64.0.7", 48901),  # resolved MagicDNS name, default port
        # offline.ts.net: resolution failed quietly, nothing sent, no raise
    ]
    for data, _ in sent:  # unicast datagrams are signed exactly like broadcast
        assert parse_frame(data, TOKEN) is not None


async def test_resolution_cached_and_recached_on_send_failure(monkeypatch):
    calls = []

    async def fake_resolve(host):
        calls.append(host)
        return "100.64.0.7"

    monkeypatch.setattr(relay_mod, "_resolve_host", fake_resolve)
    monkeypatch.setattr(relay_mod, "broadcast_addrs", lambda: [])
    mgr = _mgr(static_peers=("desk.ts.net",))

    class OkTransport:
        def sendto(self, data, addr):
            pass

    mgr._udp_transport = OkTransport()
    await mgr.announce()
    await mgr.announce()
    assert calls == ["desk.ts.net"]  # second announce used the cache

    class BoomTransport:
        def sendto(self, data, addr):
            raise OSError("unreachable")

    mgr._udp_transport = BoomTransport()
    await mgr.announce()  # failure drops the cache entry, quietly
    mgr._udp_transport = OkTransport()
    await mgr.announce()
    assert calls == ["desk.ts.net", "desk.ts.net"]  # re-resolved after failure


def test_resolve_host_failure_returns_none():
    async def run():
        return await relay_mod._resolve_host("definitely-not-a-real-host.invalid")

    assert asyncio.run(run()) is None


# ---- TCP-exchange liveness ----


def test_touch_keeps_peer_alive_without_udp():
    now = [1000.0]
    table = PeerTable("me", now_fn=lambda: now[0])
    table.observe(_payload(), ("10.0.0.5", 1))
    now[0] += PEER_TTL_SECS - 1
    table.touch("peer01")  # TCP exchange counts as liveness
    now[0] += PEER_TTL_SECS - 1
    assert [p.id for p in table.peers()] == ["peer01"]
    now[0] += PEER_TTL_SECS + 1  # nothing at all for a full TTL
    assert table.peers() == []
    table.touch("peer01")  # unknown/expired: silently ignored
    assert table.peers() == []


async def test_inbound_tcp_event_touches_sender():
    now = [1000.0]
    mgr = _mgr(now_fn=lambda: now[0])
    mgr.peers.observe(_payload(), ("10.0.0.5", 1))
    now[0] += PEER_TTL_SECS - 1

    class FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    reader = asyncio.StreamReader()
    reader.feed_data(sign_frame(
        {"bb": 1, "ev": "attention", "host": "peer01", "name": "bravo", "n": 1,
         "ts": int(now[0])}, TOKEN))  # freshness runs on the manager's clock
    reader.feed_eof()
    await mgr._on_tcp_connection(reader, FakeWriter())
    assert mgr.peers.get("peer01").last_seen == now[0]


# ---- send_to_peer / decision retries ----


async def test_send_to_peer_unknown_and_touch(monkeypatch):
    now = [1000.0]
    mgr = _mgr(now_fn=lambda: now[0])
    assert await mgr.send_to_peer("ghost", {"ev": "x"}) is False
    mgr.peers.observe(_payload(), ("10.0.0.5", 1))
    sent = []

    async def fake_frame(host, port, event):
        sent.append((host, port, event))
        return True

    mgr._send_frame = fake_frame
    now[0] += 50
    assert await mgr.send_to_peer("peer01", {"ev": "x"}) is True
    assert sent == [("10.0.0.5", 48901, {"ev": "x"})]
    assert mgr.peers.get("peer01").last_seen == now[0]  # touched


async def test_send_decision_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(relay_mod, "DECISION_RETRY_DELAY_SECS", 0.0)
    mgr = _mgr()
    mgr.peers.observe(_payload(), ("10.0.0.5", 1))
    outcomes = [False, False, True]
    sent = []

    async def fake_frame(host, port, event):
        sent.append(event)
        return outcomes[len(sent) - 1]

    mgr._send_frame = fake_frame
    assert await mgr.send_decision("peer01", "p7", "allow") is True
    assert len(sent) == 3
    assert sent[0] == {"ev": "decision", "id": "p7", "decision": "allow"}


async def test_send_decision_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(relay_mod, "DECISION_RETRY_DELAY_SECS", 0.0)
    mgr = _mgr()
    mgr.peers.observe(_payload(), ("10.0.0.5", 1))
    attempts = []

    async def fake_frame(host, port, event):
        attempts.append(1)
        return False

    mgr._send_frame = fake_frame
    assert await mgr.send_decision("peer01", "p7", "deny") is False
    assert len(attempts) == relay_mod.DECISION_SEND_ATTEMPTS


# ---- state_sync cadence ----


def _v2_holder(mgr, now=None):
    mgr.peers.observe(_payload(id="hold1", holder=True,
                               ts=int(now if now is not None else time.time())),
                      ("10.0.0.5", 1))


async def test_sync_state_requires_v2_holder():
    now = [1000.0]
    mgr = _mgr(now_fn=lambda: now[0])
    assert await mgr.sync_state({"name": "alpha"}) is False  # no holder at all
    legacy = dict(_payload(id="hold1", holder=True))
    del legacy["v"]
    mgr.peers.observe(legacy, ("10.0.0.5", 1))
    assert mgr.holder_target() is None
    assert await mgr.sync_state({"name": "alpha"}) is False  # v1 holder: fallback


async def test_sync_state_change_keepalive_and_failure():
    clock = [1000.0]
    mgr = _mgr(now_fn=lambda: clock[0])

    _v2_holder(mgr, now=clock[0])
    sent = []
    outcome = [True]

    async def fake_frame(host, port, event):
        sent.append(event)
        return outcome[0]

    mgr._send_frame = fake_frame
    state = {"name": "alpha", "sessions": [{"sid": "cli:1", "state": "wait"}]}
    assert await mgr.sync_state(state) is True
    assert sent[-1]["ev"] == EV_STATE_SYNC and sent[-1]["name"] == "alpha"
    clock[0] += 1
    assert await mgr.sync_state(state) is True  # unchanged: deduped
    assert len(sent) == 1
    assert await mgr.sync_state({**state, "name": "alph2"}) is True  # changed
    assert len(sent) == 2
    clock[0] += STATE_SYNC_KEEPALIVE_SECS + 0.5
    assert await mgr.sync_state({**state, "name": "alph2"}) is True  # keepalive
    assert len(sent) == 3
    clock[0] += 1
    assert await mgr.sync_state({**state, "name": "alph2"}, force=True) is True
    assert len(sent) == 4
    clock[0] += 1
    outcome[0] = False
    assert await mgr.sync_state({**state, "name": "alph3"}) is False  # send failed
    outcome[0] = True
    clock[0] += 0.1
    assert await mgr.sync_state({**state, "name": "alph3"}) is True  # retried at once
    assert len(sent) == 6


async def test_sync_state_holder_resets_and_reset_forces_push():
    clock = [1000.0]
    holder_flag = [False]
    mgr = _mgr(is_holder=lambda: holder_flag[0], now_fn=lambda: clock[0])

    _v2_holder(mgr, now=clock[0])
    sent = []

    async def fake_frame(host, port, event):
        sent.append(event)
        return True

    mgr._send_frame = fake_frame
    state = {"name": "alpha"}
    await mgr.sync_state(state)
    assert len(sent) == 1
    holder_flag[0] = True  # we grabbed the stick: nothing to mirror
    assert await mgr.sync_state(state) is True
    assert len(sent) == 1
    holder_flag[0] = False  # lost it again: sig was reset, full resync
    clock[0] += 0.1
    await mgr.sync_state(state)
    assert len(sent) == 2
    mgr.reset_state_sync()  # holder changed: immediate full push
    clock[0] += 0.1
    await mgr.sync_state(state)
    assert len(sent) == 3


# ---- config ----


def test_config_relay_peers_and_name_short_roundtrip(home):
    cfg = load_config(home)
    cfg.relay_enabled = True
    cfg.relay_token = "abc"
    cfg.relay_peers = ("desk.ts.net", "10.0.0.9:50000")
    cfg.relay_name_short = "alpha"
    save_config(cfg)
    again = load_config(home)
    assert again.relay_peers == ("desk.ts.net", "10.0.0.9:50000")
    assert again.relay_name_short == "alpha"
    text = to_toml(again)
    assert 'peers = ["desk.ts.net", "10.0.0.9:50000"]' in text
    assert 'name_short = "alpha"' in text
    # defaults: empty, and bad types tolerated
    (home / "config.toml").write_text("[relay]\npeers = 3\n", encoding="utf-8")
    fresh = load_config(home, create=False)
    assert fresh.relay_peers == () and fresh.relay_name_short == ""
