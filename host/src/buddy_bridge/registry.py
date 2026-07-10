"""In-memory registry of mirrored agent sessions.

Keyed by (agent, session_id). Tracks per-session state run|wait|idle, a
wait-FIFO sequence so the buddy surfaces the longest-waiting session first,
last_seen for the reaper, cumulative tokens, and a small deque of pending
prompt hints (permission requests / attention messages). A global bounded
entries feed carries recent "HH:MM <text>" activity lines for the snapshot.

The reaper drops sessions that have sat idle for more than IDLE_REAP_SECS;
running/waiting sessions are never reaped (they still need attention).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePath, PureWindowsPath
from typing import Callable, Optional

RUN = "run"
WAIT = "wait"
IDLE = "idle"
STATES = (RUN, WAIT, IDLE)

IDLE_REAP_SECS = 600.0
ENTRY_FEED_MAX = 8
PROMPTS_MAX = 4

WIRE_SID_PREFIX = {"claude": "cli", "codex": "cdx"}


def title_from_cwd(cwd: str) -> str:
    """Session title = basename of its working directory."""
    if not cwd:
        return ""
    path = PureWindowsPath(cwd) if ("\\" in cwd or ":" in cwd[:3]) else PurePath(cwd)
    return path.name or str(path)


@dataclass
class Session:
    agent: str
    sid: str
    cwd: str = ""
    title: str = ""
    state: str = IDLE
    wait_seq: int = 0  # >0 while waiting; FIFO order of entering wait
    last_seen: float = 0.0
    tokens: int = 0
    last_line: str = ""  # last activity text, for the v2 per-session display
    prompts: deque = field(default_factory=lambda: deque(maxlen=PROMPTS_MAX))

    def pending_prompt(self) -> Optional[str]:
        return self.prompts[-1] if self.prompts else None


class SessionRegistry:
    def __init__(self, now_fn: Callable[[], float] = time.time) -> None:
        self._now = now_fn
        self._sessions: dict[tuple[str, str], Session] = {}
        self._wait_counter = 0
        # Wire sid pinning: (agent, session_id) -> "cli:N"/"cdx:N". Counters
        # are monotonic and the map is never cleaned, so for the registry's
        # (= daemon's) lifetime a wire sid can never be reused for a
        # different session, and a session that drops and reappears keeps
        # the sid the device already knows.
        self._wire_sids: dict[tuple[str, str], str] = {}
        self._wire_counters: dict[str, int] = {}
        self.entries: deque[str] = deque(maxlen=ENTRY_FEED_MAX)
        self.version = 0  # bumped on every mutation (cheap change detection)

    # ---- mutation ----

    def _bump(self) -> None:
        self.version += 1

    def upsert(self, agent: str, sid: str, cwd: str | None = None) -> Session:
        key = (agent, sid)
        session = self._sessions.get(key)
        if session is None:
            session = Session(agent=agent, sid=sid)
            self._sessions[key] = session
        if cwd:
            session.cwd = cwd
            session.title = title_from_cwd(cwd)
        session.last_seen = self._now()
        self._bump()
        return session

    def set_state(self, agent: str, sid: str, state: str, cwd: str | None = None) -> Session:
        if state not in STATES:
            state = IDLE
        session = self.upsert(agent, sid, cwd)
        if state == WAIT:
            if session.state != WAIT:
                self._wait_counter += 1
                session.wait_seq = self._wait_counter
        else:
            session.wait_seq = 0
        session.state = state
        self._bump()
        return session

    def drop(self, agent: str, sid: str) -> bool:
        removed = self._sessions.pop((agent, sid), None) is not None
        if removed:
            self._bump()
        return removed

    def add_tokens(self, agent: str, sid: str, tokens: int) -> None:
        if tokens <= 0:
            return
        session = self.upsert(agent, sid)
        session.tokens += tokens

    def add_entry(self, line: str) -> None:
        if line:
            self.entries.append(line)
            self._bump()

    def note_activity(self, agent: str, sid: str, text: str) -> None:
        """Record a session's last activity line (v2 ``last``). No resurrect:
        only sessions that already exist are touched."""
        session = self._sessions.get((agent, sid))
        if session is None or not text:
            return
        session.last_line = text
        self._bump()

    def reap(self) -> int:
        """Drop sessions idle for > IDLE_REAP_SECS. Returns count removed."""
        now = self._now()
        stale = [
            key
            for key, s in self._sessions.items()
            if s.state == IDLE and now - s.last_seen > IDLE_REAP_SECS
        ]
        for key in stale:
            del self._sessions[key]
        if stale:
            self._bump()
        return len(stale)

    # ---- queries ----

    def get(self, agent: str, sid: str) -> Optional[Session]:
        return self._sessions.get((agent, sid))

    def wire_sid(self, agent: str, sid: str) -> str:
        """Short device-facing sid, stable for this registry's lifetime."""
        key = (agent, sid)
        pinned = self._wire_sids.get(key)
        if pinned is not None:
            return pinned
        prefix = WIRE_SID_PREFIX.get(agent, (agent[:3] or "unk"))
        self._wire_counters[prefix] = self._wire_counters.get(prefix, 0) + 1
        pinned = f"{prefix}:{self._wire_counters[prefix]}"
        self._wire_sids[key] = pinned
        return pinned

    def all_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def waiting_fifo(self) -> list[Session]:
        return sorted(
            (s for s in self._sessions.values() if s.state == WAIT),
            key=lambda s: s.wait_seq,
        )

    def sessions_sorted(self) -> list[Session]:
        """Waiting first (FIFO), then running (most recent first), then idle."""
        waiting = self.waiting_fifo()
        running = sorted(
            (s for s in self._sessions.values() if s.state == RUN),
            key=lambda s: -s.last_seen,
        )
        idle = sorted(
            (s for s in self._sessions.values() if s.state == IDLE),
            key=lambda s: -s.last_seen,
        )
        return waiting + running + idle

    def counts(self) -> tuple[int, int, int]:
        total = len(self._sessions)
        running = sum(1 for s in self._sessions.values() if s.state == RUN)
        waiting = sum(1 for s in self._sessions.values() if s.state == WAIT)
        return total, running, waiting

    def pending_prompt(self) -> Optional[str]:
        """Hint from the longest-waiting session that has one."""
        for session in self.waiting_fifo():
            hint = session.pending_prompt()
            if hint:
                return hint
        return None

    def total_tokens(self) -> int:
        return sum(s.tokens for s in self._sessions.values())
