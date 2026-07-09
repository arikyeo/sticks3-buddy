"""Map mirrored Codex CLI hook events onto the session registry.

Codex's hook set has no Notification/SessionEnd mirror; Stop marks the turn
finished (waiting for the user) and the registry reaper eventually clears
sessions that never come back. SessionEnd is handled anyway in case a future
Codex version grows one.
"""

from __future__ import annotations

from ..registry import IDLE, RUN, SessionRegistry, WAIT

AGENT = "codex"


def handle_event(registry: SessionRegistry, msg: dict) -> bool:
    name = msg.get("name")
    sid = str(msg.get("session_id") or "")
    if not sid or not isinstance(name, str):
        return False
    cwd = str(msg.get("cwd") or "") or None

    if name == "SessionStart":
        registry.set_state(AGENT, sid, IDLE, cwd)
    elif name == "UserPromptSubmit":
        session = registry.set_state(AGENT, sid, RUN, cwd)
        session.prompts.clear()
    elif name == "PostToolUse":
        registry.upsert(AGENT, sid, cwd)
    elif name == "Stop":
        registry.set_state(AGENT, sid, WAIT, cwd)
    elif name == "SessionEnd":
        registry.drop(AGENT, sid)
    else:
        return False
    return True
