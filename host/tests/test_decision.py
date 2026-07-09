import asyncio

from buddy_bridge.decision import ASK, DecisionRouter, build_hint


async def test_timeout_resolves_to_ask():
    router = DecisionRouter()
    pending = router.create("claude", "s1", "toolu_abc123", "Bash: rm -rf build")
    decision = await router.wait(pending, timeout=0.05)
    assert decision == ASK
    assert router.pending_count() == 0  # cleaned up after timeout


async def test_resolve_allow_and_deny():
    router = DecisionRouter()
    pending = router.create("claude", "s1", "toolu_1", "hint")

    async def push():
        await asyncio.sleep(0.01)
        assert router.resolve("toolu_1", "allow") is True

    task = asyncio.create_task(push())
    assert await router.wait(pending, timeout=2.0) == "allow"
    await task

    pending2 = router.create("claude", "s1", "toolu_2", "hint2")
    router.resolve("toolu_2", "deny")  # resolve before wait
    assert await router.wait(pending2, timeout=2.0) == "deny"


async def test_resolve_unknown_or_done():
    router = DecisionRouter()
    assert router.resolve("nope", "allow") is False
    pending = router.create("claude", "s1", "toolu_3", "h")
    router.resolve("toolu_3", "allow")
    assert router.resolve("toolu_3", "deny") is False  # already decided
    assert await router.wait(pending, 1.0) == "allow"


async def test_bogus_decision_becomes_ask():
    router = DecisionRouter()
    pending = router.create("claude", "s1", "toolu_4", "h")
    router.resolve("toolu_4", "explode")
    assert await router.wait(pending, 1.0) == ASK


async def test_wire_id_rules():
    router = DecisionRouter()
    # short printable ASCII tool_use_id used verbatim
    p1 = router.create("claude", "s", "toolu_01AbCdEf", "h")
    assert p1.wire_id == "toolu_01AbCdEf"
    # too long -> deterministic short id
    p2 = router.create("claude", "s", "x" * 40, "h")
    assert p2.wire_id == "p1"
    assert p2.tool_use_id == "x" * 40  # canonical id kept in the mapping
    # non-ASCII -> short id
    p3 = router.create("claude", "s", "id-日本語", "h")
    assert p3.wire_id == "p2"
    # empty -> short id
    p4 = router.create("claude", "s", "", "h")
    assert p4.wire_id == "p3"
    # duplicate in-flight tool_use_id must not collide
    p5 = router.create("claude", "s", "toolu_01AbCdEf", "h")
    assert p5.wire_id == "p4"
    for p in (p1, p2, p3, p4, p5):
        router.resolve(p.wire_id, "allow")


async def test_oldest_hint_and_cancel_session():
    router = DecisionRouter()
    a = router.create("claude", "s1", "t1", "first hint")
    b = router.create("claude", "s2", "t2", "second hint")
    assert router.oldest_hint() == "first hint"
    assert router.cancel_session("claude", "s1") == 1
    assert await router.wait(a, 1.0) == ASK
    assert router.oldest_hint() == "second hint"
    router.resolve("t2", "allow")
    assert await router.wait(b, 1.0) == "allow"


def test_build_hint():
    assert build_hint("Bash", {"command": "git  status\n--short"}) == "Bash: git status --short"
    assert build_hint("Write", {"file_path": "C:/x/y.txt", "content": "..."}) == (
        "Write: C:/x/y.txt"
    )
    assert build_hint("WebFetch", {"url": "https://x.test/a"}) == "WebFetch: https://x.test/a"
    assert build_hint("Mystery", {}) == "Mystery"
    assert build_hint("Mystery", None) == "Mystery"
    long = build_hint("Bash", {"command": "x" * 500})
    assert len(long) == 120 and long.endswith("...")
    # dict fallback stays compact json
    assert build_hint("Custom", {"a": 1}) == 'Custom: {"a":1}'
