"""Federation merge layer: namespacing caps, merge ordering, TTL expiry,
remote routes, ask cleaning, and the wire-budget fuzz."""

import json

from buddy_bridge.decision import DecisionRouter
from buddy_bridge.federation import (
    MAX_SESSIONS_PER_PEER,
    PROMPT_ID_MAX,
    SID_WIRE_MAX_BYTES,
    STATE_TTL_SECS,
    Federation,
    PeerFed,
)
from buddy_bridge.protocol import build_snapshot, v2_line_budget


def _fed(now=None):
    clock = now if now is not None else [1000.0]
    router = DecisionRouter()
    fed = Federation(router, now_fn=lambda: clock[0])
    return fed, router, clock


def _sess(sid="cli:1", state="wait", title="proj", agent="claude", tok=5, last="ok"):
    return {"sid": sid, "agent": agent, "title": title, "state": state,
            "tok": tok, "last": last}


def _state(name="bravo", sessions=(), prompt=None, ask=None):
    payload = {"name": name, "sessions": list(sessions)}
    if prompt is not None:
        payload["prompt"] = prompt
    if ask is not None:
        payload["ask"] = ask
    return payload


# ---- namespacing ----


def test_sid_namespacing_passthrough_and_cap():
    peer = PeerFed(host_id="p1", k=0)
    assert peer.wire_sid("cli:1") == "r0:cli:1"
    assert peer.wire_sid("cli:1") == "r0:cli:1"  # pinned
    assert len("r0:cli:1".encode()) <= SID_WIRE_MAX_BYTES
    # too long after prefixing -> minted short alias, stable
    minted = peer.wire_sid("cdx:123456")
    assert minted == "r0:s1" and peer.wire_sid("cdx:123456") == "r0:s1"
    # non-ascii orig -> minted
    assert peer.wire_sid("séance") == "r0:s2"
    # every produced sid respects the firmware cap (char[12] => 11 + NUL)
    for orig in ("cli:1", "cdx:123456", "séance", "x" * 40):
        assert len(peer.wire_sid(orig).encode()) <= SID_WIRE_MAX_BYTES


def test_sid_namespacing_unique_across_minted_and_passthrough():
    peer = PeerFed(host_id="p1", k=3)
    seen = {peer.wire_sid(orig) for orig in ("cli:1", "cli:2", "a" * 20, "b" * 20)}
    assert len(seen) == 4


def test_prompt_id_namespacing_passthrough_mint_and_letter():
    peer = PeerFed(host_id="p1", k=2)
    assert peer.wire_prompt_id("p7") == "r2.p7"
    long_id = "toolu_" + "A" * 40
    minted = peer.wire_prompt_id(long_id)
    assert minted == "r2.p1" and len(minted) <= PROMPT_ID_MAX
    assert peer.wire_prompt_id(long_id) == "r2.p1"  # stable
    assert peer.wire_prompt_id("ask_" + "B" * 40, letter="a") == "r2.a2"


def test_local_wire_id_never_collides_with_remote_namespace():
    async def run():
        router = DecisionRouter()
        router.register_remote("r0.p7", "peer01", "p7")
        # a hostile/pathological local tool_use_id in the remote namespace
        pending = router.create("claude", "s1", "r0.p7", "hint")
        assert pending.wire_id == "p1"
        pending2 = router.create("claude", "s1", "r1.whatever", "hint")
        assert pending2.wire_id == "p2"
        # normal ids still pass through
        pending3 = router.create("claude", "s1", "toolu_ok", "hint")
        assert pending3.wire_id == "toolu_ok"

    import asyncio

    asyncio.run(run())


def test_remote_route_register_lookup_unregister():
    router = DecisionRouter()
    router.register_remote("r0.p1", "peer01", "p1")
    assert router.remote_route("r0.p1") == ("peer01", "p1")
    assert router.remote_route_count() == 1
    router.unregister_remote("r0.p1")
    assert router.remote_route("r0.p1") is None
    router.register_remote("", "peer01", "p1")  # blanks ignored
    assert router.remote_route_count() == 0


# ---- folding state in ----


def test_update_cleans_sessions_prefix_and_caps():
    fed, _, _ = _fed()
    cjk = "中文项目名字很长"
    changed = fed.update("peer01", _state(
        name="bravo-station",  # > 6 chars: capped
        sessions=[
            _sess(sid="cli:1", title=cjk, state="wait"),
            _sess(sid="cli:2", state="bogus", tok=-3),
            {"sid": "", "state": "run"},  # no sid: dropped
            "not a dict",
        ],
    ))
    assert changed is True
    peer = fed._peers["peer01"]
    assert peer.name == "bravo-"  # 6 chars
    assert len(peer.sessions) == 2
    first = peer.sessions[0]
    assert first["sid"] == "r0:cli:1"
    assert first["title"].startswith("[bravo-]")
    assert len(first["title"].encode()) <= 24
    assert "?" in first["title"]  # CJK folded by sanitize
    second = peer.sessions[1]
    assert second["state"] == "idle" and second["tok"] == 0
    # unchanged resync -> not changed
    # (prompt/ask untouched, sessions identical)
    assert fed.update("peer01", _state(
        name="bravo-station",
        sessions=[_sess(sid="cli:1", title=cjk, state="wait"),
                  _sess(sid="cli:2", state="bogus", tok=-3)],
    )) is False


