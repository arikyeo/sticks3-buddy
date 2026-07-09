"""Tail Claude Code project transcripts (``~/.claude/projects/**/*.jsonl``).

Feeds two things off assistant records: activity entries ("HH:MM <text>")
and output-token counts. Byte offsets are persisted per file so a daemon
restart resumes exactly where it left off; files never seen before are
baselined at their current size on the initial sweep (existing history is
NOT replayed). Only complete lines (ending in a newline) are consumed —
a partially-flushed record stays unread until its newline arrives.

The live path uses watchfiles.awatch; all parsing is in pure functions so
tests can drive ``poll_file`` directly with fixture files.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

OFFSETS_FILENAME = "offsets.json"
ROOT_ENV = "BUDDY_BRIDGE_CLAUDE_PROJECTS"
_MISSING_ROOT_RETRY_SECS = 30.0


def claude_projects_root() -> Path:
    env = os.environ.get(ROOT_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "projects"


@dataclass
class AssistantEvent:
    session_id: str
    cwd: str
    text: str
    output_tokens: int
    hhmm: str


def _hhmm_from_timestamp(value: object) -> str:
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%H:%M")
        except ValueError:
            pass
    return time.strftime("%H:%M")


def parse_transcript_line(raw: bytes) -> Optional[AssistantEvent]:
    """One transcript line -> AssistantEvent, or None for anything else."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    tokens = 0
    if isinstance(usage, dict):
        val = usage.get("output_tokens")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            tokens = max(0, int(val))
    text = ""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()
            ):
                text = block["text"].strip()
                break
    elif isinstance(content, str):
        text = content.strip()
    if not tokens and not text:
        return None
    sid = obj.get("sessionId") or obj.get("session_id") or ""
    cwd = obj.get("cwd") or ""
    return AssistantEvent(
        session_id=str(sid),
        cwd=str(cwd),
        text=text,
        output_tokens=tokens,
        hhmm=_hhmm_from_timestamp(obj.get("timestamp")),
    )


class JsonlTailer:
    def __init__(
        self,
        root: Path,
        offsets_path: Path,
        on_event: Callable[[AssistantEvent], None],
    ) -> None:
        self.root = root
        self.offsets_path = offsets_path
        self.on_event = on_event
        self.offsets: dict[str, int] = {}
        self._dirty = False
        self._last_save = 0.0

    # ---- offset persistence ----

    def load_offsets(self) -> None:
        try:
            data = json.loads(self.offsets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.offsets = (
            {
                str(k): int(v)
                for k, v in data.items()
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0
            }
            if isinstance(data, dict)
            else {}
        )
        self._dirty = False

    def save_offsets(self) -> None:
        self.offsets_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.offsets_path.with_name(self.offsets_path.name + ".tmp")
        tmp.write_text(json.dumps(self.offsets, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.offsets_path)
        self._dirty = False
        self._last_save = time.monotonic()

    def save_if_due(self, every: float = 15.0) -> None:
        if self._dirty and time.monotonic() - self._last_save >= every:
            self.save_offsets()

    def flush(self) -> None:
        if self._dirty:
            self.save_offsets()

    # ---- sweeping/polling ----

    def initial_sweep(self) -> None:
        """Baseline untracked files at EOF so existing history is not replayed.
        Files already in the offsets store resume from their persisted offset."""
        if not self.root.is_dir():
            return
        for path in self.root.rglob("*.jsonl"):
            key = str(path)
            if key not in self.offsets:
                try:
                    self.offsets[key] = path.stat().st_size
                    self._dirty = True
                except OSError:
                    continue
        if self._dirty:
            self.save_offsets()

    def poll_file(self, path: Path) -> list[AssistantEvent]:
        """Read new complete lines past the stored offset and parse them."""
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return []
        start = self.offsets.get(key, 0)  # file born after the sweep: read from 0
        if size < start:
            start = 0  # truncated/rewritten
        if size == start:
            return []
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return []
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return []  # no complete line yet; offset stays put
        consumed = data[: last_nl + 1]
        self.offsets[key] = start + len(consumed)
        self._dirty = True
        events: list[AssistantEvent] = []
        for raw in consumed.splitlines():
            event = parse_transcript_line(raw)
            if event is not None:
                events.append(event)
        return events

    def _emit(self, events: list[AssistantEvent]) -> None:
        for event in events:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("jsonl: event handler crashed")

    # ---- live loop ----

    async def run(self) -> None:
        import asyncio

        from watchfiles import awatch

        self.load_offsets()
        while not self.root.is_dir():
            await asyncio.sleep(_MISSING_ROOT_RETRY_SECS)
        self.initial_sweep()
        log.info("jsonl: watching %s (%d files tracked)", self.root, len(self.offsets))
        async for changes in awatch(self.root, recursive=True, step=250):
            for _change, path_str in changes:
                if not path_str.endswith(".jsonl"):
                    continue
                self._emit(self.poll_file(Path(path_str)))
            self.save_if_due()
