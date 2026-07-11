"""Federation merge layer (relay v2): every machine's sessions on one stick.

Non-holder bridges stream their full session state to the current holder as
``state_sync`` relay messages (see relay.py); this module is the holder-side
model of that remote state. It

  * keeps one entry per peer with a 30s TTL (a silent peer's sessions vanish
    and its prompt/ask displays are cleaned up by the daemon);
  * namespaces remote identifiers so they can never collide with local ones:
      session sid   ``r<k>:<orig>``  (<= 11 bytes: firmware Session.sid is
                    char[12], strncpy'd — 11 chars + NUL round-trips verbatim)
      prompt/ask id ``r<k>.<orig>``  (<= 39 ASCII, the wire id budget)
    where ``k`` is a stable per-peer index. When the passthrough form would
    bust the cap (long tool_use_ids, weird sids) a short id is minted
    (``r<k>:s<n>`` / ``r<k>.p<n>`` / ``r<k>.a<n>``) and pinned in a map;
  * prefixes remote titles with ``[NAME] `` (short host name <= 6 chars);
  * re-sanitizes every peer-supplied string — HMAC only proves the sender
    holds the token, not that its strings are display-safe;
  * merges remote sessions with local ones waiting-first across ALL hosts
    (wait > run > idle); on maxSessions overflow idle remotes drop first;
  * picks the merged top-level prompt as the oldest-waiting across all
    machines, ordered by first-seen on the holder's monotonic clock (local
    pendings carry their real created time on the same clock);
  * feeds the DecisionRouter's remote-route table (wire id -> (peer, orig
    id)) so a stick answer for ``r<k>.x`` can be relayed to its origin.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .protocol import (
    PROMPT_QN_CAP,
    V2_LAST_MAX,
    V2_TITLE_MAX,
    sanitize,
    truncate_utf8_bytes,
)
from .registry import IDLE, RUN, STATES, WAIT

STATE_TTL_SECS = 30.0  # no state_sync for this long -> peer's state vanishes
SID_WIRE_MAX_BYTES = 11  # firmware Session.sid is char[12] => 11 chars + NUL
PROMPT_ID_MAX = 39  # same budget as decision.WIRE_ID_MAX
NAME_MAX_CHARS = 6
MAX_SESSIONS_PER_PEER = 8
MAX_ASK_QUESTIONS = 3
MAX_ASK_OPTIONS = 4
_SID_MAP_MAX = 64
_PID_MAP_MAX = 16
_STATE_RANK = {WAIT: 0, RUN: 1, IDLE: 2}


def _ascii_safe(candidate: str) -> bool:
    return bool(candidate) and all(33 <= ord(c) < 127 for c in candidate)


def _clean_str(value: object, max_bytes: int) -> str:
    return truncate_utf8_bytes(sanitize(value if value is not None else ""), max_bytes)


def _cap_map(mapping: dict, cap: int) -> None:
    """FIFO-evict the oldest pins once a namespace map outgrows its cap."""
    while len(mapping) > cap:
        del mapping[next(iter(mapping))]


@dataclass
class PeerFed:
    """One peer's last state_sync, cleaned and namespaced."""

    host_id: str
    k: int
    name: str = "peer"
    last_sync: float = 0.0
    sessions: list = field(default_factory=list)  # cleaned wire entry dicts
    prompt: Optional[dict] = None  # {"id","tool","hint","sid","qn"} namespaced
    prompt_orig_id: str = ""
    prompt_first_seen: float = 0.0
    ask: Optional[dict] = None  # {"id","sid","questions","multiSelect"} namespaced
    ask_orig_id: str = ""
    _sid_map: dict = field(default_factory=dict)
    _sid_counter: int = 0
    _pid_map: dict = field(default_factory=dict)
    _pid_counter: int = 0

    # ---- namespacing ----

    def wire_sid(self, orig: str) -> str:
        pinned = self._sid_map.get(orig)
        if pinned is not None:
            return pinned
        cand = f"r{self.k}:{orig}"
        if len(cand.encode("utf-8")) > SID_WIRE_MAX_BYTES or not _ascii_safe(cand):
            self._sid_counter += 1
            cand = f"r{self.k}:s{self._sid_counter}"
        self._sid_map[orig] = cand
        _cap_map(self._sid_map, _SID_MAP_MAX)
        return cand

    def wire_prompt_id(self, orig: str, letter: str = "p") -> str:
        pinned = self._pid_map.get(orig)
        if pinned is not None:
            return pinned
        cand = f"r{self.k}.{orig}"
        if len(cand) > PROMPT_ID_MAX or not _ascii_safe(cand):
            self._pid_counter += 1
            cand = f"r{self.k}.{letter}{self._pid_counter}"
        self._pid_map[orig] = cand
        _cap_map(self._pid_map, _PID_MAP_MAX)
        return cand


