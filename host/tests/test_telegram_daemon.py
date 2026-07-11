"""Daemon <-> Telegram wiring: construction gate, event fan-out points,
ask_answer IPC dispatch, status payload. NO real network."""

import asyncio
import logging

import pytest

from buddy_bridge.cards import Card
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.matchers import Matchers

TOKEN = "555:WIRE-SECRET"
CHAT = 77


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def stop(self):
        pass


class FakeClient:
    def __init__(self):
        self.calls = []
        self._msg_id = 0

    async def acall(self, method, payload, *, timeout=None, retries=1):
        self.calls.append((method, payload))
        if method == "getUpdates":
            await asyncio.sleep(0.01)
            return []
        self._msg_id += 1
        return {"message_id": self._msg_id}


def make_cfg(home, **over):
    cfg = load_config(home)
    cfg.host_name = "ARIK-PC"
    cfg.claude_decision = 0.2
    cfg.claude_enabled = True
    cfg.codex_enabled = False
    cfg.telegram_enabled = True
    cfg.telegram_token = TOKEN
    cfg.telegram_chats = (CHAT,)
    cfg.telegram_events = ("prompt", "attention", "done", "card", "update")
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def daemon(home):
    cfg = make_cfg(home)
    d = Daemon(cfg)
    d.link = FakeLink()
    d.matchers = Matchers(always_ask=("Bash:*",))
    assert d.telegram is not None and d.telegram_bot is not None
    fake = FakeClient()
    d.telegram.client = fake
    d.telegram_bot.client = fake
    return d


def queued_texts(daemon):
    return [p.text for p in daemon.telegram._queue]


def test_disabled_config_builds_nothing(home):
    cfg = make_cfg(home, telegram_enabled=False)
    d = Daemon(cfg)
    assert d.telegram is None and d.telegram_bot is None


def test_enabled_but_incomplete_stays_off(home):
    cfg = make_cfg(home, telegram_token="")
    d = Daemon(cfg)
    assert d.telegram is None and d.telegram_bot is None
    cfg = make_cfg(home, telegram_chats=())
    assert Daemon(cfg).telegram is None


async def test_gate_pending_pushes_prompt(daemon):
    task = asyncio.create_task(
        daemon.handle_permission_request({
            "event": "permission_request", "agent": "claude", "session_id": "s1",
            "tool_name": "Bash", "tool_use_id": "toolu_1",
            "tool_input": {"command": "git push origin main"},
        })
    )
    await asyncio.sleep(0.02)
    assert queued_texts(daemon) == [
        "⏳ ARIK-PC · claude/s1 wants: Bash\n  git push origin main"
    ]
    daemon.router.resolve("toolu_1", "deny")
    await task


async def test_hook_mirror_pushes_attention_and_done(daemon):
    daemon.handle_hook_mirror({
        "event": "hook", "agent": "claude", "name": "SessionStart",
        "session_id": "s1", "cwd": "C:/work/repo-x",
    })
    daemon.handle_hook_mirror({
        "event": "hook", "agent": "claude", "name": "Notification",
        "session_id": "s1", "message": "Claude needs your permission",
    })
    daemon.handle_hook_mirror({
        "event": "hook", "agent": "claude", "name": "Stop", "session_id": "s1",
    })
    texts = queued_texts(daemon)
    assert any("needs you" in t and "repo-x" in t for t in texts)
    assert any("done" in t for t in texts)


async def test_submit_card_pushes(daemon):
    assert daemon.submit_card(Card(kind="gh", title="PR #1", body="merged")) is True
    assert any("[gh] PR #1" in t for t in queued_texts(daemon))


async def test_fed_view_change_pushes_remote_prompt(daemon):
    class FakeRelay:
        def holder_target(self):
            return None

        async def send_decision(self, *a):
            return True

    daemon.relay = FakeRelay()
    daemon.federation.update("peerA", {
        "name": "NUC",
        "sessions": [],
        "prompt": {"id": "p9", "tool": "Bash", "hint": "rm -rf", "sid": "s2", "qn": 0},
    })
    await daemon._fed_view_changed()
    assert any("NUC" in t and "rm -rf" in t for t in queued_texts(daemon))


