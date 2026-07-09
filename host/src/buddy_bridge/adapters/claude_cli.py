"""Map mirrored Claude Code hook events onto the session registry.

State machine:
  SessionStart      -> idle   (fresh/resumed session, user about to type)
  UserPromptSubmit  -> run    (Claude is working); pending prompts cleared
  PostToolUse       -> touch  (still working)
  Notification      -> wait   + message queued as pending prompt hint
  Stop              -> wait   (turn finished, session wants the user back)
  SessionEnd        -> dropped
"""

from __future__ import annotations

from ..registry import IDLE, RUN, WAIT, SessionRegistry

AGENT = "claude"


def handle_event(registry: SessionRegistry, msg: dict) -> bool:
    """Apply one mirrored hook event. Returns True when it mutated the registry."""
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
    elif name == "Notification":
        session = registry.set_state(AGENT, sid, WAIT, cwd)
        message = str(msg.get("message") or "").strip()
        if message:
            session.prompts.append(message)
    elif name == "Stop":
        registry.set_state(AGENT, sid, WAIT, cwd)
    elif name == "SessionEnd":
        registry.drop(AGENT, sid)
    else:
        return False
    return True
