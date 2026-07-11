"""Claude Code hook entry point: ``python -m buddy_bridge.hooks.claude_hook``.

One script for every registered hook event; dispatch is on the stdin JSON's
``hook_event_name``. Mirror events are fire-and-forget IPC sends. The
PreToolUse gate (installed only when [claude] gate=true) blocks for a device
decision and prints Claude's hookSpecificOutput JSON ONLY when the daemon
returned allow/deny — every other path (timeout, daemon down, device
disconnected, "ask") prints nothing and exits 0, failing open to the native
permission prompt.

AskUserQuestion: always mirrored (fire-and-forget display). When installed
with ``--answer-asks`` ([telegram] answer_asks=true) it ALSO blocks on an
"ask_answer" request; a Telegram-chosen answer is emitted as
permissionDecision "allow" + updatedInput{questions, answers} (the
documented AskUserQuestion answer contract), anything else prints nothing
and the native terminal dialog takes over.

IMPORT DISCIPLINE: this module may import ONLY buddy_bridge.ipc.client and
the standard library. It must never crash the host CLI: exit code is 0 on
every path.
"""

from __future__ import annotations

import json
import os
import sys

from ..ipc import client

MIRROR_EVENTS = frozenset(
    {"SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "PostToolUse", "Notification"}
)

# Must stay under the installed hook timeout (330s) and above the daemon's
# device-decision timeout (300s) so the daemon answers first.
GATE_WAIT_SECS = 320.0

DENY_REASON = "User denied on Hardware Buddy device. Do not retry with alternative phrasings."


def _text(value: object, limit: int = 200) -> str:
    return str(value)[:limit] if value is not None else ""


def _mirror_payload(name: str, data: dict) -> dict:
    payload = {
        "event": "hook",
        "agent": "claude",
        "name": name,
        "session_id": _text(data.get("session_id"), 64),
        "cwd": _text(data.get("cwd"), 512),
    }
    if name == "SessionStart":
        payload["source"] = _text(data.get("source"), 32)
    elif name == "SessionEnd":
        payload["reason"] = _text(data.get("reason"), 64)
    elif name == "UserPromptSubmit":
        payload["prompt"] = _text(data.get("prompt"))
    elif name == "Notification":
        payload["message"] = _text(data.get("message"))
    elif name == "PostToolUse":
        payload["tool_name"] = _text(data.get("tool_name"), 64)
        payload["tool_use_id"] = _text(data.get("tool_use_id"), 64)
    return payload


ASK_TOOL = "AskUserQuestion"
ASK_MAX_QUESTIONS = 4
ANSWER_ASKS_FLAG = "--answer-asks"
# Like GATE_WAIT_SECS: below the installed hook timeout (330s), above the
# daemon's own answer window (claude_decision, 300s default) so the daemon
# always answers (or times out to {"answers": null}) first.
ASK_WAIT_SECS = 320.0


def _forward_ask(data: dict, tool_input: dict) -> None:
    """AskUserQuestion is NEVER gated (SAFE_TOOLS) and must never block —
    but its tool_input carries the full question payload, which a v2 stick
    can render. Forward it fire-and-forget; every failure is a no-op."""
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return
    first = questions[0] if isinstance(questions[0], dict) else {}
    client.send_event(
        {
            "event": "ask_pending",
            "agent": "claude",
            "session_id": _text(data.get("session_id"), 64),
            "tool_use_id": _text(data.get("tool_use_id"), 64),
            "questions": questions[:ASK_MAX_QUESTIONS],
            "multiSelect": bool(first.get("multiSelect")),
        }
    )


def _answer_ask(data: dict, tool_input: dict, stdout) -> None:
    """Blocking AskUserQuestion answering (installed with --answer-asks,
    i.e. [telegram] answer_asks=true): ask the daemon for an answer chosen
    on Telegram. When one arrives, emit permissionDecision "allow" with
    ``updatedInput`` = the ORIGINAL tool_input plus the ``answers`` map
    ({question text: chosen label(s)}) — Claude Code then runs the tool
    with pre-supplied answers and never shows the terminal dialog. On
    timeout / daemon down / feature off the daemon returns
    {"answers": null} (or nothing), we print NOTHING, and the native
    terminal dialog takes over: fail open, never brick the CLI."""
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return
    response = client.request(
        {
            "event": "ask_answer",
            "agent": "claude",
            "session_id": _text(data.get("session_id"), 64),
            "tool_use_id": _text(data.get("tool_use_id"), 64),
            "questions": questions[:ASK_MAX_QUESTIONS],
        },
        ASK_WAIT_SECS,
    )
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict) or not answers:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "answers": answers},
        }
    }
    stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
    stdout.flush()


def _handle_pretooluse(data: dict, stdout, answer_asks: bool = False) -> None:
    if os.environ.get("BUDDY_BRIDGE_NOGATE") == "1":
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if _text(data.get("tool_name"), 64) == ASK_TOOL:
        _forward_ask(data, tool_input)  # display mirror: no request, no output
        if answer_asks:
            _answer_ask(data, tool_input, stdout)
        return
    request = {
        "event": "permission_request",
        "agent": "claude",
        "session_id": _text(data.get("session_id"), 64),
        "cwd": _text(data.get("cwd"), 512),
        "tool_name": _text(data.get("tool_name"), 64),
        "tool_use_id": _text(data.get("tool_use_id"), 64),
        "tool_input": tool_input,
    }
    response = client.request(request, GATE_WAIT_SECS)
    decision = response.get("decision") if isinstance(response, dict) else None
    if decision not in ("allow", "deny"):
        # timeout / daemon unreachable / device not connected / "ask":
        # print NOTHING -> Claude Code falls back to its native prompt.
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": (
                DENY_REASON if decision == "deny" else "Approved on Hardware Buddy device."
            ),
        }
    }
    stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
    stdout.flush()


def run(raw: str, stdout, *, answer_asks: bool = False) -> int:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    name = str(data.get("hook_event_name") or "")
    if name in MIRROR_EVENTS:
        client.send_event(_mirror_payload(name, data))
    elif name == "PreToolUse":
        _handle_pretooluse(data, stdout, answer_asks)
    return 0


def main() -> int:
    try:
        answer_asks = ANSWER_ASKS_FLAG in sys.argv[1:]
        return run(sys.stdin.read(), sys.stdout, answer_asks=answer_asks)
    except Exception:
        return 0  # a hook must never break the host CLI


if __name__ == "__main__":
    sys.exit(main())