def test_update_caps_sessions_per_peer():
    fed, _, _ = _fed()
    sessions = [_sess(sid=f"cli:{i}") for i in range(MAX_SESSIONS_PER_PEER + 4)]
    fed.update("peer01", _state(sessions=sessions))
    assert len(fed._peers["peer01"].sessions) == MAX_SESSIONS_PER_PEER


def test_prompt_fold_routes_and_first_seen():
    fed, router, clock = _fed()
    prompt = {"id": "p9", "tool": "Bash", "hint": "make deploy", "sid": "cli:1", "qn": 9}
    fed.update("peer01", _state(prompt=prompt))
    peer = fed._peers["peer01"]
    assert peer.prompt == {"id": "r0.p9", "tool": "Bash", "hint": "make deploy",
                           "sid": "r0:cli:1", "qn": 3}  # qn clamped
    assert router.remote_route("r0.p9") == ("peer01", "p9")
    assert peer.prompt_first_seen == 1000.0
    clock[0] += 7
    fed.update("peer01", _state(prompt=prompt))  # same prompt persists
    assert peer.prompt_first_seen == 1000.0  # first-seen kept
    clock[0] += 1
    fed.update("peer01", _state(prompt={"id": "p10", "tool": "Bash", "hint": "x"}))
    assert router.remote_route("r0.p9") is None  # replaced: old route dropped
    assert router.remote_route("r0.p10") == ("peer01", "p10")
    assert fed._peers["peer01"].prompt_first_seen == 1008.0
    fed.update("peer01", _state())  # prompt gone
    assert fed._peers["peer01"].prompt is None
    assert router.remote_route_count() == 0


def test_prune_expires_silent_peers_and_routes():
    fed, router, clock = _fed()
    fed.update("peer01", _state(sessions=[_sess()], prompt={"id": "p1", "hint": "h"}))
    clock[0] += 10
    fed.update("peer02", _state(name="carol", sessions=[_sess(sid="cdx:9")]))
    clock[0] += STATE_TTL_SECS - 5  # peer01 now stale, peer02 fresh
    removed = fed.prune()
    assert [p.host_id for p in removed] == ["peer01"]
    assert router.remote_route_count() == 0
    assert fed.has_peer("peer02") and not fed.has_peer("peer01")
    assert fed.counts() == (1, 0, 1)


def test_peer_index_stable_across_expiry():
    fed, _, clock = _fed()
    fed.update("peer01", _state(sessions=[_sess(sid="cli:1")]))
    fed.update("peer02", _state(sessions=[_sess(sid="cli:1")]))
    assert fed._peers["peer02"].k == 1
    clock[0] += STATE_TTL_SECS + 1
    fed.prune()
    fed.update("peer02", _state(sessions=[_sess(sid="cli:1")]))
    assert fed._peers["peer02"].k == 1  # same k after coming back
    assert fed._peers["peer02"].sessions[0]["sid"] == "r1:cli:1"


# ---- merged queries ----


def test_merged_entries_waiting_first_across_hosts_idle_remotes_drop():
    fed, _, _ = _fed()
    fed.update("peer01", _state(name="bravo", sessions=[
        _sess(sid="cli:1", state="wait"),
        _sess(sid="cli:2", state="run"),
        _sess(sid="cli:3", state="idle"),
    ]))
    fed.update("peer02", _state(name="carol", sessions=[
        _sess(sid="cdx:1", state="idle"),
        _sess(sid="cdx:2", state="wait"),
    ]))
    local = [
        {"sid": "cli:9", "agent": "claude", "title": "loc", "state": "run", "tok": 0, "last": ""},
        {"sid": "cli:8", "agent": "claude", "title": "loc", "state": "wait", "tok": 0, "last": ""},
        {"sid": "cli:7", "agent": "claude", "title": "loc", "state": "idle", "tok": 0, "last": ""},
    ]
    merged = fed.merged_entries(local, max_sessions=32)
    states = [(e["state"], e["sid"]) for e in merged]
    assert states == [
        ("wait", "cli:8"), ("wait", "r0:cli:1"), ("wait", "r1:cdx:2"),
        ("run", "cli:9"), ("run", "r0:cli:2"),
        ("idle", "cli:7"), ("idle", "r0:cli:3"), ("idle", "r1:cdx:1"),
    ]
    # clamp to 6: the two remote idles fall off first
    clamped = fed.merged_entries(local, max_sessions=6)
    assert [e["sid"] for e in clamped] == [
        "cli:8", "r0:cli:1", "r1:cdx:2", "cli:9", "r0:cli:2", "cli:7",
    ]


