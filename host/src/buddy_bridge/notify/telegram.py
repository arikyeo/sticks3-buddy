"""Telegram push notifications: the "virtual buddy" outbound side.

A TelegramNotifier mirrors the same daemon events the physical stick sees
(permission prompts, attention/done state changes, info cards, federated
peers' prompts) to a set of allowlisted Telegram chats. Transport is the
Bot HTTP API over stdlib urllib run in a worker thread (asyncio.to_thread)
— no new dependency, and the event loop never blocks on the network.

Delivery contract (mirrors cards.py's spirit):
  * best-effort: one retry per send, then drop quietly. A Telegram outage
    must never block, crash, or even slow the daemon.
  * dedupe: a (host, sid, content) push repeats only after DEDUPE_TTL_SECS.
  * per-chat rate limit: at most one message per SEND_GAP_SECS per chat
    (Telegram's own per-chat ceiling is ~1 msg/s), bounded queue between.
  * mute: per-chat suppression (/mute on the bot side) drops pushes only;
    command replies and interactive asks are the bot's business.

SECRET HYGIENE: the bot token is FULL control authority over the bot and
chat messages carry prompt/command text. Neither the token nor any message
content is ever logged — failures log only the API method name, the
exception class, and/or the HTTP status code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

API_TIMEOUT_SECS = 15.0
RETRY_DELAY_SECS = 1.0
DEDUPE_TTL_SECS = 300.0
SEND_GAP_SECS = 1.1
QUEUE_MAX = 32
MUTE_DEFAULT_MIN = 60

EVENT_PROMPT = "prompt"
EVENT_ATTENTION = "attention"
EVENT_DONE = "done"
EVENT_CARD = "card"
EVENT_UPDATE = "update"
EVENTS = (EVENT_PROMPT, EVENT_ATTENTION, EVENT_DONE, EVENT_CARD, EVENT_UPDATE)

GLYPH_PROMPT = "⏳"  # hourglass
GLYPH_RUN = "▶"  # play
GLYPH_IDLE = "·"  # middle dot
GLYPH_DONE = "✓"  # check
GLYPH_CARD = "\U0001f514"  # bell
GLYPH_ASK = "❓"  # question mark
STATE_GLYPH = {"wait": GLYPH_PROMPT, "run": GLYPH_RUN, "idle": GLYPH_IDLE}
AGENT_LETTER = {"claude": "C", "codex": "X"}


def fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def decision_keyboard(wire_id: str) -> list[list[dict]]:
    """Inline [Allow once][Deny] row; callback_data fits Telegram's 64-byte
    cap for every wire id the router can mint (<= 39 chars)."""
    return [
        [
            {"text": "✅ Allow once", "callback_data": f"d:{wire_id}:a"},
            {"text": "❌ Deny", "callback_data": f"d:{wire_id}:d"},
        ]
    ]


class TelegramClient:
    """Minimal Bot API caller. Sync on purpose — run it via asyncio.to_thread
    (`acall`) from daemon code. Never raises; failure = None."""

    def __init__(self, token: str, timeout: float = API_TIMEOUT_SECS) -> None:
        self._token = token
        self.timeout = timeout

    def call(self, method: str, payload: dict, timeout: Optional[float] = None) -> Any:
        """One API call. Returns the decoded ``result`` or None on any
        failure. Logs never include the URL (token!), payload, or body."""
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            log.debug("telegram: %s failed (http %s)", method, exc.code)
            return None
        except Exception as exc:  # noqa: BLE001 — net errors must stay quiet
            log.debug("telegram: %s failed (%s)", method, type(exc).__name__)
            return None
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = data.get("error_code") if isinstance(data, dict) else "?"
            log.debug("telegram: %s rejected (code=%s)", method, code)
            return None
        return data.get("result")

    async def acall(
        self,
        method: str,
        payload: dict,
        *,
        timeout: Optional[float] = None,
        retries: int = 1,
    ) -> Any:
        """to_thread wrapper with retry-once-then-quiet semantics."""
        for attempt in range(retries + 1):
            result = await asyncio.to_thread(self.call, method, payload, timeout)
            if result is not None:
                return result
            if attempt < retries:
                await asyncio.sleep(RETRY_DELAY_SECS)
        return None


@dataclass
class _Push:
    event: str
    text: str
    keyboard: Optional[list] = None
    # (chat_id, message_id) tuples appended as sends succeed — lets the bot
    # edit the message later (e.g. to show a decision's resolution).
    sent: list = field(default_factory=list)


class TelegramNotifier:
    """Event fan-out -> formatted pushes -> bounded queue -> Bot API."""

    def __init__(
        self,
        cfg,
        registry=None,
        client: Optional[TelegramClient] = None,
        *,
        send_gap: float = SEND_GAP_SECS,
        dedupe_ttl: float = DEDUPE_TTL_SECS,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.client = client or TelegramClient(cfg.telegram_token)
        self._send_gap = send_gap
        self._dedupe_ttl = dedupe_ttl
        self._now = now_fn
        self._queue: deque[_Push] = deque()
        self._seen: dict[tuple, float] = {}
        self._muted: dict[int, float] = {}  # chat id -> muted-until (monotonic)
        self._last_send: dict[int, float] = {}
        self._wake = asyncio.Event()
        self._stopped = False

    # ---- event entry points (sync, cheap, never raise) ----

    def _title_for(self, agent: str, sid: str) -> str:
        if self.registry is not None:
            session = self.registry.get(agent, sid)
            if session is not None and session.title:
                return session.title
        return sid[:8] or "?"

    def _host(self) -> str:
        return self.cfg.host_name or self.cfg.host_id or "host"

    def push_prompt(self, pending, *, title: str = "") -> bool:
        """A local pending permission decision was created."""
        title = title or self._title_for(pending.agent, pending.sid)
        text = (
            f"{GLYPH_PROMPT} {self._host()} · {pending.agent}/{title} "
            f"wants: {pending.tool or '?'}\n  {pending.detail or pending.hint}"
        )
        keyboard = decision_keyboard(pending.wire_id) if self.cfg.telegram_decisions else None
        return self._push(
            EVENT_PROMPT, (self._host(), pending.sid, text), text, keyboard=keyboard
        )

    def push_remote_prompts(self, prompts: list[dict]) -> int:
        """Federated peers' live prompts (the merged holder view). Deduped
        per (peer, prompt id, hint) so the steady state_sync stream stays
        quiet; a peer's NEW prompt pushes once."""
        pushed = 0
        for prompt in prompts:
            name = str(prompt.get("name") or "peer")
            hint = str(prompt.get("hint") or "")
            text = (
                f"{GLYPH_PROMPT} {name} · wants: {prompt.get('tool') or '?'}"
                f"\n  {hint}"
            )
            keyboard = None
            if self.cfg.telegram_decisions and prompt.get("id"):
                keyboard = decision_keyboard(str(prompt["id"]))
            if self._push(
                EVENT_PROMPT, (name, str(prompt.get("id") or ""), hint), text,
                keyboard=keyboard,
            ):
                pushed += 1
        return pushed

    def push_hook_event(self, msg: dict) -> bool:
        """Mirrored agent hook events -> attention (Notification) and done
        (Stop) pushes. Everything else is ignored here."""
        name = msg.get("name")
        agent = str(msg.get("agent") or "")
        sid = str(msg.get("session_id") or "")
        if name == "Notification":
            message = str(msg.get("message") or "").strip()
            if not message:
                return False
            text = (
                f"{GLYPH_PROMPT} {self._host()} · {agent}/{self._title_for(agent, sid)} "
                f"needs you\n  {message}"
            )
            return self._push(EVENT_ATTENTION, (self._host(), sid, message), text)
        if name == "Stop":
            title = self._title_for(agent, sid)
            last = ""
            if self.registry is not None:
                session = self.registry.get(agent, sid)
                last = session.last_line if session is not None else ""
            text = f"{GLYPH_DONE} {self._host()} · {agent}/{title} done"
            if last:
                text += f"\n  {last}"
            return self._push(EVENT_DONE, (self._host(), sid, "done:" + last), text)
        return False

    def push_card(self, card) -> bool:
        """Info cards; kind "update" rides the separate update event."""
        event = EVENT_UPDATE if card.kind == "update" else EVENT_CARD
        text = f"{GLYPH_CARD} {self._host()} · [{card.kind}] {card.title}"
        if card.body:
            text += f"\n  {card.body}"
        return self._push(event, (self._host(), card.kind, card.title, card.body), text)

    def push_ask(self, msg: dict) -> bool:
        """Read-only AskUserQuestion display (answer_asks off, or no bot):
        the question is shown, the answer still happens at the terminal."""
        agent = str(msg.get("agent") or "claude")
        sid = str(msg.get("session_id") or "")
        questions = msg.get("questions")
        if not isinstance(questions, list) or not questions:
            return False
        lines = [
            f"{GLYPH_ASK} {self._host()} · {agent}/{self._title_for(agent, sid)} asks:"
        ]
        for q in questions[:4]:
            if not isinstance(q, dict):
                continue
            header = str(q.get("header") or "").strip()
            qtext = str(q.get("question") or q.get("text") or "").strip()
            lines.append(f"  {header + ': ' if header else ''}{qtext}")
            options = q.get("options")
            if isinstance(options, list):
                for opt in options[:4]:
                    label = opt.get("label") if isinstance(opt, dict) else opt
                    if label:
                        lines.append(f"    • {label}")
        lines.append("(answer on your machine)")
        text = "\n".join(lines)
        key = (self._host(), sid, str(msg.get("tool_use_id") or text))
        return self._push(EVENT_PROMPT, key, text)

    # ---- queue + filters ----

    def _push(
        self, event: str, key: tuple, text: str, *, keyboard: Optional[list] = None
    ) -> bool:
        if event not in self.cfg.telegram_events:
            return False
        now = self._now()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._dedupe_ttl}
        dedupe_key = (event, *key)
        if dedupe_key in self._seen:
            return False
        self._seen[dedupe_key] = now
        if len(self._queue) >= QUEUE_MAX:
            self._queue.popleft()  # oldest news is the least useful
            log.debug("telegram: push queue full, dropped the oldest entry")
        self._queue.append(_Push(event=event, text=text, keyboard=keyboard))
        self._wake.set()
        return True

    # ---- mute ----

    def mute(self, chat_id: int, minutes: int = MUTE_DEFAULT_MIN) -> None:
        self._muted[chat_id] = self._now() + max(1, int(minutes)) * 60.0
        log.info("telegram: chat %s muted for %dmin", chat_id, minutes)

    def unmute(self, chat_id: int) -> None:
        self._muted.pop(chat_id, None)
        log.info("telegram: chat %s unmuted", chat_id)

    def muted(self, chat_id: int) -> bool:
        until = self._muted.get(chat_id)
        if until is None:
            return False
        if self._now() >= until:
            self._muted.pop(chat_id, None)
            return False
        return True

    def mute_remaining_min(self, chat_id: int) -> int:
        until = self._muted.get(chat_id)
        if until is None:
            return 0
        return max(0, int((until - self._now()) / 60.0 + 0.5))

    # ---- delivery worker ----

    async def run(self) -> None:
        """Drain the queue forever; every failure path is quiet."""
        while not self._stopped:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            push = self._queue.popleft()
            try:
                await self._deliver(push)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("telegram: delivery failed")

    async def _deliver(self, push: _Push) -> None:
        for chat_id in self.cfg.telegram_chats:
            if self.muted(chat_id):
                continue
            gap = self._send_gap - (self._now() - self._last_send.get(chat_id, -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            payload: dict = {"chat_id": chat_id, "text": push.text}
            if push.keyboard:
                payload["reply_markup"] = {"inline_keyboard": push.keyboard}
            result = await self.client.acall("sendMessage", payload)
            self._last_send[chat_id] = self._now()
            if isinstance(result, dict) and result.get("message_id") is not None:
                push.sent.append((chat_id, result["message_id"]))

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def status(self) -> dict:
        """Daemon status payload fragment. NEVER includes the token."""
        return {
            "active": True,
            "chats": len(self.cfg.telegram_chats),
            "events": list(self.cfg.telegram_events),
            "queued": len(self._queue),
            "muted_chats": [c for c in self.cfg.telegram_chats if self.muted(c)],
            "decisions": bool(self.cfg.telegram_decisions),
            "answer_asks": bool(self.cfg.telegram_answer_asks),
        }
