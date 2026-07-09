from buddy_bridge.adapters import claude_cli
from buddy_bridge.registry import IDLE, RUN, WAIT, SessionRegistry


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_title_from_cwd():
    reg = SessionRegistry()
    s = reg.upsert("claude", "s1", cwd="C:\\Users\\Arik\\proj\\my-app")
    assert s.title == "my-app"
    s2 = reg.upsert("claude", "s2", cwd="/home/arik/other-proj")
    assert s2.title == "other-proj"


def test_wait_fifo_order():
    reg = SessionRegistry()
    for sid in ("a", "b", "c"):
        reg.set_state("claude", sid, RUN)
    reg.set_state("claude", "b", WAIT)
    reg.set_state("claude", "a", WAIT)
    reg.set_state("claude", "c", WAIT)
    assert [s.sid for s in reg.waiting_fifo()] == ["b", "a", "c"]

    # b resumes running, then waits again -> moves to the back of the queue
    reg.set_state("claude", "b", RUN)
    assert [s.sid for s in reg.waiting_fifo()] == ["a", "c"]
    reg.set_state("claude", "b", WAIT)
    assert [s.sid for s in reg.waiting_fifo()] == ["a", "c", "b"]

    # staying in wait does NOT re-queue
    reg.set_state("claude", "a", WAIT)
    assert [s.sid for s in reg.waiting_fifo()] == ["a", "c", "b"]


def test_counts_and_sorted():
    reg = SessionRegistry()
    reg.set_state("claude", "r1", RUN)
    reg.set_state("claude", "w1", WAIT)
    reg.set_state("codex", "i1", IDLE)
    assert reg.counts() == (3, 1, 1)
    ordered = [s.sid for s in reg.sessions_sorted()]
    assert ordered[0] == "w1"  # waiting first
    assert set(ordered) == {"r1", "w1", "i1"}


def test_reaper_drops_only_stale_idle():
    clock = Clock(1000.0)
    reg = SessionRegistry(now_fn=clock)
    reg.set_state("claude", "idle-old", IDLE)
    reg.set_state("claude", "wait-old", WAIT)
    reg.set_state("claude", "run-old", RUN)
    clock.t += 601
    reg.set_state("claude", "idle-fresh", IDLE)
    assert reg.reap() == 1
    remaining = {s.sid for s in reg.all_sessions()}
    assert remaining == {"wait-old", "run-old", "idle-fresh"}
    # boundary: exactly 600s is not yet stale
    clock.t += 599
    assert reg.reap() == 0
    clock.t += 2
    assert reg.reap() == 1  # idle-fresh crossed the threshold
    assert {s.sid for s in reg.all_sessions()} == {"wait-old", "run-old"}


def test_entries_feed_bounded():
    reg = SessionRegistry()
    for i in range(20):
        reg.add_entry(f"10:{i:02d} entry {i}")
    assert len(reg.entries) == 8
    assert list(reg.entries)[0] == "10:12 entry 12"


def test_adapter_state_machine():
    reg = SessionRegistry()
    base = {"event": "hook", "agent": "claude", "session_id": "s1", "cwd": "/tmp/proj"}

    assert claude_cli.handle_event(reg, {**base, "name": "SessionStart", "source": "startup"})
    assert reg.get("claude", "s1").state == IDLE

    claude_cli.handle_event(reg, {**base, "name": "UserPromptSubmit", "prompt": "do a thing"})
    assert reg.get("claude", "s1").state == RUN

    claude_cli.handle_event(reg, {**base, "name": "PostToolUse", "tool_name": "Bash"})
    assert reg.get("claude", "s1").state == RUN

    claude_cli.handle_event(
        reg, {**base, "name": "Notification", "message": "Claude needs permission to use Bash"}
    )
    s = reg.get("claude", "s1")
    assert s.state == WAIT
    assert s.pending_prompt() == "Claude needs permission to use Bash"
    assert reg.pending_prompt() == "Claude needs permission to use Bash"

    # user answers -> run again, pending prompt cleared
    claude_cli.handle_event(reg, {**base, "name": "UserPromptSubmit", "prompt": "yes"})
    s = reg.get("claude", "s1")
    assert s.state == RUN
    assert s.pending_prompt() is None

    claude_cli.handle_event(reg, {**base, "name": "Stop"})
    assert reg.get("claude", "s1").state == WAIT

    claude_cli.handle_event(reg, {**base, "name": "SessionEnd", "reason": "exit"})
    assert reg.get("claude", "s1") is None


def test_adapter_ignores_garbage():
    reg = SessionRegistry()
    assert not claude_cli.handle_event(reg, {"event": "hook", "name": "Stop"})  # no sid
    assert not claude_cli.handle_event(
        reg, {"event": "hook", "session_id": "x", "name": "NotARealEvent"}
    )
    assert reg.counts() == (0, 0, 0)  # nothing registered


def test_pending_prompt_fifo_source():
    reg = SessionRegistry()
    s1 = reg.set_state("claude", "s1", WAIT)
    s1.prompts.append("first hint")
    s2 = reg.set_state("claude", "s2", WAIT)
    s2.prompts.append("second hint")
    assert reg.pending_prompt() == "first hint"  # longest-waiting session wins
