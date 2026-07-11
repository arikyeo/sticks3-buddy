"""Append-only audit log of gate outcomes at ``<home>/audit.jsonl``.

One JSON object per line: ts (ISO-8601 local), host, agent, sid, tool,
hint (exactly as shown on the device), decision, source, and — for remote
sources that identify an actor — ``by`` (e.g. the Telegram chat id).

source values:
  device            the buddy's button decided
  telegram          an allowlisted Telegram chat decided/answered
                    (``by`` = chat id)
  auto_allow        a matchers.toml auto_allow rule decided
  timeout_fallback  device never answered; fell back to "ask"
  daemon_down       the bridge could not reach the device at all
                    (BLE disconnected / mode off) and answered "ask"

Auditing must never break gating: write failures are swallowed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

AUDIT_FILENAME = "audit.jsonl"

SOURCE_DEVICE = "device"
SOURCE_TELEGRAM = "telegram"
SOURCE_AUTO_ALLOW = "auto_allow"
SOURCE_TIMEOUT_FALLBACK = "timeout_fallback"
SOURCE_DAEMON_DOWN = "daemon_down"
SOURCES = (
    SOURCE_DEVICE,
    SOURCE_TELEGRAM,
    SOURCE_AUTO_ALLOW,
    SOURCE_TIMEOUT_FALLBACK,
    SOURCE_DAEMON_DOWN,
)


def audit_path(home: Path) -> Path:
    return home / AUDIT_FILENAME


def append_audit(
    home: Path,
    *,
    host: str,
    agent: str,
    sid: str,
    tool: str,
    hint: str,
    decision: str,
    source: str,
    by: str = "",
    ts: float | None = None,
) -> bool:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
        "host": host,
        "agent": agent,
        "sid": sid,
        "tool": tool,
        "hint": hint,
        "decision": decision,
        "source": source,
    }
    if by:
        record["by"] = by
    try:
        home.mkdir(parents=True, exist_ok=True)
        with audit_path(home).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
        return True
    except OSError:
        return False


def read_audit(home: Path, limit: int = 100) -> list[dict]:
    """Most recent ``limit`` records (oldest first within the slice)."""
    try:
        lines = audit_path(home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in lines[-limit:]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
