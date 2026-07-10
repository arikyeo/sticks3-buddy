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
from .relay import EV_ATTENTION, EV_PROMPT_PENDING, EV_PROMPT_RESOLVED, RelayManager
from .decision import ALLOW, ASK, DENY, DecisionRouter, build_hint, extract_detail
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

    def headline(self) -> str:
        total, running, waiting = self.registry.counts()
        if waiting:
            # Attention hints (Notification messages) are not actionable
            # prompts (no wire id), so they surface here instead: per
            # REFERENCE.md the snapshot "prompt" only appears when a
            # permission decision is needed.
            if self.router.pending_count() == 0:
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

    def pending_prompt_obj(self) -> Optional[dict]:
        """Wire prompt object for the heartbeat: the oldest-waiting session's
        pending gate decision first, else the oldest pending overall. v2 adds
        sid (pinned wire sid) and qn (how many more prompts this session has
        queued behind the shown one, capped at PROMPT_QN_CAP)."""
        pending = None
        for session in self.registry.waiting_fifo():
            pending = self.router.oldest_for(session.agent, session.sid)
            if pending is not None:
                break
        if pending is None:
            pending = self.router.oldest()
        if pending is None:
            return None
        obj: dict[str, Any] = {
            "id": pending.wire_id,
            "tool": pending.tool,
            "hint": pending.detail or pending.hint,
        }
        if self.v2_active:
            obj["sid"] = self.registry.wire_sid(pending.agent, pending.sid)
            obj["qn"] = min(
                protocol.PROMPT_QN_CAP,
                max(0, self.router.session_pending_count(pending.agent, pending.sid) - 1),
            )
        return obj

    def _compose_state_line(self, prompt_obj: Optional[dict]) -> str:
        total, running, waiting = self.registry.counts()
        tokens, tokens_today = self.tokens_totals()
        sessions = None
        if self.v2_active and self.cap_active("sessions") and self.hello_ack is not None:
            sessions = protocol.build_session_entries(
                self.registry.sessions_sorted(),
                wire_sid=self.registry.wire_sid,
                max_sessions=self.hello_ack.max_sessions,
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
            return None
        if event == "permission_request":
            return await self.handle_permission_request(msg)
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
            return await self._gate_ondemand(msg, agent, sid, tool, tool_input, hint, audit)

        if self.cfg.ble_mode == "off" or not self.link.connected:
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

    async def _gate_ondemand(self, msg, agent, sid, tool, tool_input, hint, audit) -> dict:
        """Ondemand gating: no persistent link. Acquire the lock, connect,
        (hello runs via the connect callback), push one focused snapshot with
        the prompt, wait for a decision up to the gate deadline minus a
        margin, clear the prompt, disconnect. A connect that fails fast
        fails open ("ask")."""
        async with self._ondemand_lock:
            if not await self.link.ensure_connected(timeout=ONDEMAND_CONNECT_SECS):
                audit(ASK, SOURCE_DAEMON_DOWN)
                return {"decision": ASK}
            await self._await_hello()
            if not self.device_sel:  # soft-pinned to another host
                audit(ASK, SOURCE_DAEMON_DOWN)
                await self.link.release()
                return {"decision": ASK}
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
        a running/waiting session, a pending gate decision, a queued card."""
        _, running, waiting = self.registry.counts()
        return bool(running or waiting or self.router.pending_count() or self.cards.queue)

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
        """A verified event frame arrived from a peer bridge (we hold the
        stick). Card emission is rate-limited to 1/peer/RELAY_CARD_RATE_SECS
        and deduped per (peer, sid) content, and a peer prompt prefers an
        early yield when we are idle."""
        ev = payload.get("ev")
        peer = str(payload.get("host") or "")
        if not peer:
            return
        name = protocol.sanitize(str(payload.get("name") or peer))[:12] or "peer"
        if ev == EV_PROMPT_PENDING:
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
        elif ev == EV_ATTENTION:
            n = payload.get("n")
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                self._emit_remote_card(
                    peer, (peer, "__attention__"), f"[{name}] {n} waiting", ""
                )

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
        """The peer holder-view changed. When the stick got freed and we
        have local demand, snap the reconnect loop awake instead of waiting
        out a backoff sleep (fast A-idle/B-prompts switchover)."""
        if self.relay is None or self.link.connected:
            return
        if self.relay.peers.holder() is None and self._local_busy():
            if self.yielding:
                self.wake_from_yield()
            else:
                self.link.wake()

    async def _relay_sync(self) -> None:
        """Tick-driven: expire stale remote-pending markers and mirror local
        waiting prompts to the current holder (no-op while we are it)."""
        if self.relay is None:
            return
        now = time.monotonic()
        if now - self._last_relay_sync < RELAY_SYNC_SECS:
            return
        self._last_relay_sync = now
        holder_peer = self.relay.peers.holder()
        for key in list(self._remote_pending):
            expired = now - self._remote_pending[key][2] > REMOTE_PENDING_TTL_SECS
            if expired or (holder_peer is not None and holder_peer.id == key[0]):
                # Never resolved (peer died / became the holder itself):
                # a holder-peer displays its own prompts natively.
                self._remote_pending.pop(key, None)
                self._remote_cards.pop(key, None)
        prompts: dict[str, dict] = {}
        for session in self.registry.waiting_fifo():
            hint = session.pending_prompt()
            if hint:
                prompts[session.sid] = {"agent": session.agent, "tool": "", "hint": hint[:80]}
        _, _, waiting = self.registry.counts()
        await self.relay.sync_local(prompts, waiting)

    # Device decision vocabulary per REFERENCE.md ("once" approves) mapped
    # onto the router's allow/deny/ask; unknown values fail open to ask.
    _WIRE_DECISIONS = {"once": ALLOW, "allow": ALLOW, "deny": DENY}

    async def handle_device_permission(self, obj: dict[str, Any]) -> None:
        wire_id = str(obj.get("id") or "")
        decision = self._WIRE_DECISIONS.get(str(obj.get("decision") or ""), ASK)
        if not self.router.resolve(wire_id, decision):
            log.info("ble: stale/unknown permission decision %r -> %r", wire_id, decision)

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

        Gated on the negotiated ``ask`` capability. There is currently no
        host-side origination source (the Claude hook set exposes no
        AskUserQuestion payload; Codex request_user_input only folds to
        state=wait), so this is a library path for adapters that have data.
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
        await self._relay_sync()

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
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
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
