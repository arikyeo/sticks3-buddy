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
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

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


def _clean_prompt(prompt: dict | None) -> dict | None:
    """Normalize a prompt object for the wire: {"id","tool","hint"[,"sid","qn"]}.

    REFERENCE.md defines the heartbeat ``prompt`` as an object whose ``id`` the
    device echoes back with its decision; PROTOCOL_V2.md adds optional ``sid``
    and ``qn``. Unknown keys are dropped, text is sanitized.
    """
    if not isinstance(prompt, dict):
        return None
    out: dict = {
        "id": truncate_utf8_bytes(sanitize(prompt.get("id") or ""), WIRE_ID_MAX_BYTES),
        "tool": truncate_utf8_bytes(sanitize(prompt.get("tool") or ""), 32),
        "hint": sanitize(prompt.get("hint") or ""),
    }
    sid = prompt.get("sid")
    if isinstance(sid, str) and sid:
        out["sid"] = truncate_utf8_bytes(sanitize(sid), 16)
    qn = prompt.get("qn")
    if isinstance(qn, int) and not isinstance(qn, bool) and qn >= 0:
        out["qn"] = qn
    return out


def build_snapshot(
    *,
    total: int,
    running: int,
    waiting: int,
    msg: str = "",
    entries: Sequence[str] = (),
    tokens: int = 0,
    tokens_today: int = 0,
    prompt: dict | None = None,
    sessions: Sequence[dict] | None = None,
    budget: int = LINE_BUDGET,
) -> str:
    """Compose the heartbeat snapshot line, degrading until it fits the budget.

    v1 shape per REFERENCE.md; ``sessions`` (already-built entry dicts from
    :func:`build_session_entries`, waiting-first) rides alongside the v1
    aggregates per PROTOCOL_V2.md when provided. Degrade cascade:
    drop oldest entries -> trim sessions tail -> shrink prompt hint
    96/72/48/32 -> drop prompt -> shrink msg.
    """
    msg_s = sanitize(msg)
    entry_list = [truncate_utf8_bytes(sanitize(e), ENTRY_MAX_BYTES) for e in entries]
    prompt_obj = _clean_prompt(prompt)
    session_list = list(sessions) if sessions is not None else None

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
        if session_list is not None:
            snap["sessions"] = session_list
        if prompt_obj is not None:
            snap["prompt"] = prompt_obj
        return dumps(snap)

    line = compose()
    # 1. drop oldest entries (front of the list) one at a time
    while _bytelen(line) > budget and entry_list:
        entry_list.pop(0)
        line = compose()
    # 2. trim sessions from the tail (waiting-first ordering keeps waiters)
    while _bytelen(line) > budget and session_list:
        session_list.pop()
        line = compose()
    # 3. shrink the prompt hint stepwise
    if prompt_obj is not None:
        for step in HINT_STEPS:
            if _bytelen(line) <= budget:
                break
            prompt_obj["hint"] = truncate_utf8_bytes(prompt_obj["hint"], step)
            line = compose()
    # 4. drop the prompt entirely
    if prompt_obj is not None and _bytelen(line) > budget:
        prompt_obj = None
        line = compose()
    # 5. shrink msg
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
# Protocol v2 (PROTOCOL_V2.md). Live: the daemon always opens with hello; a
# device that acks with proto >= 2 switches the link to v2 framing, anything
# else (v1 ack, no ack within 2s) leaves the link in plain v1 mode.
# ---------------------------------------------------------------------------

PROTO_V1 = 1
PROTO_V2 = 2
HELLO_ACK_TIMEOUT_SECS = 2.0  # no ack within this window -> stay in v1 mode
V2_CAPS = ("sessions", "ask", "rxack", "cancel")  # host caps in the hello
EXTRA_CAPS = ("ntfy", "play")  # extras: relevant iff the DEVICE advertises them
V2_LINE_HEADROOM = 64  # spec maxLine 4608 -> host budget 4544
RXACK_DEAD_SECS = 20.0  # sends but no rx-ack/traffic for this long -> link dead
V2_TITLE_MAX = 24
V2_LAST_MAX = 64  # spec caps neither; keep per-session "last" lines small
V2_DEFAULT_MAX_SESSIONS = 6
WIRE_ID_MAX_BYTES = 39
PROMPT_QN_CAP = 3


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
    """Parsed {"ack":"hello","ok":true,"proto":2,"data":{...}} device reply.

    ``sel`` defaults to True: PROTOCOL_V2.md defines sel:false as the special
    "pinned to another host" case, so an ack that omits it means proceed.
    """

    proto: int = PROTO_V1
    max_line: int = LINE_BUDGET
    max_sessions: int = V2_DEFAULT_MAX_SESSIONS
    sel: bool = True
    caps: tuple[str, ...] = ()
    fw: str = ""
    board: str = ""
    name: str = ""


