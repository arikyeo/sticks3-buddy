"""buddy-bridge daemon: asyncio supervisor tying IPC, BLE and the protocol together.

Responsibilities:
  * loopback IPC server on an ephemeral port + endpoint.json handshake
  * persistent BLE link per [ble] mode
  * heartbeat: 10s keepalive, immediate on-change sends deduped by content
  * timesync frame on connect and daily thereafter
  * device status poll ({"cmd":"status"}) every 15s
  * protocol v2 (PROTOCOL_V2.md): hello handshake on connect, negotiated
    line budget (maxLine - 64 headroom), per-session heartbeat detail,
    prompt sid/qn routing, prompt_cancel, rxack dead-link watch
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import secrets
import time
from typing import Any, Optional

from . import protocol
from .adapters import claude_cli, codex
from .adapters.claude_jsonl import AssistantEvent, JsonlTailer, claude_projects_root
from .adapters.codex_sessions import (
    CodexSessionInfo,
    CodexSessionScanner,
    codex_sessions_root,
)
from .audit import (
    SOURCE_AUTO_ALLOW,
    SOURCE_DAEMON_DOWN,
    SOURCE_DEVICE,
    SOURCE_TIMEOUT_FALLBACK,
    append_audit,
)
from .cards import (
    CARDS_LOG_FILENAME,
    Card,
    CardDeduper,
    CardLog,
    CardPipeline,
    build_ntfy_line,
    clean_card,
)
from .config import Config
from .ble.link import SLOW_RETRY_SECS, LinkManager
from .federation import Federation
from .relay import (
    EV_ATTENTION,
    EV_DECISION,
    EV_PROMPT_PENDING,
    EV_PROMPT_RESOLVED,
    EV_STATE_SYNC,
    RelayManager,
)
from .decision import (
    ALLOW,
    ASK,
    DECISIONS,
    DENY,
    DecisionRouter,
    PendingDecision,
    _wire_safe,
    build_hint,
    extract_detail,
)
from .ipc.endpoint import remove_endpoint, write_endpoint
from .ipc.server import IpcServer
from .matchers import (
    ALWAYS_ASK,
    AUTO_ALLOW,
    MATCHERS_FILENAME,
    STRICT,
    is_safe_tool,
    load_matchers,
)
from .registry import SessionRegistry
from .usage import LEDGER_FILENAME, UsageLedger

log = logging.getLogger(__name__)

HEARTBEAT_SECS = 10.0
STATUS_POLL_SECS = 15.0
TIMESYNC_SECS = 24 * 3600.0
TICK_SECS = 1.0
# ondemand gating: a connect that can't complete fast fails open ("ask"),
# and the device gets the gate deadline minus a safety margin to decide.
ONDEMAND_CONNECT_SECS = 5.0
ONDEMAND_DEADLINE_MARGIN_SECS = 10.0
WIFI_ACK_TIMEOUT_SECS = 10.0  # max wait for the device's {"ack":"wifi",...}
RELAY_SYNC_SECS = 2.0  # cadence of mirroring local waits to the holder
RELAY_CARD_RATE_SECS = 10.0  # max 1 remote card per peer per this window
REMOTE_PENDING_TTL_SECS = 300.0  # forget a peer's pending we never saw resolved
FED_STATE_MAX_SESSIONS = 8  # sessions per state_sync (waiting-first, so waits survive)
# Ask origination (AskUserQuestion display): per-field caps applied before
# the protocol layer's own byte-safe truncation + line-budget cascade.
ASK_MAX_QUESTIONS = 3
ASK_MAX_OPTIONS = 4
ASK_HEADER_MAX_CHARS = 24
ASK_TEXT_MAX_CHARS = 200
ASK_LABEL_MAX_CHARS = 44
ASK_DESC_MAX_CHARS = 64


def convert_ask_questions(raw: object) -> list[dict]:
    """Claude's AskUserQuestion tool_input questions -> protocol ask shape.

    Input items look like {"question","header","options":[{"label",
    "description"}],"multiSelect"}; the wire wants {"header","text",
    "options":[{"label","desc"}]}. Tolerates any malformed subset."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for q in list(raw)[:ASK_MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        options: list[dict] = []
        raw_options = q.get("options")
        if isinstance(raw_options, (list, tuple)):
            for opt in list(raw_options)[:ASK_MAX_OPTIONS]:
                if isinstance(opt, dict):
                    options.append(
                        {
                            "label": str(opt.get("label") or "")[:ASK_LABEL_MAX_CHARS],
                            "desc": str(opt.get("description") or "")[:ASK_DESC_MAX_CHARS],
                        }
                    )
                elif isinstance(opt, str) and opt:
                    options.append({"label": opt[:ASK_LABEL_MAX_CHARS], "desc": ""})
        question = {
            "header": str(q.get("header") or "")[:ASK_HEADER_MAX_CHARS],
            "text": str(q.get("question") or q.get("text") or "")[:ASK_TEXT_MAX_CHARS],
            "options": options,
        }
        if question["text"] or question["header"] or options:
            out.append(question)
    return out

