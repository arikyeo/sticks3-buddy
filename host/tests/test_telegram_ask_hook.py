"""--answer-asks hook path (AskUserQuestion answering) + settings install.

The hook contract mirrors the gate: JSON output ONLY when the daemon hands
back real answers; every other path prints nothing so the terminal dialog
takes over."""

import io
import json

from buddy_bridge.hooks import claude_hook
from buddy_bridge.install import claude_settings

QUESTIONS = [
    {
        "question": "Which format?",
        "header": "Format",
        "options": [
            {"label": "Summary", "description": "Brief"},
            {"label": "Detailed", "description": "Full"},
        ],
        "multiSelect": False,
    }
]


def _ask_stdin(questions=None):
    return json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "C:/proj",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu_ask",
        "tool_input": {"questions": QUESTIONS if questions is None else questions},
    })


def test_answer_emits_allow_with_updated_input(monkeypatch):
    requests = []
    monkeypatch.setattr(
        claude_hook.client, "send_event", lambda msg: True
    )

    def fake_request(msg, timeout):
        requests.append((msg, timeout))
        return {"answers": {"Which format?": "Summary"}}

    monkeypatch.setattr(claude_hook.client, "request", fake_request)
    out = io.StringIO()
    assert claude_hook.run(_ask_stdin(), out, answer_asks=True) == 0
    payload = json.loads(out.getvalue())
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                # the ORIGINAL questions ride along (required by the tool)
                "questions": QUESTIONS,
                "answers": {"Which format?": "Summary"},
            },
        }
    }
    (msg, timeout), = requests
    assert msg["event"] == "ask_answer"
    assert msg["tool_use_id"] == "toolu_ask"
    assert timeout == claude_hook.ASK_WAIT_SECS


def test_no_answer_prints_nothing(monkeypatch):
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: True)
    for response in ({"answers": None}, {"answers": {}}, None, {"weird": 1}):
        monkeypatch.setattr(claude_hook.client, "request", lambda m, t, r=response: r)
        out = io.StringIO()
        assert claude_hook.run(_ask_stdin(), out, answer_asks=True) == 0
        assert out.getvalue() == ""  # native terminal dialog takes over


def test_flag_off_never_blocks(monkeypatch):
    """Without --answer-asks the ask stays fire-and-forget display only."""
    sent, requested = [], []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(
        claude_hook.client, "request", lambda m, t: requested.append(m) or {"answers": {}}
    )
    out = io.StringIO()
    assert claude_hook.run(_ask_stdin(), out) == 0
    assert out.getvalue() == ""
    assert [m["event"] for m in sent] == ["ask_pending"]
    assert requested == []  # no blocking request without the flag


def test_display_mirror_still_sent_alongside_answer(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(claude_hook.client, "request", lambda m, t: {"answers": None})
    out = io.StringIO()
    claude_hook.run(_ask_stdin(), out, answer_asks=True)
    assert [m["event"] for m in sent] == ["ask_pending"]  # stick display unaffected


def test_empty_questions_never_requests(monkeypatch):
    requested = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: True)
    monkeypatch.setattr(
        claude_hook.client, "request", lambda m, t: requested.append(m) or None
    )
    out = io.StringIO()
    assert claude_hook.run(_ask_stdin(questions=[]), out, answer_asks=True) == 0
    assert out.getvalue() == ""
    assert requested == []


# ---- settings install variants ----


def test_install_answer_asks_blocking_entry(tmp_path):
    settings = tmp_path / "settings.json"
    assert claude_settings.install(settings, answer_asks=True) is True
    data = json.loads(settings.read_text(encoding="utf-8"))
    (ask_group,) = [
        g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "AskUserQuestion"
    ]
    (hook,) = ask_group["hooks"]
    assert hook["command"].endswith("--answer-asks")
    assert claude_settings.MARKER in hook["command"]
    assert hook["timeout"] == claude_settings.GATE_TIMEOUT_SECS


def test_install_default_stays_nonblocking(tmp_path):
    settings = tmp_path / "settings.json"
    claude_settings.install(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    (ask_group,) = [
        g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "AskUserQuestion"
    ]
    (hook,) = ask_group["hooks"]
    assert "--answer-asks" not in hook["command"]
    assert "timeout" not in hook


def test_install_toggle_removes_stale_variant(tmp_path):
    settings = tmp_path / "settings.json"
    claude_settings.install(settings, answer_asks=True)
    assert claude_settings.install(settings, answer_asks=False) is True  # rewritten
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [
        h["command"]
        for g in data["hooks"]["PreToolUse"]
        for h in g["hooks"]
    ]
    assert all("--answer-asks" not in c for c in commands)
    # idempotent once current
    assert claude_settings.install(settings, answer_asks=False) is False
