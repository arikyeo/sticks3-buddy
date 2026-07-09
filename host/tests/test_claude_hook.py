"""claude_hook stdout contract: JSON only for allow/deny, silence otherwise."""

import io
import json

from buddy_bridge.hooks import claude_hook

DENY_REASON = "User denied on Hardware Buddy device. Do not retry with alternative phrasings."


def _pretooluse_stdin(tool="Bash"):
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "cwd": "C:/proj",
            "tool_name": tool,
            "tool_use_id": "toolu_9",
            "tool_input": {"command": "make deploy"},
        }
    )


def test_deny_prints_exact_decision(monkeypatch):
    monkeypatch.setattr(claude_hook.client, "request", lambda msg, t: {"decision": "deny"})
    out = io.StringIO()
    assert claude_hook.run(_pretooluse_stdin(), out) == 0
    payload = json.loads(out.getvalue())
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }


def test_allow_prints_allow(monkeypatch):
    monkeypatch.setattr(claude_hook.client, "request", lambda msg, t: {"decision": "allow"})
    out = io.StringIO()
    claude_hook.run(_pretooluse_stdin(), out)
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"


def test_ask_daemon_down_or_timeout_prints_nothing(monkeypatch):
    for response in ({"decision": "ask"}, None, {"weird": True}):
        monkeypatch.setattr(claude_hook.client, "request", lambda msg, t, r=response: r)
        out = io.StringIO()
        assert claude_hook.run(_pretooluse_stdin(), out) == 0
        assert out.getvalue() == ""  # fail open to the native prompt


def test_nogate_env_skips_entirely(monkeypatch):
    called = []
    monkeypatch.setattr(
        claude_hook.client, "request", lambda msg, t: called.append(msg) or {"decision": "deny"}
    )
    monkeypatch.setenv("BUDDY_BRIDGE_NOGATE", "1")
    out = io.StringIO()
    assert claude_hook.run(_pretooluse_stdin(), out) == 0
    assert out.getvalue() == ""
    assert called == []


def test_mirror_event_sends_ipc_and_prints_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    out = io.StringIO()
    stdin = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "cwd": "C:/proj",
            "prompt": "please do the thing",
        }
    )
    assert claude_hook.run(stdin, out) == 0
    assert out.getvalue() == ""
    assert sent == [
        {
            "event": "hook",
            "agent": "claude",
            "name": "UserPromptSubmit",
            "session_id": "s1",
            "cwd": "C:/proj",
            "prompt": "please do the thing",
        }
    ]


def test_garbage_stdin_exits_zero_silently(monkeypatch):
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: True)
    out = io.StringIO()
    assert claude_hook.run("not json{{{", out) == 0
    assert claude_hook.run("", out) == 0
    assert claude_hook.run('{"hook_event_name": "SomethingNew"}', out) == 0
    assert out.getvalue() == ""