_BLE_MODES = ("exclusive", "ondemand", "off")


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token = secrets.token_hex(16)
        self.ipc = IpcServer(self.token, self._on_ipc)
        self.link = LinkManager(
            self._on_ble_line,
            name_prefix=cfg.ble_name_prefix,
            address=cfg.ble_address,
            mode=cfg.ble_mode,
            on_connect=self._on_ble_connect,
        )
        self.registry = SessionRegistry()
        self.router = DecisionRouter()
        self.matchers = load_matchers(cfg.home / MATCHERS_FILENAME)
        self.ledger = UsageLedger(cfg.home / LEDGER_FILENAME)
        self.claude_tailer = JsonlTailer(
            root=claude_projects_root(),
            offsets_path=cfg.home / "offsets.json",
            on_event=self._on_claude_transcript,
        )
        self.codex_scanner = CodexSessionScanner(
            root=codex_sessions_root(),
            on_session=self._on_codex_session,
        )
        self.cards = CardPipeline(
            deduper=CardDeduper(), log=CardLog(cfg.home / CARDS_LOG_FILENAME)
        )
        self.started_at = time.time()
        self.last_device_status: dict[str, Any] = {}
        # Protocol v2: the hello opens every connection; v2 only activates
        # after the device acks proto >= 2. No ack within
        # HELLO_ACK_TIMEOUT_SECS leaves the link in v1 mode (900-byte lines,
        # aggregates only, no sessions/rxack/cancel).
        self.negotiated_proto = protocol.PROTO_V1
        self.hello_ack: Optional[protocol.HelloAck] = None
        self.device_sel = True  # last hello-ack data.sel
        self._hello_deadline = 0.0
        # rxack dead-link watch: any inbound frame counts as rx traffic.
        self._rx_last_ts = 0.0
        self._tx_since_rx = False
        self._sent_prompt_ids: set[str] = set()  # prompt ids shown on-device
        # Live asks displayed on the device (ask cap): wire id -> origin.
        self._live_asks: dict[str, dict[str, str]] = {}
        self._ask_counter = itertools.count(1)
        self._last_sent_line = ""
        self._last_send_ts = 0.0
        self._last_timesync_ts = 0.0
        self._last_status_poll_ts = 0.0
        # yield-when-idle ([ble] idle_yield_min, exclusive mode only): after
        # N idle minutes the daemon frees the stick for other hosts and
        # retries slowly; any local activity snaps it back to a normal
        # reconnect burst.
        self.yielding = False
        self._idle_since = 0.0
        # LAN relay federation (None unless [relay] enabled + token set)
        self.relay: Optional[RelayManager] = None
        self._remote_pending: dict[tuple[str, str], tuple[str, str, float]] = {}
        self._remote_cards: dict[tuple[str, str], Card] = {}
        self._remote_card_ts: dict[str, float] = {}  # peer id -> last card ts
        self._last_relay_sync = 0.0
        # Federation v2 (merged remote sessions/prompts/asks on our stick
        # while we hold it, and our full state streamed out while we don't).
        self.federation = Federation(self.router)
        self._fed_prompt_ids: set[str] = set()  # remote prompt wire ids offered
        self._fed_answered: set[str] = set()  # ...that the stick answered
        self._remote_live_asks: set[str] = set()  # remote ask ids on-device
        self._fed_ask: Optional[dict[str, Any]] = None  # our ask riding state_sync
        self._relay_force_sync = False  # holder changed: full resync next tick
        self._fed_tasks: set[asyncio.Task] = set()  # decision relays in flight
        self._ondemand_lock = asyncio.Lock()  # one on-demand gate at a time
        self._wifi_lock = asyncio.Lock()  # one wifi provisioning request at a time
        self._wifi_ack_future: Optional[asyncio.Future] = None
        self._tasks: list[asyncio.Task] = []
        self._ble_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

    # ---- transcript + usage ----

    def _on_claude_transcript(self, event: AssistantEvent) -> None:
        if event.session_id:
            self.registry.add_tokens("claude", event.session_id, event.output_tokens)
            if event.cwd and self.registry.get("claude", event.session_id) is not None:
                self.registry.upsert("claude", event.session_id, event.cwd)
            self.ledger.add("claude", event.session_id, event.output_tokens)
            if event.text:
                self.registry.note_activity("claude", event.session_id, event.text)
        if event.text:
            self.registry.add_entry(protocol.format_entry(event.hhmm, event.text))

    def _on_codex_session(self, info: CodexSessionInfo) -> None:
        added = self.ledger.add_cumulative("codex", info.session_id, info.output_tokens)
        session = self.registry.get("codex", info.session_id)
        if session is not None:
            if added:
                self.registry.add_tokens("codex", info.session_id, added)
            if info.cwd and not session.cwd:
                self.registry.upsert("codex", info.session_id, info.cwd)

    # ---- snapshot source ----

    def tokens_totals(self) -> tuple[int, int]:
        """(lifetime, today) output-token totals from the persistent ledger."""
        return self.ledger.totals()

    def _fed_active(self) -> bool:
        """Remote state is merged into the device view only while the relay
        runs and at least one peer streams state_sync."""
        return self.relay is not None and self.federation.peer_count() > 0

    def merged_counts(self) -> tuple[int, int, int]:
        """(total, running, waiting) across this machine plus every
        federated peer (the device-facing aggregates)."""
        total, running, waiting = self.registry.counts()
        if self._fed_active():
            ft, fr, fw = self.federation.counts()
            total, running, waiting = total + ft, running + fr, waiting + fw
        return total, running, waiting

    def headline(self) -> str:
        total, running, waiting = self.merged_counts()
        if waiting:
            # Attention hints (Notification messages) are not actionable
            # prompts (no wire id), so they surface here instead: per
            # REFERENCE.md the snapshot "prompt" only appears when a
            # permission decision is needed.
            no_remote_pending = not self._fed_active() or self.federation.pending_total() == 0
            if self.router.pending_count() == 0 and no_remote_pending:
                hint = self.registry.pending_prompt()
                if hint:
                    return hint
            return f"{waiting} waiting for you"
        if running:
            return f"{running} running"
        if total:
            return f"{total} idle"
        return "bridge up"

    # ---- protocol v2 negotiated state ----

    @property
    def v2_active(self) -> bool:
        return self.negotiated_proto >= protocol.PROTO_V2

    def cap_active(self, name: str) -> bool:
        """Effective capability: intersection of what we support and what the
        device ack'd. Extras (ntfy/play) are device-advertised only."""
        if not self.v2_active or self.hello_ack is None:
            return False
        if name not in self.hello_ack.caps:
            return False
        return name in protocol.V2_CAPS or name in protocol.EXTRA_CAPS

    @property
    def line_budget(self) -> int:
        if self.v2_active and self.hello_ack is not None:
            return protocol.v2_line_budget(self.hello_ack.max_line)
        return protocol.LINE_BUDGET

    def _oldest_local_pending(self) -> Optional[PendingDecision]:
        """The oldest-waiting session's pending gate decision first, else the
        oldest pending overall."""
        for session in self.registry.waiting_fifo():
            pending = self.router.oldest_for(session.agent, session.sid)
            if pending is not None:
                return pending
        return self.router.oldest()

    def _merged_qn(self, shown_session_count: int) -> int:
        """Queue depth behind the shown prompt. Local-only view keeps the v2
        per-session semantics; with federated peers it becomes the merged
        depth across every machine (spec: cap PROMPT_QN_CAP)."""
        if not self._fed_active():
            return min(protocol.PROMPT_QN_CAP, max(0, shown_session_count - 1))
        merged = self.router.pending_count() + self.federation.pending_total()
        return min(protocol.PROMPT_QN_CAP, max(0, merged - 1))

    def pending_prompt_obj(self) -> Optional[dict]:
        """Wire prompt object for the heartbeat: the oldest-waiting decision
        across this machine AND every federated peer (remote age = first
        seen here, same monotonic clock as local ``created``). v2 adds sid
        (pinned/namespaced wire sid) and qn (queue depth behind the shown
        prompt — merged across machines once peers federate)."""
        pending = self._oldest_local_pending()
        remote = self.federation.oldest_remote_prompt() if self._fed_active() else None
        # <= tiebreak: a remote first-seen is already LATE by the relay hop,
        # so an equal holder-clock stamp means the remote asked first (and
        # Windows' ~16ms monotonic granularity makes ties common).
        if remote is not None and (pending is None or remote[0] <= pending.created):
            first_seen, obj = remote
            prompt: dict[str, Any] = {"id": obj["id"], "tool": obj["tool"], "hint": obj["hint"]}
            if self.v2_active:
                if obj.get("sid"):
                    prompt["sid"] = obj["sid"]
                prompt["qn"] = self._merged_qn(1 + int(obj.get("qn") or 0))
            return prompt
        if pending is None:
            return None
        obj = {
            "id": pending.wire_id,
            "tool": pending.tool,
            "hint": pending.detail or pending.hint,
        }
        if self.v2_active:
            obj["sid"] = self.registry.wire_sid(pending.agent, pending.sid)
            obj["qn"] = self._merged_qn(
                self.router.session_pending_count(pending.agent, pending.sid)
            )
        return obj

    def _compose_state_line(self, prompt_obj: Optional[dict]) -> str:
        total, running, waiting = self.merged_counts()
        tokens, tokens_today = self.tokens_totals()
        sessions = None
        if self.v2_active and self.cap_active("sessions") and self.hello_ack is not None:
            sessions = protocol.build_session_entries(
                self.registry.sessions_sorted(),
                wire_sid=self.registry.wire_sid,
                max_sessions=self.hello_ack.max_sessions,
            )
            if self._fed_active():
                sessions = self.federation.merged_entries(
                    sessions, self.hello_ack.max_sessions
                )
        return protocol.build_snapshot(
            total=total,
            running=running,
            waiting=waiting,
            msg=self.headline(),
            entries=tuple(self.registry.entries),
            tokens=tokens,
            tokens_today=tokens_today,
            prompt=prompt_obj,
            sessions=sessions,
            budget=self.line_budget,
        )

    def snapshot_line(self) -> str:
        return self._compose_state_line(self.pending_prompt_obj())

    def status_payload(self) -> dict[str, Any]:
        total, running, waiting = self.registry.counts()
        tokens, tokens_today = self.tokens_totals()
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - self.started_at),
            "ble_mode": self.cfg.ble_mode,
            "ble_connected": self.link.connected,
            "ble_yielding": self.yielding,
            "idle_yield_min": self.cfg.idle_yield_min,
            "relay": self.relay.status() if self.relay is not None else {"active": False},
            "federation": {
                "peers": self.federation.peer_count(),
                "remote_sessions": self.federation.counts()[0],
                "remote_pending": self.federation.pending_total(),
            },
            "device": self.last_device_status,
            "host_id": self.cfg.host_id,
            "sessions": {"total": total, "running": running, "waiting": waiting},
            "tokens": tokens,
            "tokens_today": tokens_today,
        }

    def sessions_payload(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "agent": s.agent,
                    "sid": s.sid,
                    "title": s.title,
                    "state": s.state,
                    "cwd": s.cwd,
                    "tokens": s.tokens,
                    "last_seen": int(s.last_seen),
                }
                for s in self.registry.sessions_sorted()
            ]
        }

    # ---- IPC ----

    async def _on_ipc(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        event = msg.get("event")
        if event == "status":
            return self.status_payload()
        if event == "sessions":
            return self.sessions_payload()
        if event == "shutdown":
            self._stop_evt.set()
            return {"ok": True}
        if event == "hook":
            self.handle_hook_mirror(msg)  # fire-and-forget: no reply line
            await self._on_hook_followups(msg)
            return None
        if event == "permission_request":
            return await self.handle_permission_request(msg)
        if event == "ask_pending":
            await self.handle_ask_pending(msg)  # fire-and-forget: no reply line
            return None
        if event == "set_mode":
            return await self.handle_set_mode(msg)
        if event == "wifi":
            return await self.handle_wifi(msg)
        log.debug("ipc: unhandled event %r", msg)
        return None

    async def handle_set_mode(self, msg: dict[str, Any]) -> dict[str, Any]:
        mode = str(msg.get("mode") or "")
        if mode not in _BLE_MODES:
            return {"ok": False, "error": f"invalid mode {mode!r}"}
        await self._apply_mode(mode)
        return {"ok": True, "mode": mode}

    async def _apply_mode(self, mode: str) -> None:
        """Switch the BLE mode live: update config view, retarget the link,
        and make sure the link task exists when leaving 'off'."""
        if mode != self.cfg.ble_mode:
            log.info("daemon: ble mode %s -> %s", self.cfg.ble_mode, mode)
        self.cfg.ble_mode = mode
        if self.yielding and mode != "exclusive":
            self.yielding = False  # yield is an exclusive-mode state only
            self.link.backoff_floor = 0.0
        self._idle_since = 0.0
        await self.link.set_mode(mode)
        if mode != "off":
            self._ensure_ble_task()

    def _ensure_ble_task(self) -> None:
        if self._ble_task is None or self._ble_task.done():
            self._ble_task = asyncio.create_task(self.link.run(), name="ble-link")
            self._tasks.append(self._ble_task)

    def handle_hook_mirror(self, msg: dict[str, Any]) -> None:
        self.wake_from_yield()  # any hook event is local activity
        agent = msg.get("agent")
        if agent == "claude" and self.cfg.claude_enabled:
            claude_cli.handle_event(self.registry, msg)
        elif agent == "codex" and self.cfg.codex_enabled:
            codex.handle_event(self.registry, msg)
        else:
            return
        if msg.get("name") == "SessionEnd":
            self.router.cancel_session(str(agent), str(msg.get("session_id") or ""))

    # ---- permission gate ----

    def _decision_timeout(self, agent: str) -> float:
        return self.cfg.codex_decision if agent == "codex" else self.cfg.claude_decision

    async def handle_permission_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Decide allow/deny/ask for a blocking hook request.

        ask = "no opinion": the hook prints nothing and the agent's native
        prompt takes over. That is the answer for SAFE_TOOLS, unmatched
        (default) invocations, an unreachable device, and timeouts.
        """
        self.wake_from_yield()  # a prompt is the strongest activity signal
        agent = str(msg.get("agent") or "claude")
        sid = str(msg.get("session_id") or "")
        tool = str(msg.get("tool_name") or "")
        tool_input = msg.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        command = str(tool_input.get("command") or "")
        hint = protocol.sanitize(build_hint(tool, tool_input))

        def audit(decision: str, source: str) -> None:
            append_audit(
                self.cfg.home,
                host=self.cfg.host_id,
                agent=agent,
                sid=sid,
                tool=tool,
                hint=hint,
                decision=decision,
                source=source,
            )

        if not tool or is_safe_tool(tool):
            return {"decision": ASK}  # hard-skip: never gated, not audited

        klass = self.matchers.classify(tool, command)
        if klass == AUTO_ALLOW:
            audit(ALLOW, SOURCE_AUTO_ALLOW)
            return {"decision": ALLOW}
        if klass not in (ALWAYS_ASK, STRICT):
            return {"decision": ASK}  # default: native prompt handles it

        if self.cfg.ble_mode == "ondemand":
            result = await self._gate_ondemand(msg, agent, sid, tool, tool_input, hint, audit)
            if result is not None:
                return result
            # No local device (connect failed / pinned elsewhere): fall
            # through to the remote holder if the federation has one.
            if self._remote_gate_available():
                return await self._gate_remote(msg, agent, sid, tool, tool_input, hint, audit)
            audit(ASK, SOURCE_DAEMON_DOWN)
            return {"decision": ASK}

        if self.cfg.ble_mode == "off" or not self.link.connected:
            if self._remote_gate_available():
                return await self._gate_remote(msg, agent, sid, tool, tool_input, hint, audit)
            audit(ASK, SOURCE_DAEMON_DOWN)
            return {"decision": ASK}

        pending = self._create_pending(msg, agent, sid, tool, tool_input, hint)
        await self._send_snapshot(force=True)  # surface the prompt immediately
        decision = ASK
        try:
            decision = await self.router.wait(pending, self._decision_timeout(agent))
        finally:
            await self._cancel_displayed_prompt(
                pending.wire_id, decision_from_device=decision in (ALLOW, DENY)
            )
            await self._send_snapshot(force=True)  # and clear it again
        if decision in (ALLOW, DENY):
            audit(decision, SOURCE_DEVICE)
        else:
            audit(ASK, SOURCE_TIMEOUT_FALLBACK)
        return {"decision": decision}

    def _create_pending(self, msg, agent, sid, tool, tool_input, hint):
        return self.router.create(
            agent,
            sid,
            str(msg.get("tool_use_id") or ""),
            hint,
            tool=protocol.sanitize(tool),
            detail=protocol.sanitize(extract_detail(tool_input)),
        )

    def _remote_gate_available(self) -> bool:
        """A federated v2 holder is on the air: gates can be answered from
        the stick on ANOTHER machine, so it's worth waiting for a decision
        instead of failing straight open to the native prompt."""
        return self.relay is not None and self.relay.holder_target() is not None

    async def _gate_remote(self, msg, agent, sid, tool, tool_input, hint, audit) -> dict:
        """Gate via the federation: the prompt rides state_sync to the v2
        holder, whose stick answer comes back as a relayed decision that
        resolves our pending future exactly like a local button press.

        Our own clock still rules: the wait is the same decision timeout as
        a local gate, and anything but a relayed allow/deny fails open to
        the native prompt (relay hop adds <~1s when healthy; if the holder
        vanishes mid-wait we simply time out). On exit the origin-side
        prompt_resolved (with pid) clears any leftover holder display."""
        pending = self._create_pending(msg, agent, sid, tool, tool_input, hint)
        await self._relay_sync(force=True)  # surface the prompt immediately
        decision = ASK
        try:
            decision = await self.router.wait(pending, self._decision_timeout(agent))
        finally:
            if self.relay is not None:
                await self.relay.send_to_holder(
                    {"ev": EV_PROMPT_RESOLVED, "sid": sid, "pid": pending.wire_id}
                )
            await self._relay_sync(force=True)  # and clear it again
        if decision in (ALLOW, DENY):
            audit(decision, SOURCE_DEVICE)
        else:
            audit(ASK, SOURCE_TIMEOUT_FALLBACK)
        return {"decision": decision}

    async def _gate_ondemand(self, msg, agent, sid, tool, tool_input, hint, audit) -> Optional[dict]:
        """Ondemand gating: no persistent link. Acquire the lock, connect,
        (hello runs via the connect callback), push one focused snapshot with
        the prompt, wait for a decision up to the gate deadline minus a
        margin, clear the prompt, disconnect. A connect that fails fast
        returns None so the caller can try the federation (or fail open)."""
        async with self._ondemand_lock:
            if not await self.link.ensure_connected(timeout=ONDEMAND_CONNECT_SECS):
                return None
            await self._await_hello()
            if not self.device_sel:  # soft-pinned to another host
                await self.link.release()
                return None
            pending = self._create_pending(msg, agent, sid, tool, tool_input, hint)
            await self._send_snapshot(force=True)
            timeout = max(0.05, self._decision_timeout(agent) - ONDEMAND_DEADLINE_MARGIN_SECS)
            decision = ASK
            try:
                decision = await self.router.wait(pending, timeout)
            finally:
                await self._cancel_displayed_prompt(
                    pending.wire_id, decision_from_device=decision in (ALLOW, DENY)
                )
                await self._send_snapshot(force=True)  # clear the prompt display
                await self.link.release()
            if decision in (ALLOW, DENY):
                audit(decision, SOURCE_DEVICE)
            else:
                audit(ASK, SOURCE_TIMEOUT_FALLBACK)
            return {"decision": decision}

    async def _await_hello(self) -> None:
        """Wait out an in-flight hello negotiation (bounded by its own 2s
        deadline) so on-demand gates get v2 framing when available."""
        while (
            self.hello_ack is None
            and self._hello_deadline
            and time.monotonic() < self._hello_deadline
        ):
            await asyncio.sleep(0.05)

    async def _cancel_displayed_prompt(
        self, wire_id: str, *, decision_from_device: bool
    ) -> None:
        """Send prompt_cancel for a prompt that resolved host-side (timeout /
        session end) while it may still be on the device screen. Only fires
        when the device ack'd the ``cancel`` capability and this id was
        actually part of a sent heartbeat."""
        displayed = wire_id in self._sent_prompt_ids
        self._sent_prompt_ids.discard(wire_id)
        if not displayed or decision_from_device:
            return
        if not self.cap_active("cancel"):
            return
        await self._send_line(protocol.build_prompt_cancel(wire_id))

    # ---- BLE ----

    async def _on_ble_connect(self) -> None:
        # Spec order: hello first (nothing non-v1 before it completes), then
        # the v1 one-shots (time sync, owner), then the first heartbeat.
        if self.yielding:
            # A slow yield retry won the stick back (nobody else wanted it):
            # exit yield and restart the idle clock from scratch.
            log.info("daemon: reconnected while yielding, holding the stick again")
            self.yielding = False
            self.link.backoff_floor = 0.0
        self._idle_since = 0.0
        self.negotiated_proto = protocol.PROTO_V1
        self.hello_ack = None
        self.device_sel = True
        self._hello_deadline = 0.0
        self._rx_last_ts = time.monotonic()
        self._tx_since_rx = False
        self._sent_prompt_ids.clear()
        self._live_asks.clear()  # a fresh connection means a fresh device screen
        self._remote_live_asks.clear()  # remote asks re-forward on the next fed change
        if self.relay is not None:
            # We are (becoming) the holder: nothing of ours to mirror out,
            # and if we lose the stick later the first sync is a full push.
            self.relay.reset_state_sync()
        if await self._send_line(protocol.build_hello(self.cfg)):
            self._hello_deadline = time.monotonic() + protocol.HELLO_ACK_TIMEOUT_SECS
        await self._send_timesync()
        if self.cfg.owner:
            await self._send_line(protocol.build_owner_frame(self.cfg.owner))
        await self._send_snapshot(force=True)

    async def _on_ble_line(self, obj: dict[str, Any]) -> None:
        # Any inbound frame proves the device is still receiving us.
        self._rx_last_ts = time.monotonic()
        self._tx_since_rx = False
        cmd = obj.get("cmd")
        if cmd == "permission":
            await self.handle_device_permission(obj)
            return
        ack = obj.get("ack")
        if ack == "hello":
            await self._on_hello_ack(obj)
            return
        if ack == "wifi":
            self._resolve_wifi_ack(obj)
            return
        if isinstance(ack, str):
            # rx acks and command acks ({"ack":"rx","n":...},
            # {"ack":"prompt_cancel","ok":true}): liveness only.
            return
        if cmd is None and obj:
            # status replies and other unsolicited frames
            self.last_device_status = obj
            return
        log.debug("ble: unhandled device frame %r", obj)

    async def _on_hello_ack(self, obj: dict[str, Any]) -> None:
        ack = protocol.parse_hello_ack(obj)
        if ack is None:
            return
        self.hello_ack = ack
        self._hello_deadline = 0.0
        self.negotiated_proto = protocol.effective_proto(ack.proto)
        self.device_sel = ack.sel
        if self.v2_active:
            log.info(
                "ble: negotiated protocol v2 (maxLine=%d budget=%d maxSessions=%d "
                "caps=%s sel=%s)",
                ack.max_line, self.line_budget, ack.max_sessions,
                ",".join(ack.caps), ack.sel,
            )
        else:
            log.info("ble: device ack'd proto %d, staying v1", ack.proto)
        if not ack.sel:
            await self._on_unselected()
            return
        self.link.backoff_floor = 0.0  # selected again: normal reconnect cadence
        await self._send_snapshot(force=True)
        await self._sync_remote_asks()  # re-forward federated asks to the fresh screen

    async def _on_unselected(self) -> None:
        """hello ack said sel:false — the device is soft-pinned to another
        host and will drop this link in ~1s anyway. Disconnect ourselves and
        raise the reconnect floor to a slow retry (60s) so we don't spam a
        device that's actively pinned elsewhere, until sel flips back."""
        log.info(
            "ble: device is pinned to another host (sel:false); retrying every %.0fs",
            SLOW_RETRY_SECS,
        )
        self.link.backoff_floor = SLOW_RETRY_SECS
        await self.link.drop_connection()

    # ---- yield-when-idle (share the stick between machines) ----

    def _local_busy(self) -> bool:
        """Anything that should keep the stick held (or wake it from yield):
        a running/waiting session, a pending gate decision, a queued card —
        or a federated peer's prompt currently displayed through us."""
        _, running, waiting = self.registry.counts()
        if running or waiting or self.router.pending_count() or self.cards.queue:
            return True
        return self._fed_active() and self.federation.pending_total() > 0

    async def _maybe_idle_yield(self) -> None:
        """Tick-driven idle clock. Only exclusive mode yields: ondemand
        drops the link by itself and off never holds one."""
        if self.yielding or self.cfg.idle_yield_min <= 0 or self.cfg.ble_mode != "exclusive":
            return
        if not self.link.connected or self._local_busy():
            self._idle_since = 0.0
            return
        now = time.monotonic()
        if not self._idle_since:
            self._idle_since = now
            return
        if now - self._idle_since >= self.cfg.idle_yield_min * 60.0:
            await self._yield_now(f"idle {self.cfg.idle_yield_min}min")

    async def _yield_now(self, reason: str) -> None:
        """Gracefully free the stick for other hosts: push one last clean
        snapshot (so no stale prompt lingers on the display), drop the link,
        and slow the reconnect cadence to SLOW_RETRY_SECS. The stick's AUTO
        host selection lets the next active machine grab it; if one does,
        our slow retries keep failing and we stay patient."""
        if self.yielding or not self.link.connected:
            return
        log.info(
            "daemon: yielding BLE to other hosts (%s); retrying every %.0fs",
            reason, SLOW_RETRY_SECS,
        )
        self.yielding = True
        self._idle_since = 0.0
        await self._send_snapshot(force=True)
        self.link.backoff_floor = SLOW_RETRY_SECS
        await self.link.drop_connection()

    def wake_from_yield(self) -> None:
        """Local activity while yielded -> instant reconnect attempt burst at
        normal backoff. If another host holds the stick, the attempts fail
        (or hello-ack sel:false re-raises the floor) and we go patient again."""
        if not self.yielding:
            return
        log.info("daemon: local activity, waking from yield")
        self.yielding = False
        self._idle_since = 0.0
        self.link.backoff_floor = 0.0
        self.link.wake()

    # ---- LAN relay federation (multi-machine, one stick) ----

    async def _on_peer_event(self, payload: dict[str, Any]) -> None:
        """A verified event frame arrived from a peer bridge.

        v2 federation: state_sync folds the peer's full state into the
        merge layer (we are, or are about to be, the holder) and decision
        resolves one of OUR pending gates (we are the origin). The v1 card
        events remain the fallback for peers that never sent state_sync;
        a federated peer's card events are suppressed as redundant."""
        ev = payload.get("ev")
        peer = str(payload.get("host") or "")
        if not peer:
            return
        if ev == EV_STATE_SYNC:
            # No early yield here (unlike v1 prompt_pending): federation's
            # whole point is showing + answering the peer's prompt on OUR
            # stick, so holding the link is the feature.
            if self.federation.update(peer, payload):
                await self._fed_view_changed()
            return
        if ev == EV_DECISION:
            self._on_remote_decision(peer, payload)
            return
        name = protocol.sanitize(str(payload.get("name") or peer))[:12] or "peer"
        if ev == EV_PROMPT_PENDING:
            if self.federation.has_peer(peer):
                return  # federated peer: its prompts ride state_sync
            sid = str(payload.get("sid") or "")
            tool = protocol.sanitize(str(payload.get("tool") or ""))[:32]
            hint = str(payload.get("hint") or "")[:80]
            key = (peer, sid)
            known = self._remote_pending.get(key)
            self._remote_pending[key] = (tool, hint, time.monotonic())
            if known is None or known[:2] != (tool, hint):
                title = f"[{name}] approve: {tool}" if tool else f"[{name}] approve"
                self._emit_remote_card(peer, key, title, hint)
            await self._maybe_peer_yield()
        elif ev == EV_PROMPT_RESOLVED:
            key = (peer, str(payload.get("sid") or ""))
            self._remote_pending.pop(key, None)
            card = self._remote_cards.pop(key, None)
            if card is not None and card in self.cards.queue:
                self.cards.queue.remove(card)  # not delivered yet: retract
            # v2: the origin resolved a prompt some other way (timeout /
            # native answer) — drop it from the merged view right away.
            pid = str(payload.get("pid") or "")
            if pid and self.federation.drop_prompt_by_orig(peer, pid):
                await self._fed_view_changed()
        elif ev == EV_ATTENTION:
            if self.federation.has_peer(peer):
                return  # federated peer: its waits ride state_sync
            n = payload.get("n")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                self._emit_remote_card(
                    peer, (peer, "__attention__"), f"[{name}] {n} waiting", ""
                )

    def _on_remote_decision(self, peer: str, payload: dict[str, Any]) -> None:
        """The holder relayed a stick decision for one of OUR prompts: it
        resolves the pending future exactly as a local button press would
        (handle_permission_request then audits it as a device decision).
        Unknown/late ids are ignored — the gate has already fallen open."""
        orig_id = str(payload.get("id") or "")
        decision = str(payload.get("decision") or "")
        if decision not in DECISIONS:
            decision = ASK
        if orig_id and self.router.resolve(orig_id, decision):
            log.info("relay: decision %r for %r arrived from holder %s",
                     decision, orig_id, peer)
        else:
            log.info("relay: stale/unknown remote decision %r -> %r", orig_id, decision)

    async def _fed_view_changed(self) -> None:
        """The merged remote view changed (state_sync fold, TTL prune,
        answered/resolved prompt): retract on-device displays that no
        longer exist, forward newly arrived remote asks, and refresh the
        heartbeat (content-deduped, so an unchanged line stays quiet)."""
        current = self.federation.prompt_ids()
        for wire_id in self._fed_prompt_ids - current:
            answered = wire_id in self._fed_answered
            self._fed_answered.discard(wire_id)
            await self._cancel_displayed_prompt(wire_id, decision_from_device=answered)
        self._fed_prompt_ids = current
        self._fed_answered &= current
        await self._sync_remote_asks()
        if self.link.connected:
            await self._send_snapshot()

    async def _sync_remote_asks(self) -> None:
        """Mirror federated peers' live asks onto the stick (ask cap only).
        Display-only, like local asks: the [NAME] header prefix tells the
        user which machine's CLI is waiting for the real answer."""
        if not self.cap_active("ask") or not self.link.connected:
            return
        wanted = self.federation.remote_asks()
        for ask_id in [a for a in self._remote_live_asks if a not in wanted]:
            self._remote_live_asks.discard(ask_id)
            await self._send_line(protocol.build_ask_cancel(ask_id))
        for ask_id, ask in wanted.items():
            if ask_id in self._remote_live_asks:
                continue
            questions = [dict(q) for q in ask["questions"]]
            header = f"[{ask['name']}] {questions[0].get('header') or ''}".strip()
            questions[0]["header"] = header
            line = protocol.build_ask_evt(
                ask_id,
                questions,
                sid=ask.get("sid") or None,
                multi_select=ask.get("multiSelect") is True,
                budget=self.line_budget,
            )
            if await self._send_line(line):
                self._remote_live_asks.add(ask_id)

    def _emit_remote_card(self, peer: str, key: tuple[str, str], title: str, body: str) -> None:
        now = time.monotonic()
        if now - self._remote_card_ts.get(peer, -RELAY_CARD_RATE_SECS) < RELAY_CARD_RATE_SECS:
            return
        # Pre-cleaned with a pinned ts so the queued instance compares equal
        # for retraction on prompt_resolved. force=True: the relay does its
        # own dedupe/rate limit, the 6h content TTL must not swallow repeats.
        card = clean_card(Card(kind="remote", title=title, body=body, ts=int(time.time())))
        if self.cards.submit(card, force=True):
            self._remote_card_ts[peer] = now
            self._remote_cards[key] = card

    async def _maybe_peer_yield(self) -> None:
        """A peer announced a pending prompt. If we hold the stick but have
        no local demand (sessions/prompts — queued cards don't count, they
        are flushed below), yield right away instead of waiting out the
        idle timer, so the peer's reconnect grabs the stick in seconds."""
        if self.yielding or self.cfg.ble_mode != "exclusive" or not self.link.connected:
            return
        _, running, waiting = self.registry.counts()
        if running or waiting or self.router.pending_count():
            return
        if not self._remote_pending:
            return
        for _ in range(len(self.cards.queue)):
            await self._flush_cards()  # best effort: show the peer card first
        await self._yield_now("peer prompt pending")

    def _on_relay_holder_view(self) -> None:
        """The peer holder-view changed. Schedule a full state resync (the
        new holder starts from nothing — every peer pushes immediately on
        the holder-change datagram), and when the stick got freed while we
        have local demand, snap the reconnect loop awake instead of waiting
        out a backoff sleep (fast A-idle/B-prompts switchover)."""
        if self.relay is None:
            return
        self.relay.reset_state_sync()
        self._relay_force_sync = True  # picked up by the next 1s tick
        if self.link.connected:
            return
        if self.relay.peers.holder() is None and self._local_busy():
            if self.yielding:
                self.wake_from_yield()
            else:
                self.link.wake()

    async def _relay_sync(self, force: bool = False) -> None:
        """Tick-driven (and forced at prompt/ask edges): expire stale
        holder-side remote state, then mirror our local state out — full
        state_sync when the holder speaks v2, the v1 card events otherwise."""
        if self.relay is None:
            return
        now = time.monotonic()
        if not force and now - self._last_relay_sync < RELAY_SYNC_SECS:
            return
        self._last_relay_sync = now
        # holder-side v2: a silent peer's sessions/prompts/asks vanish
        if self.federation.prune():
            await self._fed_view_changed()
        holder_peer = self.relay.peers.holder()
        for key in list(self._remote_pending):
            expired = now - self._remote_pending[key][2] > REMOTE_PENDING_TTL_SECS
            if expired or (holder_peer is not None and holder_peer.id == key[0]):
                # Never resolved (peer died / became the holder itself):
                # a holder-peer displays its own prompts natively.
                self._remote_pending.pop(key, None)
                self._remote_cards.pop(key, None)
        if self.relay.holder_target() is not None:
            # v2 holder: stream the full state; the v1 mirror stays quiet.
            await self.relay.sync_state(self._fed_state(), force=force)
            return
        prompts: dict[str, dict] = {}
        for session in self.registry.waiting_fifo():
            hint = session.pending_prompt()
            if hint:
                prompts[session.sid] = {"agent": session.agent, "tool": "", "hint": hint[:80]}
        _, _, waiting = self.registry.counts()
        await self.relay.sync_local(prompts, waiting)

    # ---- federation origin side (we stream state to the holder) ----

    def _relay_short_name(self) -> str:
        name = self.cfg.relay_name_short or self.cfg.host_name or self.cfg.host_id
        return protocol.truncate_utf8_bytes(protocol.sanitize(name), 6) or "peer"

    def _fed_prompt_obj(self) -> Optional[dict]:
        """Our oldest pending gate as a state_sync prompt: like the local
        wire prompt but always with sid/qn (the holder decides what its
        stick can render), qn = OUR total queue depth behind it."""
        pending = self._oldest_local_pending()
        if pending is None:
            return None
        return {
            "id": pending.wire_id,
            "tool": pending.tool,
            "hint": (pending.detail or pending.hint)[:80],
            "sid": self.registry.wire_sid(pending.agent, pending.sid),
            "qn": min(protocol.PROMPT_QN_CAP, max(0, self.router.pending_count() - 1)),
        }

    def _fed_state(self) -> dict[str, Any]:
        """Everything the holder needs to represent us: the same wire
        session entries the v2 heartbeat composer would build, our oldest
        pending prompt, and our live ask. Sanitized/capped here at the
        origin (the holder re-sanitizes anyway — token holders are trusted
        to approve, not to inject display bytes)."""
        state: dict[str, Any] = {
            "name": self._relay_short_name(),
            "sessions": protocol.build_session_entries(
                self.registry.sessions_sorted(),
                wire_sid=self.registry.wire_sid,
                max_sessions=FED_STATE_MAX_SESSIONS,
            ),
        }
        prompt = self._fed_prompt_obj()
        if prompt is not None:
            state["prompt"] = prompt
        if self._fed_ask is not None:
            state["ask"] = {
                "id": self._fed_ask["id"],
                "sid": self._fed_ask["sid"],
                "questions": self._fed_ask["questions"],
                "multiSelect": self._fed_ask["multiSelect"],
            }
        return state

    # Device decision vocabulary per REFERENCE.md ("once" approves) mapped
    # onto the router's allow/deny/ask; unknown values fail open to ask.
    _WIRE_DECISIONS = {"once": ALLOW, "allow": ALLOW, "deny": DENY}

    async def handle_device_permission(self, obj: dict[str, Any]) -> None:
        wire_id = str(obj.get("id") or "")
        decision = self._WIRE_DECISIONS.get(str(obj.get("decision") or ""), ASK)
        if self.router.resolve(wire_id, decision):
            return
        route = self.router.remote_route(wire_id)
        if route is not None and self.relay is not None:
            # A federated peer's prompt: relay the decision home. The relay
            # retries in the background (2s spacing must not stall the BLE
            # inbound loop); the merged view advances to the next prompt
            # right away, and if delivery ultimately fails the origin's 5s
            # state_sync keepalive re-offers the prompt.
            peer_id, orig_id = route
            self._fed_answered.add(wire_id)
            self.federation.drop_prompt(peer_id)
            self._spawn_fed_task(self.relay.send_decision(peer_id, orig_id, decision))
            await self._fed_view_changed()  # display advances to the next prompt
            return
        log.info("ble: stale/unknown permission decision %r -> %r", wire_id, decision)

    def _spawn_fed_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._fed_tasks.add(task)
        task.add_done_callback(self._fed_tasks.discard)

    async def _send_line(self, line: str) -> bool:
        """All outbound protocol lines funnel through here so the rxack
        watch knows bytes went out."""
        if await self.link.send_line(line):
            self._tx_since_rx = True
            return True
        return False

    async def _send_timesync(self) -> None:
        if await self._send_line(protocol.build_time_frame()):
            self._last_timesync_ts = time.monotonic()

    def outbound_state_line(self) -> str:
        """The heartbeat line: v1 snapshot shape, plus sessions[] and prompt
        sid/qn once proto >= 2 is negotiated."""
        return self.snapshot_line()

    async def _send_snapshot(self, force: bool = False) -> bool:
        prompt_obj = self.pending_prompt_obj()
        line = self._compose_state_line(prompt_obj)
        now = time.monotonic()
        if not force and line == self._last_sent_line and now - self._last_send_ts < HEARTBEAT_SECS:
            return False
        if await self._send_line(line):
            self._last_sent_line = line
            self._last_send_ts = now
            # The degrade cascade may have dropped the prompt from the line;
            # only ids that actually reached the device are cancel-worthy.
            # (Inside JSON string values quotes are escaped, so a bare
            # '"prompt"' can only be the object key.)
            if prompt_obj and prompt_obj.get("id") and '"prompt"' in line:
                self._sent_prompt_ids.add(str(prompt_obj["id"]))
            return True
        return False

    # ---- ask relay (send path only; no origination source yet) ----

    async def send_ask(
        self,
        ask_id: str,
        questions: list[dict],
        *,
        agent: str = "",
        sid: str = "",
        multi_select: bool = False,
    ) -> bool:
        """Relay a structured read-only question to the device.

        Gated on the negotiated ``ask`` capability. Origination: the Claude
        PreToolUse hook forwards AskUserQuestion payloads as "ask_pending"
        IPC events (see handle_ask_pending); Codex has no source yet (its
        request_user_input only folds to state=wait).
        """
        if not self.cap_active("ask"):
            return False
        wire_sid = self.registry.wire_sid(agent, sid) if agent and sid else None
        return await self._send_line(
            protocol.build_ask_evt(
                ask_id,
                questions,
                sid=wire_sid,
                multi_select=multi_select,
                budget=self.line_budget,
            )
        )

    async def cancel_ask(self, ask_id: str) -> bool:
        if not self.cap_active("ask"):
            return False
        return await self._send_line(protocol.build_ask_cancel(ask_id))

    # ---- ask origination (Claude AskUserQuestion display) ----

    async def handle_ask_pending(self, msg: dict[str, Any]) -> None:
        """IPC "ask_pending" from the Claude PreToolUse hook: display the
        structured question on a v2 stick with the ``ask`` capability —
        ours when we hold it, the federation holder's otherwise (the ask
        rides state_sync). Display-only (PROTOCOL_V2.md): the user still
        answers in the CLI; with no stick anywhere, silently do nothing."""
        self.wake_from_yield()  # a question at the keyboard is local activity
        local_ok = self.cap_active("ask") and self.link.connected
        fed_ok = self._remote_gate_available()
        if not local_ok and not fed_ok:
            return
        agent = str(msg.get("agent") or "claude")
        sid = str(msg.get("session_id") or "")
        tool_use_id = str(msg.get("tool_use_id") or "")
        questions = convert_ask_questions(msg.get("questions"))
        if not questions:
            return
        if tool_use_id and any(
            info["tool_use_id"] == tool_use_id for info in self._live_asks.values()
        ):
            return  # duplicate delivery (two matching hook groups): keep the first
        if _wire_safe(tool_use_id) and tool_use_id not in self._live_asks:
            ask_id = tool_use_id
        else:
            ask_id = f"a{next(self._ask_counter)}"
        multi = msg.get("multiSelect") is True
        if local_ok:
            sent = await self.send_ask(
                ask_id, questions, agent=agent, sid=sid, multi_select=multi
            )
        else:  # federated: ride state_sync to the holder's stick
            self._fed_ask = {
                "id": ask_id,
                "sid": self.registry.wire_sid(agent, sid) if agent and sid else "",
                "questions": questions,
                "multiSelect": multi,
            }
            await self._relay_sync(force=True)
            sent = True
        if sent:
            self._live_asks[ask_id] = {
                "agent": agent, "sid": sid, "tool_use_id": tool_use_id,
            }

    async def _on_hook_followups(self, msg: dict[str, Any]) -> None:
        """Async follow-ups to a mirrored hook event (the sync registry
        mutation already ran): clear displayed asks that the user answered
        (PostToolUse of the same tool_use_id) or abandoned (new prompt,
        turn end, session end)."""
        agent = str(msg.get("agent") or "")
        name = msg.get("name")
        if name == "PostToolUse":
            tool_use_id = str(msg.get("tool_use_id") or "")
            if tool_use_id:
                await self._cancel_asks(
                    lambda info: info["tool_use_id"] == tool_use_id
                )
        elif name in ("UserPromptSubmit", "Stop", "SessionEnd"):
            sid = str(msg.get("session_id") or "")
            if sid:
                await self._cancel_asks(
                    lambda info: info["agent"] == agent and info["sid"] == sid
                )

    async def _cancel_asks(self, predicate) -> None:
        for ask_id in [a for a, info in self._live_asks.items() if predicate(info)]:
            del self._live_asks[ask_id]
            if self._fed_ask is not None and self._fed_ask.get("id") == ask_id:
                # Riding state_sync: drop it and let the next (forced) sync
                # clear the holder's display.
                self._fed_ask = None
                self._relay_force_sync = True
            else:
                await self.cancel_ask(ask_id)

    # ---- wifi provisioning ----

    async def handle_wifi(self, msg: dict[str, Any]) -> dict[str, Any]:
        """IPC "wifi" event: relay credentials to a v2 device and wait for
        its ack. ``msg`` (and the ``password`` local below) carry the
        plaintext password — never pass ``msg`` itself to a log call, only
        the ssid is safe to mention."""
        ssid = str(msg.get("ssid") or "")
        password = str(msg.get("pass") or "")
        if not ssid:
            return {"ok": False, "error": "missing ssid"}
        if not self.link.connected:
            return {"ok": False, "error": "device not connected"}
        if not self.v2_active:
            return {"ok": False, "error": "device firmware is v1-only (wifi needs protocol v2)"}
        log.info("daemon: wifi provisioning requested (ssid=%r)", ssid)
        ok = await self.send_wifi(ssid, password)
        if ok is None:
            return {"ok": False, "error": "device did not acknowledge within timeout"}
        return {"ok": ok}

    async def send_wifi(
        self, ssid: str, password: str, timeout: Optional[float] = None
    ) -> Optional[bool]:
        """Send ``{"cmd":"wifi",...}`` and wait for the device's
        ``{"ack":"wifi","ok":...}``. Returns the device's ``ok`` verbatim,
        or None when the send failed or nothing came back within
        ``timeout`` (default WIFI_ACK_TIMEOUT_SECS). Serialized by
        ``_wifi_lock`` so a second concurrent call can't steal the first
        call's pending ack future."""
        if timeout is None:
            timeout = WIFI_ACK_TIMEOUT_SECS
        async with self._wifi_lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._wifi_ack_future = fut
            try:
                if not await self._send_line(protocol.build_wifi_frame(ssid, password)):
                    return None
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                if self._wifi_ack_future is fut:
                    self._wifi_ack_future = None

    def _resolve_wifi_ack(self, obj: dict[str, Any]) -> None:
        fut = self._wifi_ack_future
        if fut is not None and not fut.done():
            # Strict identity check (matches parse_hello_ack's style): only
            # a literal JSON `true` counts as success, so a malformed or
            # wrong-typed `ok` can never be misread as one.
            fut.set_result(obj.get("ok") is True)

    # ---- info cards (extras: ntfy) ----

    def device_fw_version(self) -> str:
        """Firmware version string from the last hello ack (``""`` when no
        v2 device has completed the handshake yet). Consumed by the
        update-check card poller to compare against the latest release."""
        return self.hello_ack.fw if self.hello_ack is not None else ""

    def submit_card(self, card: Card) -> bool:
        """Adapter entry point: dedupe + log + queue. Delivery happens from
        the pump whenever a v2 link with the ntfy capability is up."""
        accepted = self.cards.submit(card)
        if accepted:
            self.wake_from_yield()  # a queued card wants the link back
        return accepted

    async def _flush_cards(self) -> None:
        if not self.cap_active("ntfy") or not self.link.connected:
            return
        card = self.cards.pop()
        if card is None:
            return
        line = build_ntfy_line(card, budget=self.line_budget)
        if not await self._send_line(line):
            self.cards.queue.insert(0, card)  # send failed: retry next tick

    async def _pump(self) -> None:
        """Periodic outbound traffic: heartbeat/dedupe, status poll, daily timesync."""
        while not self._stop_evt.is_set():
            await asyncio.sleep(TICK_SECS)
            try:
                await self.tick()
                if not self.link.connected:
                    continue
                now = time.monotonic()
                await self._send_snapshot()  # sends on change or 10s keepalive
                await self._flush_cards()  # one queued ntfy card per tick
                if now - self._last_status_poll_ts >= STATUS_POLL_SECS:
                    if await self._send_line(protocol.build_status_poll()):
                        self._last_status_poll_ts = now
                if self._last_timesync_ts and now - self._last_timesync_ts >= TIMESYNC_SECS:
                    await self._send_timesync()
            except Exception:  # noqa: BLE001
                log.exception("daemon: pump tick failed")

    async def tick(self) -> None:
        """Once-a-second housekeeping."""
        self.registry.reap()
        self.ledger.save_if_dirty()
        if self._hello_deadline and time.monotonic() > self._hello_deadline:
            self._hello_deadline = 0.0
            log.info("ble: no hello-ack within %.0fs, staying in protocol v1",
                     protocol.HELLO_ACK_TIMEOUT_SECS)
        await self._rxack_watch()
        await self._maybe_idle_yield()
        force = self._relay_force_sync
        self._relay_force_sync = False
        await self._relay_sync(force=force)

    async def _rxack_watch(self) -> None:
        """Send-side dead-link detection (PROTOCOL_V2.md rxack): we have sent
        bytes but seen no rx-ack (or any other traffic) for >20s -> the device
        stopped listening; drop the link so the reconnect loop revives it."""
        if not self.cap_active("rxack") or not self.link.connected:
            return
        if not self._tx_since_rx or not self._rx_last_ts:
            return
        if time.monotonic() - self._rx_last_ts <= protocol.RXACK_DEAD_SECS:
            return
        log.warning(
            "ble: sends unacknowledged for >%.0fs (rxack), declaring link dead",
            protocol.RXACK_DEAD_SECS,
        )
        self._tx_since_rx = False
        await self.link.drop_connection()

    # ---- lifecycle ----

    async def start_background(self) -> list[asyncio.Task]:
        """Extra long-running tasks (adapters, relay)."""
        tasks: list[asyncio.Task] = []
        if self.cfg.relay_enabled and not self.cfg.relay_active:
            log.warning(
                "relay: [relay] enabled but token is empty — relay stays OFF "
                "(set the same non-empty token on every machine)"
            )
        if self.cfg.relay_active:
            self.relay = RelayManager(
                host_id=self.cfg.host_id,
                host_name=self.cfg.host_name,
                port=self.cfg.relay_port,
                token=self.cfg.relay_token,
                is_holder=lambda: self.link.connected,
                on_peer_event=self._on_peer_event,
                on_holder_view=self._on_relay_holder_view,
                static_peers=self.cfg.relay_peers,
            )
            try:
                tasks.extend(await self.relay.start())
            except OSError as exc:
                log.warning("relay: disabled for this run (bind failed: %s)", exc)
                await self.relay.stop()
                self.relay = None
        if self.cfg.claude_enabled:
            tasks.append(asyncio.create_task(self.claude_tailer.run(), name="claude-jsonl"))
        if self.cfg.codex_enabled:
            tasks.append(asyncio.create_task(self.codex_scanner.run(), name="codex-sessions"))
        # Card pollers are the only network-touching tasks in the daemon and
        # exist solely behind [cards] enabled (default off).
        if self.cfg.cards_enabled:
            if self.cfg.cards_github:
                from .adapters.github_cards import GithubCardsPoller

                poller = GithubCardsPoller(self.submit_card, repos=self.cfg.cards_repos)
                tasks.append(asyncio.create_task(poller.run(), name="github-cards"))
            if (
                self.cfg.cards_weather_lat is not None
                and self.cfg.cards_weather_lon is not None
            ):
                from .adapters.weather_cards import WeatherCardsPoller

                weather = WeatherCardsPoller(
                    self.submit_card,
                    lat=self.cfg.cards_weather_lat,
                    lon=self.cfg.cards_weather_lon,
                )
                tasks.append(asyncio.create_task(weather.run(), name="weather-cards"))
            if self.cfg.cards_update_check:
                from .adapters.update_cards import UpdateCardsPoller

                updates = UpdateCardsPoller(
                    self.submit_card,
                    get_device_fw=self.device_fw_version,
                    repo=self.cfg.cards_firmware_repo,
                )
                tasks.append(asyncio.create_task(updates.run(), name="update-cards"))
        return tasks

    async def run(self) -> None:
        self.cfg.home.mkdir(parents=True, exist_ok=True)
        port = await self.ipc.start(self.cfg.ipc_port)
        write_endpoint(self.cfg.home, port, self.token, os.getpid())
        log.info(
            "daemon: ipc on 127.0.0.1:%d, home=%s, ble mode=%s",
            port, self.cfg.home, self.cfg.ble_mode,
        )
        self._tasks = [asyncio.create_task(self._pump(), name="pump")]
        if self.cfg.ble_mode != "off":
            self._ensure_ble_task()  # mode=off: BLE tasks never start
        self._tasks.extend(await self.start_background())
        try:
            await self._stop_evt.wait()
        finally:
            for task in list(self._tasks) + list(self._fed_tasks):
                task.cancel()
            for task in list(self._tasks) + list(self._fed_tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if self.relay is not None:
                await self.relay.stop()
            await self.link.stop()
            await self.ipc.stop()
            with contextlib.suppress(Exception):
                self.ledger.save_if_dirty()
                self.claude_tailer.flush()
            remove_endpoint(self.cfg.home)
            log.info("daemon: stopped")

    def request_stop(self) -> None:
        self._stop_evt.set()


def run_daemon(cfg: Config) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    daemon = Daemon(cfg)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("daemon: interrupted")
    return 0
