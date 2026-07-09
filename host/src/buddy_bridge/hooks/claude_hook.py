"""Claude Code hook entry point: ``python -m buddy_bridge.hooks.claude_hook``.

One script for every registered hook event; dispatch is on the stdin JSON's
``hook_event_name``. Mirror events are fire-and-forget IPC sends. The
PreToolUse gate (installed only when [claude] gate=true) blocks for a device
decision and prints Claude's hookSpecificOutput JSON ONLY when the daemon
returned allow/deny — every other path (timeout, daemon down, device
disconnected, "ask") prints nothing and exits 0, failing open to the native
permission prompt.

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
    return payload


def _handle_pretooluse(data: dict, stdout) -> None:
    if os.environ.get("BUDDY_BRIDGE_NOGATE") == "1":
        return
    tool_input = data.get("tool_input")
    request = {
        "event": "permission_request",
        "agent": "claude",
        "session_id": _text(data.get("session_id"), 64),
        "cwd": _text(data.get("cwd"), 512),
        "tool_name": _text(data.get("tool_name"), 64),
        "tool_use_id": _text(data.get("tool_use_id"), 64),
        "tool_input": tool_input if isinstance(tool_input, dict) else {},
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


def run(raw: str, stdout) -> int:
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
        _handle_pretooluse(data, stdout)
    return 0


def main() -> int:
    try:
        return run(sys.stdin.read(), sys.stdout)
    except Exception:
        return 0  # a hook must never break the host CLI


if __name__ == "__main__":
    sys.exit(main())
