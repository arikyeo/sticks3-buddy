# Stdout-discipline pattern ported from Yamiqu/codex-buddy-bridge@e84d4b3 (MIT).
"""Codex CLI hook entry point: ``python -m buddy_bridge.hooks.codex_hook``.

STDOUT DISCIPLINE: Codex parses hook stdout as JSON, so the module swaps
``sys.stdout`` to ``sys.stderr`` at import time while keeping the real
handle. Stray prints from anywhere can then never corrupt the protocol;
only an explicit allow/deny decision is written to the real stdout.

Dispatch on the stdin JSON's hook_event_name:
  PermissionRequest  blocking: ask the daemon (<=110s). allow/deny ->
                     print the decision JSON (message only on deny);
                     anything else -> print NOTHING, exit 0 (Codex falls
                     back to its native approval prompt).
  SessionStart / UserPromptSubmit / PostToolUse / Stop -> fire-and-forget
                     IPC mirrors, never any stdout.

IMPORT DISCIPLINE: only buddy_bridge.ipc.client + stdlib; exit 0 always.
"""

from __future__ import annotations

import json
import os
import sys

from ..ipc import client

# Keep the real handle, then make accidental prints harmless.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

MIRROR_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"})

# Must stay under the installed hook timeout (115s) and above the daemon's
# codex decision timeout (105s) so the daemon answers first.
GATE_WAIT_SECS = 110.0

DENY_MESSAGE = "Denied via Hardware Buddy"


def _text(value: object, limit: int = 200) -> str:
    return str(value)[:limit] if value is not None else ""


def _sid(data: dict) -> str:
    return _text(data.get("session_id") or data.get("thread_id") or data.get("rollout_id"), 64)


def _mirror_payload(name: str, data: dict) -> dict:
    payload = {
        "event": "hook",
        "agent": "codex",
        "name": name,
        "session_id": _sid(data),
        "cwd": _text(data.get("cwd"), 512),
    }
    if name == "SessionStart":
        payload["source"] = _text(data.get("source"), 32)
    elif name == "UserPromptSubmit":
        payload["prompt"] = _text(data.get("prompt"))
    elif name == "PostToolUse":
        payload["tool_name"] = _text(data.get("tool_name"), 64)
    return payload


def _emit_decision(stdout, behavior: str) -> None:
    decision: dict = {"behavior": behavior}
    if behavior == "deny":
        decision["message"] = DENY_MESSAGE
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }
    stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stdout.flush()


def _handle_permission(data: dict, stdout) -> None:
    if os.environ.get("BUDDY_BRIDGE_NOGATE") == "1":
        return
    tool_input = data.get("tool_input")
    request = {
        "event": "permission_request",
        "agent": "codex",
        "session_id": _sid(data),
        "cwd": _text(data.get("cwd"), 512),
        "tool_name": _text(data.get("tool_name"), 64),
        "tool_use_id": _text(data.get("call_id") or data.get("tool_use_id"), 64),
        "tool_input": tool_input if isinstance(tool_input, dict) else {},
    }
    response = client.request(request, GATE_WAIT_SECS)
    decision = response.get("decision") if isinstance(response, dict) else None
    if decision in ("allow", "deny"):
        _emit_decision(stdout, decision)
    # every other outcome: print NOTHING -> native approval prompt continues


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
    elif name == "PermissionRequest":
        _handle_permission(data, stdout)
    return 0


def main() -> int:
    try:
        return run(sys.stdin.read(), _REAL_STDOUT)
    except Exception:
        return 0  # a hook must never break the host CLI


if __name__ == "__main__":
    sys.exit(main())
