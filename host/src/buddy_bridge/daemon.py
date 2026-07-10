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
from .cards import CARDS_LOG_FILENAME, Card, CardDeduper, CardLog, CardPipeline, build_ntfy_line
from .config import Config
from .ble.link import SLOW_RETRY_SECS, LinkManager
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
        self._ondemand_lock = asyncio.Lock()  # one on-demand gate at a time
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
        await self.link.set_mode(mode)
        if mode != "off":
            self._ensure_ble_task()

    def _ensure_ble_task(self) -> None:
        if self._ble_task is None or self._ble_task.done():
            self._ble_task = asyncio.create_task(self.link.run(), name="ble-link")
            self._tasks.append(self._ble_task)

    def handle_hook_mirror(self, msg: dict[str, Any]) -> None:
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

    # ---- info cards (extras: ntfy) ----

    def submit_card(self, card: Card) -> bool:
        """Adapter entry point: dedupe + log + queue. Delivery happens from
        the pump whenever a v2 link with the ntfy capability is up."""
        return self.cards.submit(card)

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
        """Extra long-running tasks (adapters)."""
        tasks: list[asyncio.Task] = []
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
