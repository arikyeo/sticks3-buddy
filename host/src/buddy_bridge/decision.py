"""Pending permission decisions: hook request -> device button -> resolution.

Wire ids: the buddy echoes the id back with the button decision, and the
firmware line buffer is small, so the wire id is the hook's tool_use_id only
when it is short (<= 39 chars) printable ASCII and not already in flight;
otherwise a deterministic short id ``p<counter>`` is minted. The router maps
wire id -> (agent, sid, canonical tool_use_id) and holds one asyncio.Future
per pending request. A timeout always resolves to "ask" (fail open to the
native prompt).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from typing import Optional

ALLOW = "allow"
DENY = "deny"
ASK = "ask"
DECISIONS = (ALLOW, DENY, ASK)

WIRE_ID_MAX = 39


def _wire_safe(candidate: str) -> bool:
    return 0 < len(candidate) <= WIRE_ID_MAX and all(33 <= ord(c) < 127 for c in candidate)


def extract_detail(tool_input: dict | None, max_chars: int = 120) -> str:
    """The interesting part of a tool call (command/path/url/...), whitespace
    collapsed — this is the wire prompt's ``hint`` (the tool name travels in
    its own ``tool`` field per REFERENCE.md)."""
    detail = ""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "url", "pattern"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        else:
            if tool_input:
                try:
                    detail = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=True)
                except (TypeError, ValueError):
                    detail = ""
    detail = " ".join(detail.split())
    if len(detail) > max_chars:
        detail = detail[: max_chars - 3] + "..."
    return detail


def build_hint(tool_name: str, tool_input: dict | None, max_chars: int = 120) -> str:
    """Human-readable one-liner for the device screen (pre-sanitize)."""
    detail = extract_detail(tool_input, max_chars)
    hint = f"{tool_name}: {detail}" if detail else str(tool_name)
    if len(hint) > max_chars:
        hint = hint[: max_chars - 3] + "..."
    return hint


@dataclass
class PendingDecision:
    wire_id: str
    agent: str
    sid: str
    tool_use_id: str
    hint: str
    future: asyncio.Future
    created: float
    tool: str = ""  # wire prompt "tool" field
    detail: str = ""  # wire prompt "hint" field (tool-less detail)


class DecisionRouter:
    def __init__(self) -> None:
        self._pending: dict[str, PendingDecision] = {}
        self._counter = itertools.count(1)

    def create(
        self,
        agent: str,
        sid: str,
        tool_use_id: str,
        hint: str,
        *,
        tool: str = "",
        detail: str = "",
    ) -> PendingDecision:
        canonical = str(tool_use_id or "")
        if _wire_safe(canonical) and canonical not in self._pending:
            wire_id = canonical
        else:
            wire_id = f"p{next(self._counter)}"
            while wire_id in self._pending:  # paranoia; counter is monotonic
                wire_id = f"p{next(self._counter)}"
        pending = PendingDecision(
            wire_id=wire_id,
            agent=agent,
            sid=sid,
            tool_use_id=canonical,
            hint=hint,
            future=asyncio.get_running_loop().create_future(),
            created=time.monotonic(),
            tool=tool,
            detail=detail,
        )
        self._pending[wire_id] = pending
        return pending

    async def wait(self, pending: PendingDecision, timeout: float) -> str:
        """Block until the device decides or the timeout fires ("ask")."""
        try:
            return await asyncio.wait_for(pending.future, timeout)
        except asyncio.TimeoutError:
            return ASK
        finally:
            self._pending.pop(pending.wire_id, None)

    def resolve(self, wire_id: str, decision: str) -> bool:
        """Apply a device decision. False when the id is unknown/already done."""
        pending = self._pending.get(wire_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision if decision in DECISIONS else ASK)
        return True

    def cancel_session(self, agent: str, sid: str) -> int:
        """Resolve every pending decision of a session to "ask" (session ended)."""
        cancelled = 0
        for pending in list(self._pending.values()):
            if pending.agent == agent and pending.sid == sid and not pending.future.done():
                pending.future.set_result(ASK)
                cancelled += 1
        return cancelled

    def oldest(self) -> Optional[PendingDecision]:
        if not self._pending:
            return None
        return min(self._pending.values(), key=lambda p: p.created)

    def oldest_hint(self) -> Optional[str]:
        pending = self.oldest()
        return pending.hint if pending else None

    def oldest_for(self, agent: str, sid: str) -> Optional[PendingDecision]:
        """Oldest pending decision belonging to one session, if any."""
        mine = [p for p in self._pending.values() if p.agent == agent and p.sid == sid]
        return min(mine, key=lambda p: p.created) if mine else None

    def session_pending_count(self, agent: str, sid: str) -> int:
        return sum(1 for p in self._pending.values() if p.agent == agent and p.sid == sid)

    def pending_count(self) -> int:
        return len(self._pending)
