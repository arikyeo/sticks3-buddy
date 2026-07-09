"""Wire protocol v1 for the StickS3 buddy (newline-delimited JSON over NUS).

Frames (v1, distinguished by their keys):
  snapshot  {"total","running","waiting","msg","entries":[],"tokens","tokens_today","prompt"?}
  time      {"time":[epoch_sec, tz_offset_sec]}
  owner     {"owner": "..."}

The device parses one line at a time from a small buffer, so every outbound
line is budgeted to <= 900 bytes via a degradation cascade:
  drop oldest entries -> shrink prompt hint 96/72/48/32 -> drop prompt -> shrink msg.

All display text is glyph-sanitized to be ASCII-safe for the firmware font:
em/en dashes -> '-', curly quotes -> straight, ellipsis -> '...', NBSP -> ' ',
other non-latin-1 codepoints -> '?'. JSON is emitted with ensure_ascii so the
wire itself is pure ASCII.
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

# Nordic UART Service
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device (write)
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host (notify)

LINE_BUDGET = 900
HINT_STEPS = (96, 72, 48, 32)
MSG_STEPS = (96, 72, 48, 32, 16)
ENTRY_MAX_BYTES = 96  # entries are "HH:MM <text<=80 chars>"; hard cap on bytes

_GLYPH_MAP = {
    0x2010: "-",  # hyphen
    0x2011: "-",  # non-breaking hyphen
    0x2012: "-",  # figure dash
    0x2013: "-",  # en dash
    0x2014: "-",  # em dash
    0x2015: "-",  # horizontal bar
    0x2212: "-",  # minus sign
    0x2018: "'",  # left single curly
    0x2019: "'",  # right single curly
    0x201A: "'",
    0x2032: "'",  # prime
    0x201C: '"',  # left double curly
    0x201D: '"',  # right double curly
    0x201E: '"',
    0x2026: "...",  # ellipsis
    0x00A0: " ",  # NBSP
    0x2022: "*",  # bullet
    0x2192: "->",  # rightwards arrow
    0x2190: "<-",  # leftwards arrow
}


def sanitize(text: object) -> str:
    """Make display text safe for the buddy's latin-1 firmware font."""
    out: list[str] = []
    for ch in str(text):
        cp = ord(ch)
        mapped = _GLYPH_MAP.get(cp)
        if mapped is not None:
            out.append(mapped)
        elif ch in "\r\n\t":
            out.append(" ")
        elif cp < 32 or cp == 127:
            continue  # other control chars: drop
        elif cp > 255:
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate so the UTF-8 encoding is <= max_bytes, never splitting a codepoint."""
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    # Input is valid UTF-8, so only the tail can be an incomplete sequence;
    # errors="ignore" drops exactly that partial codepoint.
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def _bytelen(line: str) -> int:
    return len(line.encode("utf-8"))


def build_snapshot(
    *,
    total: int,
    running: int,
    waiting: int,
    msg: str = "",
    entries: Sequence[str] = (),
    tokens: int = 0,
    tokens_today: int = 0,
    prompt: str | None = None,
    budget: int = LINE_BUDGET,
) -> str:
    """Compose the v1 snapshot line, degrading until it fits the byte budget."""
    msg_s = sanitize(msg)
    entry_list = [truncate_utf8_bytes(sanitize(e), ENTRY_MAX_BYTES) for e in entries]
    prompt_s: str | None = sanitize(prompt) if prompt is not None else None

    def compose() -> str:
        snap: dict = {
            "total": int(total),
            "running": int(running),
            "waiting": int(waiting),
            "msg": msg_s,
            "entries": entry_list,
            "tokens": int(tokens),
            "tokens_today": int(tokens_today),
        }
        if prompt_s is not None:
            snap["prompt"] = prompt_s
        return dumps(snap)

    line = compose()
    # 1. drop oldest entries (front of the list) one at a time
    while _bytelen(line) > budget and entry_list:
        entry_list.pop(0)
        line = compose()
    # 2. shrink the prompt hint stepwise
    if prompt_s is not None:
        for step in HINT_STEPS:
            if _bytelen(line) <= budget:
                break
            prompt_s = truncate_utf8_bytes(prompt_s, step)
            line = compose()
    # 3. drop the prompt entirely
    if prompt_s is not None and _bytelen(line) > budget:
        prompt_s = None
        line = compose()
    # 4. shrink msg
    for step in MSG_STEPS:
        if _bytelen(line) <= budget:
            break
        msg_s = truncate_utf8_bytes(msg_s, step)
        line = compose()
    if _bytelen(line) > budget:
        msg_s = ""
        line = compose()
    return line


def tz_offset_seconds(ts: float | None = None) -> int:
    lt = _time.localtime(ts if ts is not None else _time.time())
    return -(_time.altzone if lt.tm_isdst else _time.timezone)


def build_time_frame(ts: float | None = None) -> str:
    epoch = int(ts if ts is not None else _time.time())
    return dumps({"time": [epoch, tz_offset_seconds(epoch)]})


def build_owner_frame(owner: str) -> str:
    return dumps({"owner": truncate_utf8_bytes(sanitize(owner), 32)})


def build_status_poll() -> str:
    return dumps({"cmd": "status"})


def format_entry(hhmm: str, text: str, max_chars: int = 80) -> str:
    """One activity-feed line: 'HH:MM <text>' with text capped at max_chars."""
    clean = sanitize(text).strip()
    if len(clean) > max_chars:
        clean = clean[: max_chars - 3].rstrip() + "..."
    return f"{hhmm} {clean}".strip()


def iter_lines(lines: Iterable[str]) -> Iterable[bytes]:
    """Encode outbound protocol lines with the trailing newline the device expects."""
    for line in lines:
        yield line.encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Protocol v2 — STUB ONLY (firmware side lands in a later track; everything
# here stays dormant unless a hello-ack negotiates proto >= 2, and the daemon
# does not even send the hello unless its proto2 flag is switched on).
# ---------------------------------------------------------------------------

PROTO_V1 = 1
PROTO_V2 = 2
HELLO_ACK_TIMEOUT_SECS = 2.0  # no ack within this window -> stay in v1 mode
V2_CAPS = ("sessions", "ask", "rxack", "cancel")
V2_TITLE_MAX = 24
V2_DEFAULT_MAX_SESSIONS = 6

_SID_PREFIX = {"claude": "cli", "codex": "cdx"}


def build_hello(cfg: "Config") -> str:
    """Canonical v2 hello: {"cmd":"hello","proto":2,"host":{...},"caps":[...]}."""
    from . import __version__

    return dumps(
        {
            "cmd": "hello",
            "proto": PROTO_V2,
            "host": {
                "id": cfg.host_id,
                "name": truncate_utf8_bytes(sanitize(cfg.host_name), V2_TITLE_MAX),
                "app": "buddy-bridge",
                "ver": __version__,
            },
            "caps": list(V2_CAPS),
        }
    )


@dataclass
class HelloAck:
    proto: int = PROTO_V1
    max_line: int = LINE_BUDGET
    max_sessions: int = V2_DEFAULT_MAX_SESSIONS
    sel: bool = False


def parse_hello_ack(obj: object) -> HelloAck | None:
    """Parse a device hello-ack frame; None when ``obj`` is something else."""
    if not isinstance(obj, dict):
        return None
    if obj.get("cmd") not in ("hello-ack", "helloAck", "hello_ack"):
        return None
    ack = HelloAck()
    proto = obj.get("proto")
    if isinstance(proto, int) and not isinstance(proto, bool) and proto >= 1:
        ack.proto = proto
    max_line = obj.get("maxLine")
    if isinstance(max_line, int) and not isinstance(max_line, bool):
        ack.max_line = max(128, min(4096, max_line))
    max_sessions = obj.get("maxSessions")
    if isinstance(max_sessions, int) and not isinstance(max_sessions, bool):
        ack.max_sessions = max(1, min(16, max_sessions))
    ack.sel = obj.get("sel") is True
    return ack


def build_sessions_frame(
    sessions: Sequence,
    *,
    max_sessions: int = V2_DEFAULT_MAX_SESSIONS,
    budget: int = LINE_BUDGET,
) -> str:
    """v2 sessions frame from Session-like objects (agent/title/state/tokens/
    last_seen/wait_seq attributes — the registry's Session works as-is).

    Waiting sessions come first (FIFO by wait_seq) so a max_sessions cut can
    never drop a session that needs the user. Short wire sids are minted per
    agent in emitted order: cli:1, cli:2 / cdx:1, ... (stable only within a
    frame — the phase-6 device UI work will pin them properly).
    """
    ordered = sorted(
        sessions,
        key=lambda s: (
            0 if getattr(s, "state", "") == "wait" else 1,
            getattr(s, "wait_seq", 0) or float("inf"),
            -float(getattr(s, "last_seen", 0.0)),
        ),
    )
    counters: dict[str, int] = {}
    entries: list[dict] = []
    for session in ordered[: max(0, max_sessions)]:
        agent = str(getattr(session, "agent", "") or "")
        prefix = _SID_PREFIX.get(agent, agent[:3] or "unk")
        counters[prefix] = counters.get(prefix, 0) + 1
        entries.append(
            {
                "sid": f"{prefix}:{counters[prefix]}",
                "agent": agent,
                "title": truncate_utf8_bytes(
                    sanitize(getattr(session, "title", "") or ""), V2_TITLE_MAX
                ),
                "state": str(getattr(session, "state", "") or "idle"),
                "tok": int(getattr(session, "tokens", 0) or 0),
                "last": int(getattr(session, "last_seen", 0.0) or 0),
            }
        )
    line = dumps({"cmd": "sessions", "sessions": entries})
    while _bytelen(line) > budget and entries:
        entries.pop()  # trim from the tail: waiting sessions at the front survive
        line = dumps({"cmd": "sessions", "sessions": entries})
    return line