class Federation:
    """Holder-side merge model over every peer's streamed state.

    ``router`` is the daemon's DecisionRouter: this class owns WHEN remote
    routes appear/vanish (state_sync folds, prompt clears, TTL prune), the
    router owns the wire-id -> (peer, orig id) table itself.
    """

    def __init__(self, router, now_fn: Callable[[], float] = time.monotonic) -> None:
        self._router = router
        self._now = now_fn
        self._peers: dict[str, PeerFed] = {}
        self._k_by_host: dict[str, int] = {}  # stable across peer expiry

    # ---- folding state in ----

    def _peer(self, host_id: str) -> PeerFed:
        peer = self._peers.get(host_id)
        if peer is None:
            k = self._k_by_host.get(host_id)
            if k is None:
                k = len(self._k_by_host)
                self._k_by_host[host_id] = k
            peer = PeerFed(host_id=host_id, k=k)
            self._peers[host_id] = peer
        return peer

    def update(self, host_id: str, payload: dict) -> bool:
        """Fold one verified state_sync payload in. Returns True when the
        merged view changed (sessions/prompt/ask differ from last sync)."""
        if not host_id:
            return False
        peer = self._peer(host_id)
        peer.last_sync = self._now()
        name = _clean_str(payload.get("name") or host_id, NAME_MAX_CHARS) or "peer"
        sessions = self._clean_sessions(peer, payload.get("sessions"), name)
        changed = name != peer.name or sessions != peer.sessions
        peer.name = name
        peer.sessions = sessions
        changed |= self._fold_prompt(peer, payload.get("prompt"))
        changed |= self._fold_ask(peer, payload.get("ask"), name)
        return changed

    def _clean_sessions(self, peer: PeerFed, raw: object, name: str) -> list[dict]:
        out: list[dict] = []
        if not isinstance(raw, (list, tuple)):
            return out
        for item in list(raw)[:MAX_SESSIONS_PER_PEER]:
            if not isinstance(item, dict):
                continue
            orig_sid = str(item.get("sid") or "")
            if not orig_sid:
                continue
            state = item.get("state")
            if state not in STATES:
                state = IDLE
            tok = item.get("tok")
            if not isinstance(tok, int) or isinstance(tok, bool) or tok < 0:
                tok = 0
            title = _clean_str(f"[{name}] {item.get('title') or ''}".rstrip(), V2_TITLE_MAX)
            out.append(
                {
                    "sid": peer.wire_sid(orig_sid),
                    "agent": _clean_str(item.get("agent"), 7),
                    "title": title,
                    "state": state,
                    "tok": tok,
                    "last": _clean_str(item.get("last"), V2_LAST_MAX),
                }
            )
        return out

    def _fold_prompt(self, peer: PeerFed, raw: object) -> bool:
        orig_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if not orig_id:
            return self._clear_peer_prompt(peer)
        wire_id = peer.wire_prompt_id(orig_id, letter="p")
        if peer.prompt_orig_id and peer.prompt_orig_id != orig_id:
            self._clear_peer_prompt(peer)  # old prompt replaced: drop its route
        if peer.prompt_orig_id != orig_id:
            peer.prompt_first_seen = self._now()
            peer.prompt_orig_id = orig_id
            self._router.register_remote(wire_id, peer.host_id, orig_id)
        qn = raw.get("qn")
        if not isinstance(qn, int) or isinstance(qn, bool) or qn < 0:
            qn = 0
        orig_sid = str(raw.get("sid") or "")
        prompt = {
            "id": wire_id,
            "tool": _clean_str(raw.get("tool"), 32),
            "hint": _clean_str(raw.get("hint"), 80),
            "sid": peer.wire_sid(orig_sid) if orig_sid else "",
            "qn": min(PROMPT_QN_CAP, qn),
        }
        changed = prompt != peer.prompt
        peer.prompt = prompt
        return changed

    def _clear_peer_prompt(self, peer: PeerFed) -> bool:
        if not peer.prompt_orig_id and peer.prompt is None:
            return False
        wire_id = peer.prompt["id"] if peer.prompt else ""
        if wire_id:
            self._router.unregister_remote(wire_id)
        peer.prompt = None
        peer.prompt_orig_id = ""
        peer.prompt_first_seen = 0.0
        return True

    def _fold_ask(self, peer: PeerFed, raw: object, name: str) -> bool:
        orig_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if not orig_id or not isinstance(raw, dict):
            changed = peer.ask is not None
            peer.ask = None
            peer.ask_orig_id = ""
            return changed
        questions = self._clean_questions(raw.get("questions"))
        if not questions:
            changed = peer.ask is not None
            peer.ask = None
            peer.ask_orig_id = ""
            return changed
        orig_sid = str(raw.get("sid") or "")
        ask = {
            "id": peer.wire_prompt_id(orig_id, letter="a"),
            "sid": peer.wire_sid(orig_sid) if orig_sid else "",
            "questions": questions,
            "multiSelect": raw.get("multiSelect") is True,
            "name": name,
        }
        changed = ask != peer.ask
        peer.ask = ask
        peer.ask_orig_id = orig_id
        return changed

    @staticmethod
    def _clean_questions(raw: object) -> list[dict]:
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[dict] = []
        for q in list(raw)[:MAX_ASK_QUESTIONS]:
            if not isinstance(q, dict):
                continue
            options = []
            raw_options = q.get("options")
            if isinstance(raw_options, (list, tuple)):
                for opt in list(raw_options)[:MAX_ASK_OPTIONS]:
                    if isinstance(opt, dict):
                        options.append(
                            {
                                "label": _clean_str(opt.get("label"), 32),
                                "desc": _clean_str(opt.get("desc"), 64),
                            }
                        )
            out.append(
                {
                    "header": _clean_str(q.get("header"), 32),
                    "text": _clean_str(q.get("text"), 96),
                    "options": options,
                }
            )
        return out

    # ---- lifecycle ----

    def prune(self) -> list[PeerFed]:
        """Drop peers silent for > STATE_TTL_SECS; their remote routes are
        unregistered here. Returns the removed peers (the daemon cleans up
        any on-device prompt/ask display they still own)."""
        cutoff = self._now() - STATE_TTL_SECS
        removed = []
        for host_id in [h for h, p in self._peers.items() if p.last_sync < cutoff]:
            peer = self._peers.pop(host_id)
            self._clear_peer_prompt(peer)
            removed.append(peer)
        return removed

    def drop_prompt(self, host_id: str) -> Optional[str]:
        """Clear one peer's live prompt (stick answered / origin resolved).
        Returns the cleared wire id, if there was one."""
        peer = self._peers.get(host_id)
        if peer is None or peer.prompt is None:
            return None
        wire_id = peer.prompt["id"]
        self._clear_peer_prompt(peer)
        return wire_id

    def drop_prompt_by_orig(self, host_id: str, orig_id: str) -> Optional[str]:
        """Clear a peer's prompt only when it matches the origin's wire id
        (the ``pid`` riding a prompt_resolved event)."""
        peer = self._peers.get(host_id)
        if peer is None or not orig_id or peer.prompt_orig_id != orig_id:
            return None
        return self.drop_prompt(host_id)

    # ---- merged queries ----

    def peer_count(self) -> int:
        return len(self._peers)

    def has_peer(self, host_id: str) -> bool:
        """True while this peer streams state_sync (its v1 card events are
        then redundant and suppressed by the daemon)."""
        return host_id in self._peers

    def counts(self) -> tuple[int, int, int]:
        """(total, running, waiting) across all remote sessions."""
        total = running = waiting = 0
        for peer in self._peers.values():
            for entry in peer.sessions:
                total += 1
                if entry["state"] == RUN:
                    running += 1
                elif entry["state"] == WAIT:
                    waiting += 1
        return total, running, waiting

    def merged_entries(self, local: list[dict], max_sessions: int) -> list[dict]:
        """Merge local + remote wire entries, waiting-first across all hosts
        (wait > run > idle); locals sort before remotes within a state, so
        the tail clamp drops idle remotes first."""
        keyed: list[tuple[int, int, int, int, dict]] = []
        for i, entry in enumerate(local):
            keyed.append((_STATE_RANK.get(entry.get("state"), 2), 0, 0, i, entry))
        for peer in sorted(self._peers.values(), key=lambda p: p.k):
            for i, entry in enumerate(peer.sessions):
                keyed.append((_STATE_RANK.get(entry.get("state"), 2), 1, peer.k, i, entry))
        keyed.sort(key=lambda item: item[:4])
        return [entry for *_, entry in keyed[: max(0, int(max_sessions))]]

    def oldest_remote_prompt(self) -> Optional[tuple[float, dict]]:
        """(first_seen, namespaced prompt dict) of the longest-waiting remote
        prompt, on the holder's monotonic clock."""
        best: Optional[tuple[float, dict]] = None
        for peer in self._peers.values():
            if peer.prompt is None:
                continue
            if best is None or peer.prompt_first_seen < best[0]:
                best = (peer.prompt_first_seen, dict(peer.prompt))
        return best

    def pending_total(self) -> int:
        """Total pending decisions across peers: each live prompt counts 1
        plus the queue depth (qn) its origin reported behind it."""
        return sum(
            1 + int(peer.prompt.get("qn") or 0)
            for peer in self._peers.values()
            if peer.prompt is not None
        )

    def remote_prompts(self) -> list[dict]:
        """Every peer's live prompt, oldest-first: the namespaced prompt dict
        plus the peer's display ``name`` and holder-clock ``first_seen``
        (the holder-side merged /pending view, e.g. for the Telegram bot)."""
        out = [
            {**peer.prompt, "name": peer.name, "first_seen": peer.prompt_first_seen}
            for peer in self._peers.values()
            if peer.prompt is not None
        ]
        out.sort(key=lambda p: p["first_seen"])
        return out

    def prompt_ids(self) -> set[str]:
        return {p.prompt["id"] for p in self._peers.values() if p.prompt is not None}

    def remote_asks(self) -> dict[str, dict]:
        """wire ask id -> namespaced ask dict for every peer's live ask."""
        return {p.ask["id"]: dict(p.ask) for p in self._peers.values() if p.ask is not None}

    def route(self, wire_id: str) -> Optional[tuple[str, str]]:
        """(peer host_id, origin wire id) for a namespaced prompt id."""
        return self._router.remote_route(wire_id)