async def test_ask_pending_readonly_push_without_stick(daemon):
    # v1 link, ask cap inactive, no federation holder -> stick path returns
    # early, the Telegram mirror still fires (answer_asks is off).
    await daemon.handle_ask_pending({
        "event": "ask_pending", "agent": "claude", "session_id": "s1",
        "tool_use_id": "toolu_a1",
        "questions": [{
            "question": "Which db?",
            "options": [{"label": "sqlite"}, {"label": "postgres"}],
        }],
    })
    texts = queued_texts(daemon)
    assert any("Which db?" in t and "(answer on your machine)" in t for t in texts)


async def test_ask_pending_no_readonly_push_when_answer_asks(home):
    cfg = make_cfg(home, telegram_answer_asks=True)
    d = Daemon(cfg)
    d.link = FakeLink()
    d.telegram.client = FakeClient()
    d.telegram_bot.client = d.telegram.client
    await d.handle_ask_pending({
        "event": "ask_pending", "agent": "claude", "session_id": "s1",
        "tool_use_id": "toolu_a1",
        "questions": [{"question": "Q?", "options": [{"label": "A"}]}],
    })
    assert queued_texts(d) == []  # the interactive ask_answer path owns it


async def test_ipc_ask_answer_dispatch_roundtrip(home):
    cfg = make_cfg(home, telegram_answer_asks=True)
    d = Daemon(cfg)
    d.link = FakeLink()
    fake = FakeClient()
    d.telegram.client = fake
    d.telegram_bot.client = fake
    task = asyncio.create_task(d._on_ipc({
        "event": "ask_answer", "agent": "claude", "session_id": "s1",
        "tool_use_id": "toolu_q1",
        "questions": [{
            "question": "Which format?",
            "options": [{"label": "Summary"}, {"label": "Detailed"}],
        }],
    }))
    await asyncio.sleep(0.02)
    sends = [p for m, p in fake.calls if m == "sendMessage"]
    assert sends and "Which format?" in sends[0]["text"]
    await d.telegram_bot._on_update({
        "callback_query": {
            "id": "cb1", "data": "q:toolu_q1:0:0", "from": {"id": CHAT},
            "message": {"chat": {"id": CHAT}, "message_id": 1, "text": "x"},
        }
    })
    resp = await asyncio.wait_for(task, 1.0)
    assert resp == {"answers": {"Which format?": "Summary"}}


async def test_ipc_ask_answer_none_when_telegram_off(home):
    cfg = make_cfg(home, telegram_enabled=False)
    d = Daemon(cfg)
    resp = await d._on_ipc({"event": "ask_answer", "questions": []})
    assert resp == {"answers": None}


async def test_status_payload_has_telegram_section(daemon, home):
    payload = daemon.status_payload()
    assert payload["telegram"]["active"] is True
    assert payload["telegram"]["chats"] == 1
    assert TOKEN not in str(payload)
    cfg = make_cfg(home, telegram_enabled=False)
    assert Daemon(cfg).status_payload()["telegram"] == {"active": False}


async def test_start_background_warns_when_incomplete(home, caplog):
    cfg = make_cfg(home, telegram_token="", relay_enabled=False,
                   claude_enabled=False, codex_enabled=False, cards_enabled=False)
    d = Daemon(cfg)
    with caplog.at_level(logging.WARNING, logger="buddy_bridge.daemon"):
        tasks = await d.start_background()
    assert tasks == []
    assert "telegram stays OFF" in caplog.text


async def test_start_background_spawns_telegram_tasks(daemon):
    daemon.cfg.claude_enabled = False
    daemon.cfg.codex_enabled = False
    daemon.cfg.cards_enabled = False
    tasks = await daemon.start_background()
    names = {t.get_name() for t in tasks}
    assert {"telegram-notify", "telegram-bot"} <= names
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
