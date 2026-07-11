"""Interactive Telegram bot: query sessions and answer prompts from anywhere.

One getUpdates long-poll task per daemon. Inbound is allowlisted-chats-only
(everything else is ignored and the chat id logged once); within an allowed
chat a sliding-window rate limit caps command/callback bursts.

Commands
  /sessions, /status  merged session list across this machine and every
                      federated peer (the holder view — same data as the
                      stick dashboard)
  /pending            only what's waiting, each with inline buttons
  /mute [min], /unmute  per-chat push suppression (commands still answered)
  /help

Inline answering
  * Permission prompts: [Allow once][Deny] buttons route the callback
    through the DecisionRouter exactly like a stick button press — local
    wire ids resolve the pending future (the gate's own fail-open timeout
    still rules), federated ``r<k>.x`` ids relay the decision home via
    ``Daemon.handle_device_permission``. Gated on [telegram]
    allow_decisions; every decision is audited (source="telegram",
    by=<chat id>).
  * AskUserQuestion: with [telegram] answer_asks the Claude PreToolUse
    hook blocks on an ``ask_answer`` IPC request; the chosen option(s)
    return as the hook's ``updatedInput.answers`` and the CLI never shows
    the terminal dialog. A timeout resolves to no-answer and the native
    dialog takes over (fail open, never brick the CLI).

Secret hygiene matches notify/telegram.py: no token, no chat/message
content in any log line — ids and method names only.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import protocol
from ..audit import SOURCE_TELEGRAM, append_audit
from ..decision import ALLOW, DENY, _wire_safe
from .telegram import (
    AGENT_LETTER,
    GLYPH_ASK,
    GLYPH_DONE,
    GLYPH_PROMPT,
    STATE_GLYPH,
    TelegramNotifier,
    decision_keyboard,
    fmt_tokens,
)

log = logging.getLogger(__name__)

POLL_TIMEOUT_SECS = 50  # getUpdates long-poll hold
POLL_HTTP_TIMEOUT_SECS = 75.0  # socket timeout must outlive the hold
BACKOFF_START_SECS = 1.0
BACKOFF_MAX_SECS = 60.0
INBOUND_WINDOW_SECS = 10.0  # per-chat inbound rate limit ...
INBOUND_WINDOW_MAX = 20  # ... at most this many updates per window
SESSIONS_MAX = 16  # /sessions rows (waiting-first, so waits always fit)
ASK_MAX_QUESTIONS = 4
ASK_MAX_OPTIONS = 4
EXPIRED_NOTE = "⌛ expired — answer in the terminal"


@dataclass
class PendingAsk:
    """One AskUserQuestion waiting for a Telegram answer. ``questions`` are
    the ORIGINAL hook dicts: the resolved answers must be keyed by the
    verbatim question text for Claude's ``updatedInput.answers``."""

    ask_id: str
    agent: str
    sid: str
    tool_use_id: str
    questions: list
    future: asyncio.Future
    created: float
    text: str = ""  # base message text (edits append to it)
    selections: dict = field(default_factory=dict)  # qi -> set of toggled oi
    answers: dict = field(default_factory=dict)  # qi -> label | [labels]
    messages: list = field(default_factory=list)  # (chat_id, message_id)


