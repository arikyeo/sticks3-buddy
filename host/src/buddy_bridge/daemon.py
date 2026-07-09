"""buddy-bridge daemon: asyncio supervisor tying IPC, BLE and the protocol together.

Responsibilities (phase 1):
  * loopback IPC server on an ephemeral port + endpoint.json handshake
  * persistent BLE link per [ble] mode
  * heartbeat: 10s keepalive, immediate on-change sends deduped by content
  * timesync frame on connect and daily thereafter
  * device status poll ({"cmd":"status"}) every 15s
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
from .adapters import claude_cli
from .adapters.claude_jsonl import AssistantEvent, JsonlTailer, claude_projects_root
from .config import Config
from .ble.link import LinkManager
from .ipc.endpoint import remove_endpoint, write_endpoint
from .ipc.server import IpcServer
from .registry import SessionRegistry
from .usage import LEDGER_FILENAME, UsageLedger

log = logging.getLogger(__name__)

HEARTBEAT_SECS = 10.0
STATUS_POLL_SECS = 15.0
TIMESYNC_SECS = 24 * 3600.0
TICK_SECS = 1.0


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
        self.ledger = UsageLedger(cfg.home / LEDGER_FILENAME)
        self.claude_tailer = JsonlTailer(
            root=claude_projects_root(),
            offsets_path=cfg.home / "offsets.json",
            on_event=self._on_claude_transcript,
        )
        self.started_at = time.time()
        self.last_device_status: dict[str, Any] = {}
        self._last_sent_line = ""
        self._last_send_ts = 0.0
        self._last_timesync_ts = 0.0
        self._last_status_poll_ts = 0.0
        self._stop_evt = asyncio.Event()

    # ---- transcript + usage ----

    def _on_claude_transcript(self, event: AssistantEvent) -> None:
        if event.session_id:
            self.registry.add_tokens("claude", event.session_id, event.output_tokens)
            if event.cwd and self.registry.get("claude", event.session_id) is not None:
                self.registry.upsert("claude", event.session_id, event.cwd)
            self.ledger.add("claude", event.session_id, event.output_tokens)
        if event.text:
            self.registry.add_entry(protocol.format_entry(event.hhmm, event.text))

    # ---- snapshot source ----

    def tokens_totals(self) -> tuple[int, int]:
        """(lifetime, today) output-token totals from the persistent ledger."""
        return self.ledger.totals()

    def headline(self) -> str:
        total, running, waiting = self.registry.counts()
        if waiting:
            return f"{waiting} waiting for you"
        if running:
            return f"{running} running"
        if total:
            return f"{total} idle"
        return "bridge up"

    def pending_prompt(self) -> Optional[str]:
        return self.registry.pending_prompt()

    def snapshot_line(self) -> str:
        total, running, waiting = self.registry.counts()
        tokens, tokens_today = self.tokens_totals()
        return protocol.build_snapshot(
            total=total,
            running=running,
            waiting=waiting,
            msg=self.headline(),
            entries=tuple(self.registry.entries),
            tokens=tokens,
            tokens_today=tokens_today,
            prompt=self.pending_prompt(),
        )

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
        log.debug("ipc: unhandled event %r", msg)
        return None

    def handle_hook_mirror(self, msg: dict[str, Any]) -> None:
        agent = msg.get("agent")
        if agent == "claude" and self.cfg.claude_enabled:
            claude_cli.handle_event(self.registry, msg)

    async def handle_permission_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Gate requests answer "ask" until the DecisionRouter lands (phase 4)."""
        return {"decision": "ask"}

    # ---- BLE ----

    async def _on_ble_connect(self) -> None:
        await self._send_timesync()
        if self.cfg.owner:
            await self.link.send_line(protocol.build_owner_frame(self.cfg.owner))
        await self._send_snapshot(force=True)

    async def _on_ble_line(self, obj: dict[str, Any]) -> None:
        cmd = obj.get("cmd")
        if cmd == "permission":
            await self.handle_device_permission(obj)
            return
        if cmd is None and obj:
            # status replies and other unsolicited frames
            self.last_device_status = obj
            return
        log.debug("ble: unhandled device frame %r", obj)

    async def handle_device_permission(self, obj: dict[str, Any]) -> None:
        """Device permission decisions; wired to the DecisionRouter in phase 4."""
        log.info("ble: permission frame ignored (gate not active): %r", obj)

    async def _send_timesync(self) -> None:
        if await self.link.send_line(protocol.build_time_frame()):
            self._last_timesync_ts = time.monotonic()

    async def _send_snapshot(self, force: bool = False) -> bool:
        line = self.snapshot_line()
        now = time.monotonic()
        if not force and line == self._last_sent_line and now - self._last_send_ts < HEARTBEAT_SECS:
            return False
        if await self.link.send_line(line):
            self._last_sent_line = line
            self._last_send_ts = now
            return True
        return False

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
                if now - self._last_status_poll_ts >= STATUS_POLL_SECS:
                    if await self.link.send_line(protocol.build_status_poll()):
                        self._last_status_poll_ts = now
                if self._last_timesync_ts and now - self._last_timesync_ts >= TIMESYNC_SECS:
                    await self._send_timesync()
            except Exception:  # noqa: BLE001
                log.exception("daemon: pump tick failed")

    async def tick(self) -> None:
        """Once-a-second housekeeping (extended by later phases)."""
        self.registry.reap()
        self.ledger.save_if_dirty()

    # ---- lifecycle ----

    async def start_background(self) -> list[asyncio.Task]:
        """Extra long-running tasks (adapters); extended in later phases."""
        tasks: list[asyncio.Task] = []
        if self.cfg.claude_enabled:
            tasks.append(asyncio.create_task(self.claude_tailer.run(), name="claude-jsonl"))
        return tasks

    async def run(self) -> None:
        self.cfg.home.mkdir(parents=True, exist_ok=True)
        port = await self.ipc.start(self.cfg.ipc_port)
        write_endpoint(self.cfg.home, port, self.token, os.getpid())
        log.info(
            "daemon: ipc on 127.0.0.1:%d, home=%s, ble mode=%s",
            port, self.cfg.home, self.cfg.ble_mode,
        )
        tasks = [
            asyncio.create_task(self.link.run(), name="ble-link"),
            asyncio.create_task(self._pump(), name="pump"),
        ]
        tasks.extend(await self.start_background())
        try:
            await self._stop_evt.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
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
