"""Scan Codex rollout files (``~/.codex/sessions/**/*.jsonl``) for tokens.

Rollout records (one JSON object per line) we care about:
  {"type":"session_meta","payload":{"id":"...","cwd":"...", ...}}
  {"type":"event_msg","payload":{"type":"token_count",
      "info":{"total_token_usage":{"output_tokens":N, ...}, ...}}}
Everything else is skipped; a garbled line never aborts a file.

The token counter is CUMULATIVE per session, so each sweep emits only a
file's FINAL observed total; UsageLedger.add_cumulative turns that into a
delta against its persisted baseline. Because only the latest total is
applied (never every historical line), re-reading whole files after a
daemon restart cannot double count.

Active-window detection: a sweep only opens files whose mtime falls inside
ACTIVE_WINDOW_SECS; dormant historical rollouts cost nothing. In-memory
byte offsets keep repeat sweeps of an active file incremental.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

ROOT_ENV = "BUDDY_BRIDGE_CODEX_SESSIONS"
SCAN_INTERVAL_SECS = 60.0
ACTIVE_WINDOW_SECS = 48 * 3600.0


def codex_sessions_root() -> Path:
    env = os.environ.get(ROOT_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex" / "sessions"


@dataclass
class CodexSessionInfo:
    session_id: str
    cwd: str
    output_tokens: int  # cumulative total for the session


@dataclass
class _FileState:
    offset: int = 0
    session_id: str = ""
    cwd: str = ""
    last_total: int = -1
    applied_total: int = -1
    pending: bool = field(default=False)


def _parse_record(raw: bytes, state: _FileState) -> None:
    raw = raw.strip()
    if not raw:
        return
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return
    if not isinstance(obj, dict):
        return
    rtype = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    if rtype == "session_meta":
        sid = payload.get("id")
        if isinstance(sid, str) and sid:
            state.session_id = sid
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            state.cwd = cwd
        return
    if rtype == "event_msg" and payload.get("type") == "token_count":
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if usage is None and isinstance(payload.get("total_token_usage"), dict):
            usage = payload["total_token_usage"]  # older shape
        if not isinstance(usage, dict):
            return
        value = usage.get("output_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            state.last_total = int(value)
            state.pending = True


class CodexSessionScanner:
    def __init__(
        self,
        root: Path,
        on_session: Callable[[CodexSessionInfo], None],
        *,
        active_window_secs: float = ACTIVE_WINDOW_SECS,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.root = root
        self.on_session = on_session
        self.active_window_secs = active_window_secs
        self._now = now_fn or time.time
        self._files: dict[str, _FileState] = {}

    def sweep(self) -> list[CodexSessionInfo]:
        """One synchronous pass; returns (and emits) sessions whose total moved."""
        if not self.root.is_dir():
            return []
        emitted: list[CodexSessionInfo] = []
        cutoff = self._now() - self.active_window_secs
        for path in self.root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            state = self._files.setdefault(str(path), _FileState())
            if stat.st_size < state.offset:
                self._files[str(path)] = state = _FileState()  # rewritten file
            if stat.st_size == state.offset:
                continue
            try:
                with path.open("rb") as fh:
                    fh.seek(state.offset)
                    data = fh.read(stat.st_size - state.offset)
            except OSError:
                continue
            last_nl = data.rfind(b"\n")
            if last_nl < 0:
                continue  # only a partial line so far
            consumed = data[: last_nl + 1]
            state.offset += len(consumed)
            for raw in consumed.splitlines():
                _parse_record(raw, state)
            if not state.session_id:
                state.session_id = path.stem  # fallback: rollout filename
            if state.pending and state.last_total != state.applied_total:
                state.applied_total = state.last_total
                state.pending = False
                info = CodexSessionInfo(
                    session_id=state.session_id,
                    cwd=state.cwd,
                    output_tokens=state.last_total,
                )
                emitted.append(info)
                try:
                    self.on_session(info)
                except Exception:  # noqa: BLE001
                    log.exception("codex-sessions: handler crashed")
            else:
                state.pending = False
        return emitted

    async def run(self, interval: float = SCAN_INTERVAL_SECS) -> None:
        import asyncio

        log.info("codex-sessions: scanning %s every %.0fs", self.root, interval)
        while True:
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                log.exception("codex-sessions: sweep failed")
            await asyncio.sleep(interval)