def test_counts_and_pending_math():
    fed, _, _ = _fed()
    fed.update("peer01", _state(sessions=[
        _sess(sid="a", state="wait"), _sess(sid="b", state="run"),
    ], prompt={"id": "p1", "hint": "h", "qn": 2}))
    fed.update("peer02", _state(sessions=[_sess(sid="c", state="idle")],
                                prompt={"id": "p5", "hint": "h"}))
    assert fed.counts() == (3, 1, 1)
    assert fed.pending_total() == (1 + 2) + (1 + 0)
    assert fed.prompt_ids() == {"r0.p1", "r1.p5"}


def test_oldest_remote_prompt_first_seen_ordering():
    fed, _, clock = _fed()
    fed.update("peer01", _state(prompt={"id": "p1", "hint": "first"}))
    clock[0] += 5
    fed.update("peer02", _state(prompt={"id": "p2", "hint": "second"}))
    first_seen, prompt = fed.oldest_remote_prompt()
    assert prompt["id"] == "r0.p1" and first_seen == 1000.0
    fed.drop_prompt("peer01")
    _, prompt = fed.oldest_remote_prompt()
    assert prompt["id"] == "r1.p2"


def test_drop_prompt_and_by_orig():
    fed, router, _ = _fed()
    fed.update("peer01", _state(prompt={"id": "p1", "hint": "h"}))
    assert fed.drop_prompt_by_orig("peer01", "nope") is None
    assert fed.drop_prompt_by_orig("peer01", "p1") == "r0.p1"
    assert router.remote_route_count() == 0
    assert fed.drop_prompt("peer01") is None  # nothing live anymore
    assert fed.oldest_remote_prompt() is None


# ---- asks ----


def test_ask_fold_namespace_and_cleaning():
    fed, _, _ = _fed()
    ask = {
        "id": "toolu_" + "Q" * 40,
        "sid": "cli:1",
        "multiSelect": True,
        "questions": [
            {"header": "Pick", "text": "Which one?", "options": [
                {"label": "A", "desc": "first"}, {"label": "B", "desc": "second"},
            ]},
        ],
    }
    fed.update("peer01", _state(name="bravo", ask=ask))
    asks = fed.remote_asks()
    (wire_id, cleaned), = asks.items()
    assert wire_id == "r0.a1"  # minted (too long), ask letter
    assert cleaned["sid"] == "r0:cli:1"
    assert cleaned["multiSelect"] is True
    assert cleaned["name"] == "bravo"
    assert cleaned["questions"][0]["options"][0] == {"label": "A", "desc": "first"}
    fed.update("peer01", _state(name="bravo"))  # ask gone
    assert fed.remote_asks() == {}


def test_ask_requires_id_and_questions():
    fed, _, _ = _fed()
    fed.update("peer01", _state(ask={"id": "", "questions": [{"text": "x"}]}))
    assert fed.remote_asks() == {}
    fed.update("peer01", _state(ask={"id": "a1", "questions": []}))
    assert fed.remote_asks() == {}
    fed.update("peer01", _state(ask={"id": "a1", "questions": "bogus"}))
    assert fed.remote_asks() == {}


# ---- fuzz: worst-case merged heartbeat still fits the wire ----


def test_fuzz_three_peers_cjk_titles_respect_budget():
    fed, _, _ = _fed()
    cjk = "测试会话标题非常长非常长"
    for p in range(3):
        sessions = [
            _sess(sid=f"cdx:{i}{p}", state=("wait", "run", "idle")[i % 3],
                  title=cjk + str(i), tok=10**9, last=cjk * 3)
            for i in range(8)
        ]
        fed.update(f"peer{p:02d}", _state(
            name=f"机器{p}", sessions=sessions,
            prompt={"id": "toolu_" + "Z" * 38, "tool": "Bash",
                    "hint": cjk * 5, "sid": f"cdx:0{p}", "qn": 3},
        ))
    local = [
        {"sid": "cli:1", "agent": "claude", "title": "local", "state": "wait",
         "tok": 123, "last": "building"},
    ]
    merged = fed.merged_entries(local, max_sessions=12)
    assert len(merged) == 12
    for entry in merged:
        assert len(entry["sid"].encode()) <= SID_WIRE_MAX_BYTES
        assert len(entry["title"].encode()) <= 24
    _, prompt = fed.oldest_remote_prompt()
    assert len(prompt["id"]) <= PROMPT_ID_MAX
    budget = v2_line_budget(4608)
    line = build_snapshot(
        total=25, running=8, waiting=8, msg="8 waiting for you",
        entries=["12:00 " + "x" * 80] * 8, tokens=10**10, tokens_today=10**9,
        prompt=prompt, sessions=merged, budget=budget,
    )
    assert len(line.encode("utf-8")) <= budget
    line.encode("ascii")  # pure-ASCII wire guarantee
    parsed = json.loads(line)
    assert parsed["prompt"]["id"] == prompt["id"]
    assert all(len(s["sid"].encode()) <= SID_WIRE_MAX_BYTES for s in parsed["sessions"])