def parse_hello_ack(obj: object) -> HelloAck | None:
    """Parse a device hello-ack frame; None when ``obj`` is something else.

    Spec shape: {"ack":"hello","ok":true,"proto":2,"data":{"fw","board",
    "name","caps","maxSessions","maxLine","sel"}}. An explicit ok:false is
    treated as a declined handshake (proto forced to 1, no caps).
    """
    if not isinstance(obj, dict) or obj.get("ack") != "hello":
        return None
    ack = HelloAck()
    if obj.get("ok") is False:
        return ack  # device declined: stay v1
    proto = obj.get("proto")
    if isinstance(proto, int) and not isinstance(proto, bool) and proto >= 1:
        ack.proto = proto
    data = obj.get("data")
    if not isinstance(data, dict):
        data = {}
    max_line = data.get("maxLine")
    if isinstance(max_line, int) and not isinstance(max_line, bool):
        ack.max_line = max(256, min(16384, max_line))
    max_sessions = data.get("maxSessions")
    if isinstance(max_sessions, int) and not isinstance(max_sessions, bool):
        ack.max_sessions = max(1, min(32, max_sessions))
    if data.get("sel") is False:
        ack.sel = False
    caps = data.get("caps")
    if isinstance(caps, (list, tuple)):
        ack.caps = tuple(str(c) for c in caps if isinstance(c, str) and c)
    for attr in ("fw", "board", "name"):
        val = data.get(attr)
        if isinstance(val, str):
            setattr(ack, attr, sanitize(val))
    return ack


def effective_proto(theirs: int, ours: int = PROTO_V2) -> int:
    """min(host.proto, device.proto), floored at v1."""
    return max(PROTO_V1, min(int(ours), int(theirs)))


def v2_line_budget(max_line: int) -> int:
    """Outbound byte budget for a v2 link: device maxLine minus headroom."""
    return max(128, int(max_line) - V2_LINE_HEADROOM)


def build_session_entries(
    sessions: Sequence,
    *,
    wire_sid: "Callable[[str, str], str]",
    max_sessions: int = V2_DEFAULT_MAX_SESSIONS,
) -> list[dict]:
    """Wire dicts for the heartbeat ``sessions`` array (PROTOCOL_V2.md).

    ``sessions`` must already be ordered waiting-first (the registry's
    ``sessions_sorted()``); this only caps and formats. ``wire_sid`` pins the
    short sid (cli:N / cdx:N) per (agent, session) — registry-owned so a sid
    is never reused for a different session while the daemon lives.
    """
    entries: list[dict] = []
    for session in list(sessions)[: max(0, int(max_sessions))]:
        agent = str(getattr(session, "agent", "") or "")
        entries.append(
            {
                "sid": wire_sid(agent, str(getattr(session, "sid", "") or "")),
                "agent": agent,
                "title": truncate_utf8_bytes(
                    sanitize(getattr(session, "title", "") or ""), V2_TITLE_MAX
                ),
                "state": str(getattr(session, "state", "") or "idle"),
                "tok": int(getattr(session, "tokens", 0) or 0),
                "last": truncate_utf8_bytes(
                    sanitize(getattr(session, "last_line", "") or ""), V2_LAST_MAX
                ),
            }
        )
    return entries


def build_prompt_cancel(wire_id: str) -> str:
    """{"cmd":"prompt_cancel","id":...} — requires the negotiated cancel cap."""
    return dumps({"cmd": "prompt_cancel", "id": wire_id})


WIFI_MAX_ENTRIES = 256  # device-side LittleFS-backed cap; ack {"error":"full"} beyond it


