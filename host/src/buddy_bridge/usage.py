"""Token usage ledger persisted at ``<home>/ledger.json``.

Tracks lifetime output tokens, a tokens_today counter that rolls over at
LOCAL midnight (day key = local calendar date), additive per-session totals,
and per-session cumulative baselines for agents that report a running counter
(Codex token_count events) instead of per-message deltas.

Loads are tolerant: unknown keys are ignored, wrong-typed values fall back to
defaults, a garbled file starts a fresh ledger rather than crashing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

LEDGER_FILENAME = "ledger.json"


def day_key(ts: float) -> str:
    lt = time.localtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(k): _int(v)
        for k, v in value.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


class UsageLedger:
    def __init__(self, path: Path, now_fn: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now_fn
        self.lifetime = 0
        self.tokens_today = 0
        self.day = ""
        self.sessions: dict[str, int] = {}
        self.baselines: dict[str, int] = {}
        self._dirty = False
        self.load()

    # ---- persistence ----

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.lifetime = _int(data.get("lifetime"))
        self.tokens_today = _int(data.get("tokens_today"))
        self.day = data.get("day") if isinstance(data.get("day"), str) else ""
        self.sessions = _int_map(data.get("sessions"))
        self.baselines = _int_map(data.get("baselines"))
        self._dirty = False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lifetime": self.lifetime,
            "tokens_today": self.tokens_today,
            "day": self.day,
            "sessions": self.sessions,
            "baselines": self.baselines,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

    def save_if_dirty(self) -> bool:
        if self._dirty:
            self.save()
            return True
        return False

    # ---- accounting ----

    def _rollover(self, ts: float) -> None:
        key = day_key(ts)
        if key != self.day:
            self.day = key
            self.tokens_today = 0
            self._dirty = True

    def add(self, agent: str, sid: str, tokens: int, ts: float | None = None) -> int:
        """Add a per-message token delta. Returns the amount actually added."""
        if tokens <= 0:
            return 0
        stamp = self._now() if ts is None else ts
        self._rollover(stamp)
        self.lifetime += tokens
        self.tokens_today += tokens
        key = f"{agent}:{sid}"
        self.sessions[key] = self.sessions.get(key, 0) + tokens
        self._dirty = True
        return tokens

    def add_cumulative(self, agent: str, sid: str, cumulative: int, ts: float | None = None) -> int:
        """Feed a monotonically-increasing per-session counter (Codex style).

        Only the positive delta over the stored baseline is added. The first
        sighting of a session, or a counter that went backwards (restart),
        re-baselines without counting anything.
        """
        if cumulative < 0:
            return 0
        key = f"{agent}:{sid}"
        base = self.baselines.get(key)
        self.baselines[key] = cumulative
        if base is None or cumulative <= base:
            self._dirty = self._dirty or base != cumulative
            return 0
        return self.add(agent, sid, cumulative - base, ts)

    # ---- queries ----

    def totals(self, ts: float | None = None) -> tuple[int, int]:
        """(lifetime, tokens_today) — rolls the day over first so a fresh
        morning reads 0 even before the first add."""
        self._rollover(self._now() if ts is None else ts)
        return self.lifetime, self.tokens_today

    def session_tokens(self, agent: str, sid: str) -> int:
        return self.sessions.get(f"{agent}:{sid}", 0)