class TelegramBot:
    """getUpdates dispatcher. Holds a reference to the daemon for the
    session registry, decision router, federation view, and relay — all
    read/resolve paths a stick button press would use."""

    def __init__(
        self,
        cfg,
        daemon,
        notifier: TelegramNotifier,
        client=None,
        *,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.daemon = daemon
        self.notifier = notifier
        self.client = client if client is not None else notifier.client
        self._now = now_fn
        self._offset = 0
        self._primed = False  # first poll drops the offline backlog
        self._backoff = BACKOFF_START_SECS
        self._asks: dict[str, PendingAsk] = {}
        self._ask_counter = itertools.count(1)
        self._inbound: dict[int, deque] = {}
        self._unknown_chats: set[int] = set()
        self._stopped = False

    # ---- long-poll loop ----

    async def run(self) -> None:
        log.info(
            "telegram: bot polling (%d allowlisted chat(s), decisions=%s, asks=%s)",
            len(self.cfg.telegram_chats),
            "on" if self.cfg.telegram_decisions else "off",
            "on" if self.cfg.telegram_answer_asks else "off",
        )
        while not self._stopped:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the poll loop never dies
                log.exception("telegram: poll loop error")
                await self._sleep_backoff()

    async def _poll_once(self) -> None:
        if not self._primed:
            # Updates sent while the daemon was down are dropped on purpose:
            # their pendings are gone, and replaying stale commands after a
            # restart would be surprising.
            result = await self.client.acall(
                "getUpdates", {"offset": -1, "timeout": 0}, retries=0
            )
            if result is None:
                await self._sleep_backoff()
                return
            if isinstance(result, list) and result:
                last = result[-1].get("update_id")
                if isinstance(last, int):
                    self._offset = last + 1
                log.info("telegram: dropped %d pre-startup update(s)", len(result))
            self._primed = True
            self._backoff = BACKOFF_START_SECS
            return
        result = await self.client.acall(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": POLL_TIMEOUT_SECS,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=POLL_HTTP_TIMEOUT_SECS,
            retries=0,
        )
        if result is None:
            await self._sleep_backoff()
            return
        self._backoff = BACKOFF_START_SECS
        if not isinstance(result, list):
            return
        for upd in result:
            if not isinstance(upd, dict):
                continue
            uid = upd.get("update_id")
            if isinstance(uid, int):
                self._offset = max(self._offset, uid + 1)
            try:
                await self._on_update(upd)
            except Exception:  # noqa: BLE001
                log.exception("telegram: update handling failed")

    async def _sleep_backoff(self) -> None:
        await asyncio.sleep(self._backoff)
        self._backoff = min(BACKOFF_MAX_SECS, self._backoff * 2)

    def stop(self) -> None:
        self._stopped = True

    # ---- gatekeeping ----

    def _allowed(self, chat_id: object) -> bool:
        if isinstance(chat_id, int) and chat_id in self.cfg.telegram_chats:
            return True
        if isinstance(chat_id, int) and chat_id not in self._unknown_chats:
            self._unknown_chats.add(chat_id)
            log.warning("telegram: ignoring non-allowlisted chat %s", chat_id)
        return False

    def _rate_ok(self, chat_id: int) -> bool:
        dq = self._inbound.setdefault(chat_id, deque())
        now = self._now()
        while dq and now - dq[0] > INBOUND_WINDOW_SECS:
            dq.popleft()
        if len(dq) >= INBOUND_WINDOW_MAX:
            log.debug("telegram: rate limit hit for chat %s", chat_id)
            return False
        dq.append(now)
        return True

    # ---- update dispatch ----

    async def _on_update(self, upd: dict) -> None:
        message = upd.get("message")
        if isinstance(message, dict):
            await self._on_message(message)
            return
        callback = upd.get("callback_query")
        if isinstance(callback, dict):
            await self._on_callback(callback)

    async def _on_message(self, message: dict) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = chat.get("id")
        if not self._allowed(chat_id) or not self._rate_ok(chat_id):
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()
        if cmd in ("/sessions", "/status"):
            await self._cmd_sessions(chat_id, header=(cmd == "/status"))
        elif cmd == "/pending":
            await self._cmd_pending(chat_id)
        elif cmd == "/mute":
            await self._cmd_mute(chat_id, arg)
        elif cmd == "/unmute":
            self.notifier.unmute(chat_id)
            await self._send(chat_id, "unmuted — pushes resume")
        elif cmd in ("/help", "/start"):
            await self._cmd_help(chat_id)
        else:
            await self._send(chat_id, "unknown command — /help")

    # ---- commands ----

    def _short_host(self) -> str:
        name = self.cfg.host_name or self.cfg.host_id or "host"
        return name[:12]

    def _merged_session_entries(self) -> list[dict]:
        local = [
            dict(entry)
            for entry in protocol.build_session_entries(
                self.daemon.registry.sessions_sorted(),
                wire_sid=self.daemon.registry.wire_sid,
                max_sessions=SESSIONS_MAX,
            )
        ]
        host = self._short_host()
        for entry in local:
            entry["title"] = f"[{host}] {entry['title']}".rstrip()
        if self.daemon._fed_active():
            return self.daemon.federation.merged_entries(local, SESSIONS_MAX)
        return local

    @staticmethod
    def _session_line(entry: dict) -> str:
        glyph = STATE_GLYPH.get(entry.get("state"), "·")
        agent = AGENT_LETTER.get(entry.get("agent"), "?")
        line = f"{glyph} {agent} {entry.get('title') or '?'}"
        tok = entry.get("tok")
        if isinstance(tok, int) and tok > 0:
            line += f" · {fmt_tokens(tok)}t"
        last = entry.get("last")
        if last:
            line += f"\n      {last}"
        return line

    async def _cmd_sessions(self, chat_id: int, *, header: bool) -> None:
        entries = self._merged_session_entries()
        lines: list[str] = []
        if header:
            total, running, waiting = self.daemon.merged_counts()
            stick = "stick ✓" if self.daemon.link.connected else "stick ✗"
            peers = self.daemon.federation.peer_count()
            lines.append(
                f"{self._short_host()} · {total} session(s): {running} running, "
                f"{waiting} waiting · {stick} · {peers} peer(s)"
            )
            lines.append(self.daemon.headline())
        if not entries:
            lines.append("no sessions")
        lines.extend(self._session_line(e) for e in entries)
        await self._send(chat_id, "\n".join(lines))

    async def _cmd_pending(self, chat_id: int) -> None:
        buttons = self.cfg.telegram_decisions
        sent = 0
        answered_sids = set()
        for pending in self.daemon.router.all_pending():
            answered_sids.add((pending.agent, pending.sid))
            title = self.notifier._title_for(pending.agent, pending.sid)
            text = (
                f"{GLYPH_PROMPT} {self._short_host()} · {pending.agent}/{title} "
                f"wants: {pending.tool or '?'}\n  {pending.detail or pending.hint}"
            )
            kb = decision_keyboard(pending.wire_id) if buttons else None
            if await self._send(chat_id, text, kb) is not None:
                sent += 1
        if self.daemon._fed_active():
            for prompt in self.daemon.federation.remote_prompts():
                text = (
                    f"{GLYPH_PROMPT} {prompt.get('name') or 'peer'} · wants: "
                    f"{prompt.get('tool') or '?'}\n  {prompt.get('hint') or ''}"
                )
                kb = decision_keyboard(str(prompt["id"])) if buttons and prompt.get("id") else None
                if await self._send(chat_id, text, kb) is not None:
                    sent += 1
        for ask in list(self._asks.values()):
            if ask.future.done():
                continue
            mid = await self._send(chat_id, ask.text, self._ask_keyboard(ask))
            if mid is not None:
                ask.messages.append((chat_id, mid))
                sent += 1
        # Waiting sessions with no actionable prompt (Stop/Notification
        # waits): visible, but there is nothing to press.
        for session in self.daemon.registry.waiting_fifo():
            if (session.agent, session.sid) in answered_sids:
                continue
            hint = session.pending_prompt() or "waiting at the terminal"
            text = (
                f"{GLYPH_PROMPT} {self._short_host()} · {session.agent}/"
                f"{session.title or session.sid[:8]}\n  {hint}\n(no remote action)"
            )
            if await self._send(chat_id, text) is not None:
                sent += 1
        if not sent:
            await self._send(chat_id, f"{GLYPH_DONE} nothing waiting")
        elif not buttons:
            await self._send(
                chat_id, "read-only: enable [telegram] allow_decisions for buttons"
            )

    async def _cmd_mute(self, chat_id: int, arg: str) -> None:
        try:
            minutes = max(1, int(arg)) if arg else 60
        except ValueError:
            minutes = 60
        self.notifier.mute(chat_id, minutes)
        await self._send(
            chat_id, f"muted pushes for {minutes}min (commands still answered) — /unmute"
        )

    async def _cmd_help(self, chat_id: int) -> None:
        decisions = "on" if self.cfg.telegram_decisions else "off"
        asks = "on" if self.cfg.telegram_answer_asks else "off"
        await self._send(
            chat_id,
            "buddy-bridge virtual buddy\n"
            "/sessions — merged session list (all machines)\n"
            "/status — same, with a summary header\n"
            "/pending — what's waiting, with buttons\n"
            "/mute [min] — pause pushes (default 60)\n"
            "/unmute — resume pushes\n"
            f"decisions: {decisions} · answer_asks: {asks}",
        )

    # ---- callbacks ----

    async def _on_callback(self, callback: dict) -> None:
        cq_id = str(callback.get("id") or "")
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = chat.get("id")
        if not self._allowed(chat_id):
            await self._answer_cq(cq_id)  # stop the spinner, say nothing
            return
        if not self._rate_ok(chat_id):
            await self._answer_cq(cq_id)
            return
        data = str(callback.get("data") or "")
        if data.startswith("d:"):
            await self._decision_callback(chat_id, cq_id, message, data)
        elif data.startswith("q:"):
            await self._ask_callback(chat_id, cq_id, data)
        else:
            await self._answer_cq(cq_id)

    async def _decision_callback(
        self, chat_id: int, cq_id: str, message: dict, data: str
    ) -> None:
        parts = data.split(":")
        if len(parts) != 3 or parts[2] not in ("a", "d"):
            await self._answer_cq(cq_id)
            return
        wire_id, act = parts[1], parts[2]
        if not self.cfg.telegram_decisions:
            await self._answer_cq(
                cq_id, "decisions are off ([telegram] allow_decisions=false)"
            )
            return
        decision = ALLOW if act == "a" else DENY
        by = str(chat_id)
        note = f"{'✅ allowed' if decision == ALLOW else '❌ denied'} via Telegram"
        # 1) Local pending: resolve the future exactly like a stick button;
        #    the waiting gate path writes the audit line (source=telegram).
        if self.daemon.router.resolve(wire_id, decision, source=SOURCE_TELEGRAM, by=by):
            log.info("telegram: %s %r from chat %s", decision, wire_id, chat_id)
            await self._answer_cq(cq_id, note)
            await self._edit_resolution(message, note)
            return
        # 2) Federated peer's prompt: relay home through the same path a
        #    stick button uses; audit here (the origin machine's own gate
        #    audits its outcome on its side).
        route = self.daemon.router.remote_route(wire_id)
        if route is not None:
            prompt = next(
                (
                    p
                    for p in self.daemon.federation.remote_prompts()
                    if p.get("id") == wire_id
                ),
                {},
            )
            append_audit(
                self.cfg.home,
                host=self.cfg.host_id,
                agent="remote",
                sid=str(prompt.get("sid") or wire_id),
                tool=str(prompt.get("tool") or ""),
                hint=str(prompt.get("hint") or ""),
                decision=decision,
                source=SOURCE_TELEGRAM,
                by=by,
            )
            log.info("telegram: %s %r (remote) from chat %s", decision, wire_id, chat_id)
            await self.daemon.handle_device_permission(
                {"cmd": "permission", "id": wire_id,
                 "decision": "once" if decision == ALLOW else "deny"}
            )
            await self._answer_cq(cq_id, note)
            await self._edit_resolution(message, note)
            return
        await self._answer_cq(cq_id, "expired — already resolved")
        await self._edit_resolution(message, "⌛ expired")

    async def _edit_resolution(self, message: dict, note: str) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id, message_id = chat.get("id"), message.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        text = str(message.get("text") or "")
        await self._edit(chat_id, message_id, f"{text}\n{note}")

    # ---- AskUserQuestion answering ----

    async def handle_ask_answer(self, msg: dict) -> dict:
        """Blocking IPC from the Claude hook (--answer-asks): park the
        question on Telegram and wait for a full answer. ``{"answers":
        None}`` on ANY non-answer path — the hook prints nothing and the
        native terminal dialog takes over."""
        none: dict[str, Any] = {"answers": None}
        if not self.cfg.telegram_answer_asks:
            return none
        questions = self._clean_questions(msg.get("questions"))
        if not questions:
            return none
        agent = str(msg.get("agent") or "claude")
        sid = str(msg.get("session_id") or "")
        tool_use_id = str(msg.get("tool_use_id") or "")
        ask = None
        if tool_use_id:  # duplicate hook delivery: share the pending ask
            ask = next(
                (a for a in self._asks.values() if a.tool_use_id == tool_use_id), None
            )
        if ask is None:
            if _wire_safe(tool_use_id) and tool_use_id not in self._asks:
                ask_id = tool_use_id
            else:
                ask_id = f"tga{next(self._ask_counter)}"
            ask = PendingAsk(
                ask_id=ask_id,
                agent=agent,
                sid=sid,
                tool_use_id=tool_use_id,
                questions=questions,
                future=asyncio.get_running_loop().create_future(),
                created=self._now(),
            )
            ask.text = self._ask_text(ask)
            self._asks[ask_id] = ask
            if not await self._broadcast_ask(ask):
                # every chat muted/unreachable: fail open immediately
                # instead of holding the CLI hostage for the full timeout
                self._asks.pop(ask_id, None)
                return none
        timeout = (
            self.cfg.codex_decision if agent == "codex" else self.cfg.claude_decision
        )
        try:
            answers = await asyncio.wait_for(asyncio.shield(ask.future), timeout)
        except asyncio.TimeoutError:
            answers = None
        if answers is None:
            if not ask.future.done():
                ask.future.set_result(None)
                await self._edit_ask_messages(ask, EXPIRED_NOTE)
            self._asks.pop(ask.ask_id, None)
            return none
        self._asks.pop(ask.ask_id, None)
        return {"answers": answers}

    @staticmethod
    def _clean_questions(raw: object) -> list[dict]:
        """Validate the hook's raw AskUserQuestion questions; keep the
        ORIGINAL dicts (question text is the answers key)."""
        if not isinstance(raw, list):
            return []
        out = []
        for q in raw[:ASK_MAX_QUESTIONS]:
            if not isinstance(q, dict):
                continue
            if not (q.get("question") or q.get("text")):
                continue
            options = q.get("options")
            if not isinstance(options, list) or not options:
                continue
            out.append(q)
        return out

    @staticmethod
    def _q_text(q: dict) -> str:
        return str(q.get("question") or q.get("text") or "")

    @staticmethod
    def _opt_label(q: dict, oi: int) -> str:
        options = q.get("options") or []
        if 0 <= oi < len(options):
            opt = options[oi]
            if isinstance(opt, dict):
                return str(opt.get("label") or "")
            return str(opt)
        return ""

    def _ask_text(self, ask: PendingAsk) -> str:
        title = self.notifier._title_for(ask.agent, ask.sid)
        lines = [f"{GLYPH_ASK} {self._short_host()} · {ask.agent}/{title} asks:"]
        many = len(ask.questions) > 1
        for qi, q in enumerate(ask.questions):
            header = str(q.get("header") or "").strip()
            prefix = f"{qi + 1}. " if many else ""
            lines.append(f"{prefix}{header + ': ' if header else ''}{self._q_text(q)}")
            options = q.get("options") or []
            for oi, opt in enumerate(options[:ASK_MAX_OPTIONS]):
                label = self._opt_label(q, oi)
                desc = str(opt.get("description") or "") if isinstance(opt, dict) else ""
                lines.append(f"  • {label}{' — ' + desc if desc else ''}")
            if q.get("multiSelect") is True:
                lines.append("  (multi-select: toggle, then Submit)")
        return "\n".join(lines)

    def _ask_keyboard(self, ask: PendingAsk) -> list[list[dict]]:
        rows: list[list[dict]] = []
        many = len(ask.questions) > 1
        for qi, q in enumerate(ask.questions):
            if qi in ask.answers:
                continue  # answered: its buttons disappear on refresh
            multi = q.get("multiSelect") is True
            toggled = ask.selections.get(qi, set())
            options = q.get("options") or []
            for oi in range(min(len(options), ASK_MAX_OPTIONS)):
                label = self._opt_label(q, oi)[:32] or f"option {oi + 1}"
                mark = "☑ " if multi and oi in toggled else ""
                prefix = f"{qi + 1}) " if many else ""
                rows.append(
                    [{"text": f"{mark}{prefix}{label}",
                      "callback_data": f"q:{ask.ask_id}:{qi}:{oi}"}]
                )
            if multi:
                label = f"Submit {qi + 1}" if many else "Submit"
                rows.append(
                    [{"text": label, "callback_data": f"q:{ask.ask_id}:s:{qi}"}]
                )
        return rows

    async def _broadcast_ask(self, ask: PendingAsk) -> bool:
        keyboard = self._ask_keyboard(ask)
        for chat_id in self.cfg.telegram_chats:
            if self.notifier.muted(chat_id):
                continue
            mid = await self._send(chat_id, ask.text, keyboard)
            if mid is not None:
                ask.messages.append((chat_id, mid))
        return bool(ask.messages)

    async def _ask_callback(self, chat_id: int, cq_id: str, data: str) -> None:
        parts = data.split(":")
        if len(parts) != 4:
            await self._answer_cq(cq_id)
            return
        ask = self._asks.get(parts[1])
        if ask is None or ask.future.done():
            await self._answer_cq(cq_id, "expired")
            return
        if parts[2] == "s":  # multi-select submit
            try:
                qi = int(parts[3])
            except ValueError:
                await self._answer_cq(cq_id)
                return
            if not (0 <= qi < len(ask.questions)) or qi in ask.answers:
                await self._answer_cq(cq_id)
                return
            toggled = sorted(ask.selections.get(qi, set()))
            if not toggled:
                await self._answer_cq(cq_id, "pick at least one option first")
                return
            ask.answers[qi] = [self._opt_label(ask.questions[qi], oi) for oi in toggled]
        else:
            try:
                qi, oi = int(parts[2]), int(parts[3])
            except ValueError:
                await self._answer_cq(cq_id)
                return
            if not (0 <= qi < len(ask.questions)) or qi in ask.answers:
                await self._answer_cq(cq_id)
                return
            q = ask.questions[qi]
            if not self._opt_label(q, oi):
                await self._answer_cq(cq_id)
                return
            if q.get("multiSelect") is True:
                toggled = ask.selections.setdefault(qi, set())
                toggled.symmetric_difference_update({oi})
                await self._answer_cq(cq_id)
                await self._refresh_ask_messages(ask)
                return
            ask.answers[qi] = self._opt_label(q, oi)
        await self._answer_cq(cq_id, "noted")
        if len(ask.answers) == len(ask.questions):
            answers = {
                self._q_text(q): ask.answers[qi] for qi, q in enumerate(ask.questions)
            }
            append_audit(
                self.cfg.home,
                host=self.cfg.host_id,
                agent=ask.agent,
                sid=ask.sid,
                tool="AskUserQuestion",
                hint=self._q_text(ask.questions[0])[:80],
                decision="answer",
                source=SOURCE_TELEGRAM,
                by=str(chat_id),
            )
            log.info(
                "telegram: ask %r answered from chat %s", ask.ask_id, chat_id
            )
            ask.future.set_result(answers)
            await self._edit_ask_messages(
                ask, f"{GLYPH_DONE} answered via Telegram"
            )
        else:
            await self._refresh_ask_messages(ask)

    async def _refresh_ask_messages(self, ask: PendingAsk) -> None:
        keyboard = self._ask_keyboard(ask)
        for chat_id, message_id in ask.messages:
            await self._edit(chat_id, message_id, ask.text, keyboard)

    async def _edit_ask_messages(self, ask: PendingAsk, note: str) -> None:
        for chat_id, message_id in ask.messages:
            await self._edit(chat_id, message_id, f"{ask.text}\n{note}")

    # ---- outbound helpers (retry-once, quiet) ----

    async def _send(self, chat_id: int, text: str, keyboard: Optional[list] = None):
        payload: dict = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self.client.acall("sendMessage", payload)
        if isinstance(result, dict):
            return result.get("message_id")
        return None

    async def _edit(
        self, chat_id: int, message_id: int, text: str, keyboard: Optional[list] = None
    ) -> None:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        await self.client.acall("editMessageText", payload, retries=0)

    async def _answer_cq(self, cq_id: str, text: str = "") -> None:
        if not cq_id:
            return
        payload: dict = {"callback_query_id": cq_id}
        if text:
            payload["text"] = text
        await self.client.acall("answerCallbackQuery", payload, retries=0)