def build_wifi_frame(ssid: str, password: str) -> str:
    """{"cmd":"wifi","ssid":...,"pass":...} — WiFi upsert-by-ssid, proto>=2 only.

    Unlike every other builder here, ``ssid``/``password`` are NOT run
    through :func:`sanitize`: that function is for *display* text
    (glyph-maps dashes/quotes, drops control chars, folds non-latin1 to
    '?') and would corrupt credential bytes that must round-trip exactly
    for the AP handshake to succeed. ``dumps()`` still guarantees a
    pure-ASCII wire via JSON's ``ensure_ascii`` \\uXXXX escaping, so line
    framing is never at risk. Ack: ``{"ack":"wifi","ok":true,"n":<count>}``,
    or ``{"ok":false,"error":"full","n":<count>}`` once the device is at
    :data:`WIFI_MAX_ENTRIES`.
    """
    return dumps({"cmd": "wifi", "ssid": ssid, "pass": password})


def build_wifi_remove_frame(ssid: str) -> str:
    """{"cmd":"wifi","ssid":...,"remove":true} — drop one stored network.

    ``ssid`` is not sanitized, matching :func:`build_wifi_frame`: it must
    match the stored credential byte-for-byte, not a display rendering of
    it. Ack: ``{"ack":"wifi","ok":true,"n":<count>}``.
    """
    return dumps({"cmd": "wifi", "ssid": ssid, "remove": True})


def build_wifi_clear_frame() -> str:
    """{"cmd":"wifi","clear":true} — drop every stored network.

    Ack: ``{"ack":"wifi","ok":true,"n":0}``.
    """
    return dumps({"cmd": "wifi", "clear": True})


def build_wifi_list_frame() -> str:
    """{"cmd":"wifi","list":true} — enumerate stored SSIDs (names only).

    Ack: ``{"ack":"wifi","ok":true,"ssids":[...],"n":<count>}``.
    """
    return dumps({"cmd": "wifi", "list": True})


def build_ask_evt(
    ask_id: str,
    questions: Sequence[dict],
    *,
    sid: str | None = None,
    multi_select: bool = False,
    budget: int = LINE_BUDGET,
) -> str:
    """Read-only structured question event (PROTOCOL_V2.md `ask` capability).

    ``questions``: [{"header","text","options":[{"label","desc"}]}]. Text is
    sanitized and field-capped; if the line still exceeds the budget, option
    descs are dropped, then options past the first two, then whole questions.
    """
    cleaned: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = []
        raw_options = q.get("options")
        if isinstance(raw_options, (list, tuple)):
            for opt in raw_options:
                if not isinstance(opt, dict):
                    continue
                options.append(
                    {
                        "label": truncate_utf8_bytes(sanitize(opt.get("label") or ""), 32),
                        "desc": truncate_utf8_bytes(sanitize(opt.get("desc") or ""), 64),
                    }
                )
        cleaned.append(
            {
                "header": truncate_utf8_bytes(sanitize(q.get("header") or ""), 32),
                "text": truncate_utf8_bytes(sanitize(q.get("text") or ""), 96),
                "options": options,
            }
        )

    def compose() -> str:
        evt: dict = {
            "evt": "ask",
            "id": truncate_utf8_bytes(sanitize(ask_id), WIRE_ID_MAX_BYTES),
        }
        if sid:
            evt["sid"] = truncate_utf8_bytes(sanitize(sid), 16)
        evt["multiSelect"] = bool(multi_select)
        evt["questions"] = cleaned
        return dumps(evt)

    line = compose()
    if _bytelen(line) > budget:  # drop option descs
        for q in cleaned:
            for opt in q["options"]:
                opt.pop("desc", None)
        line = compose()
    while _bytelen(line) > budget and any(len(q["options"]) > 2 for q in cleaned):
        for q in cleaned:  # trim options from the tail, two survive
            if len(q["options"]) > 2:
                q["options"].pop()
        line = compose()
    while _bytelen(line) > budget and len(cleaned) > 1:
        cleaned.pop()
        line = compose()
    return line


def build_ask_cancel(ask_id: str) -> str:
    """{"evt":"ask_cancel","id":...} — clears a displayed ask."""
    return dumps({"evt": "ask_cancel", "id": truncate_utf8_bytes(sanitize(ask_id), WIRE_ID_MAX_BYTES)})
