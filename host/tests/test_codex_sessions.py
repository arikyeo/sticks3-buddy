import json

from buddy_bridge.adapters import codex
from buddy_bridge.adapters.codex_sessions import CodexSessionScanner
from buddy_bridge.registry import IDLE, RUN, WAIT, SessionRegistry


def _meta(sid="cdx-abc", cwd="C:\\proj\\demo"):
    return json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}) + "\n"


def _tokens(total):
    return (
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 999, "output_tokens": total},
                        "last_token_usage": {"output_tokens": 5},
                    },
                },
            }
        )
        + "\n"
    )


def _scanner(tmp_path, emitted):
    return CodexSessionScanner(tmp_path / "sessions", on_session=emitted.append)


def _rollout(tmp_path, name="rollout-2026-07-10-cdx-abc.jsonl"):
    d = tmp_path / "sessions" / "2026" / "07" / "10"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def test_sweep_parses_meta_and_final_token_count(tmp_path):
    f = _rollout(tmp_path)
    f.write_text(
        _meta()
        + '{"type":"turn_context","payload":{"model":"gpt-5.4"}}\n'
        + "garbage not json\n"
        + _tokens(150)
        + _tokens(400),
        encoding="utf-8",
    )
    emitted = []
    scanner = _scanner(tmp_path, emitted)
    got = scanner.sweep()
    assert len(got) == 1
    info = got[0]
    assert info.session_id == "cdx-abc"
    assert info.cwd == "C:\\proj\\demo"
    assert info.output_tokens == 400  # only the FINAL total is applied
    assert emitted == got

    # unchanged file: nothing re-emitted
    assert scanner.sweep() == []

    # counter grows -> one new emission with the new total
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_tokens(500))
    got2 = scanner.sweep()
    assert [i.output_tokens for i in got2] == [500]


def test_sweep_skips_stale_files_outside_active_window(tmp_path):
    import os

    f = _rollout(tmp_path)
    f.write_text(_meta() + _tokens(100), encoding="utf-8")
    old = 1_600_000_000  # 2020
    os.utime(f, (old, old))
    scanner = CodexSessionScanner(tmp_path / "sessions", on_session=lambda i: None)
    assert scanner.sweep() == []


def test_sweep_session_id_falls_back_to_filename(tmp_path):
    f = _rollout(tmp_path, name="rollout-xyz.jsonl")
    f.write_text(_tokens(42), encoding="utf-8")  # no session_meta at all
    emitted = []
    _scanner(tmp_path, emitted).sweep()
    assert [i.session_id for i in emitted] == ["rollout-xyz"]


def test_sweep_missing_root_is_noop(tmp_path):
    scanner = CodexSessionScanner(tmp_path / "nope", on_session=lambda i: None)
    assert scanner.sweep() == []


def test_codex_adapter_state_machine():
    reg = SessionRegistry()
    base = {"event": "hook", "agent": "codex", "session_id": "cdx-1", "cwd": "/w/repo"}
    assert codex.handle_event(reg, {**base, "name": "SessionStart"})
    assert reg.get("codex", "cdx-1").state == IDLE
    assert reg.get("codex", "cdx-1").title == "repo"
    codex.handle_event(reg, {**base, "name": "UserPromptSubmit", "prompt": "go"})
    assert reg.get("codex", "cdx-1").state == RUN
    codex.handle_event(reg, {**base, "name": "PostToolUse", "tool_name": "shell"})
    assert reg.get("codex", "cdx-1").state == RUN
    codex.handle_event(reg, {**base, "name": "Stop"})
    assert reg.get("codex", "cdx-1").state == WAIT
    codex.handle_event(reg, {**base, "name": "SessionEnd"})
    assert reg.get("codex", "cdx-1") is None
    assert not codex.handle_event(reg, {**base, "name": "Whatever"})
