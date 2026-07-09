"""codex_hook stdout discipline: only an allow/deny decision ever reaches
the real stdout; mirrors and every failure path stay silent."""

import importlib
import io
import json
import sys

_ORIG_STDOUT = sys.stdout

from buddy_bridge.hooks import codex_hook  # noqa: E402  (import performs the swap)

# Undo the module's process-global side effect for the rest of the test
# session; the swap mechanics themselves are re-verified with a controlled
# reload in the LAST test of this file.
sys.stdout = _ORIG_STDOUT


def _perm_stdin():
    return json.dumps(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "cdx-1",
            "cwd": "C:/proj",
            "tool_name": "shell",
            "call_id": "call_42",
            "tool_input": {"command": "cargo publish"},
        }
    )


def test_real_stdout_handle_was_kept():
    assert codex_hook._REAL_STDOUT is _ORIG_STDOUT


def test_deny_prints_decision_with_message(monkeypatch):
    monkeypatch.setattr(codex_hook.client, "request", lambda msg, t: {"decision": "deny"})
    out = io.StringIO()
    assert codex_hook.run(_perm_stdin(), out) == 0
    payload = json.loads(out.getvalue())
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Denied via Hardware Buddy"},
        }
    }


def test_allow_prints_decision_without_message(monkeypatch):
    monkeypatch.setattr(codex_hook.client, "request", lambda msg, t: {"decision": "allow"})
    out = io.StringIO()
    codex_hook.run(_perm_stdin(), out)
    decision = json.loads(out.getvalue())["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}  # message ONLY on deny


def test_ask_timeout_daemon_down_print_nothing(monkeypatch):
    for response in ({"decision": "ask"}, None, {"junk": 1}):
        monkeypatch.setattr(codex_hook.client, "request", lambda msg, t, r=response: r)
        out = io.StringIO()
        assert codex_hook.run(_perm_stdin(), out) == 0
        assert out.getvalue() == ""


def test_nogate_env_prints_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(codex_hook.client, "request", lambda msg, t: called.append(1))
    monkeypatch.setenv("BUDDY_BRIDGE_NOGATE", "1")
    out = io.StringIO()
    codex_hook.run(_perm_stdin(), out)
    assert out.getvalue() == ""
    assert called == []


def test_mirrors_print_nothing_to_stdout(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(codex_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    for name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        stdin = json.dumps(
            {"hook_event_name": name, "thread_id": "cdx-9", "cwd": "C:/x", "prompt": "p"}
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert codex_hook.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""  # NOTHING on stdout for mirrors, ever
    assert [m["name"] for m in sent] == ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]
    assert all(m["agent"] == "codex" for m in sent)
    assert all(m["session_id"] == "cdx-9" for m in sent)  # thread_id fallback


def test_permission_request_ipc_shape(monkeypatch):
    seen = {}

    def fake_request(msg, timeout):
        seen.update(msg)
        seen["timeout"] = timeout
        return None

    monkeypatch.setattr(codex_hook.client, "request", fake_request)
    codex_hook.run(_perm_stdin(), io.StringIO())
    assert seen["event"] == "permission_request"
    assert seen["agent"] == "codex"
    assert seen["session_id"] == "cdx-1"
    assert seen["tool_name"] == "shell"
    assert seen["tool_use_id"] == "call_42"
    assert seen["tool_input"] == {"command": "cargo publish"}
    assert seen["timeout"] == 110.0


def test_garbage_stdin_silent(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("}}}not json"))
    assert codex_hook.main() == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "FutureThing"}'))
    assert codex_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_import_time_swap_mechanics():
    """KEEP LAST in this file: reloads the module under sentinel streams."""
    fake_out, fake_err = io.StringIO(), io.StringIO()
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = fake_out, fake_err
        mod = importlib.reload(codex_hook)
        assert mod._REAL_STDOUT is fake_out  # real handle kept for decisions
        assert sys.stdout is fake_err  # stray prints now land on stderr
        print("stray debug output")  # must NOT reach the real stdout
        assert fake_out.getvalue() == ""
        assert "stray debug output" in fake_err.getvalue()
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err
        importlib.reload(codex_hook)  # rebind the module to the restored streams
        sys.stdout = orig_out  # the reload swapped again; undo for the session
