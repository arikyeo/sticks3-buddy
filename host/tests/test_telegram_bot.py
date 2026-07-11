"""TelegramBot: long-poll dispatch, merged /sessions + /pending, decision
callbacks through the DecisionRouter, AskUserQuestion answering, allowlist,
rate limiting. All Telegram I/O is a fake client — NO real network."""

import asyncio
import logging

import pytest

from buddy_bridge import notify
from buddy_bridge.audit import read_audit
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.matchers import Matchers
from buddy_bridge.notify.telegram import TelegramNotifier
from buddy_bridge.notify.telegram_bot import TelegramBot

TOKEN = "999:BOT-SECRET"
CHAT = 42


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


class FakeRelay:
    def __init__(self):
        self.decisions = []

    async def send_decision(self, peer_id, orig_id, decision):
        self.decisions.append((peer_id, orig_id, decision))
        return True

    def holder_target(self):
        return None


class FakeBotClient:
    """Scripted getUpdates results; records every call."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list = []  # popped per getUpdates; None simulates net error
        self._msg_id = 100

    async def acall(self, method, payload, *, timeout=None, retries=1):
        self.calls.append((method, payload))
        if method == "getUpdates":
            if self.updates:
                return self.updates.pop(0)
            # an empty long-poll blocks server-side in production; simulate
            # the wait so a test-driven run() loop can't busy-spin the loop
            await asyncio.sleep(0.01)
            return []
        if method == "sendMessage":
            self._msg_id += 1
            return {"message_id": self._msg_id}
        return True

    def sends(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def edits(self):
        return [p for m, p in self.calls if m == "editMessageText"]


@pytest.fixture
def rig(home):
    cfg = load_config(home)
    cfg.host_name = "ARIK-PC"
    cfg.telegram_enabled = True
    cfg.telegram_token = TOKEN
    cfg.telegram_chats = (CHAT,)
    cfg.telegram_events = ("prompt",)
    cfg.telegram_decisions = True
    cfg.telegram_answer_asks = True
    cfg.claude_decision = 0.5
    daemon = Daemon(cfg)
    daemon.link = FakeLink()
    daemon.matchers = Matchers(always_ask=("Bash:*",))
    client = FakeBotClient()
    notifier = TelegramNotifier(cfg, daemon.registry, client=client, send_gap=0.0)
    bot = TelegramBot(cfg, daemon, notifier, client=client)
    bot._primed = True  # most tests skip the backlog-drop handshake
    return daemon, bot, client, cfg


def _msg(text, chat=CHAT, uid=None):
    upd = {"message": {"chat": {"id": chat}, "text": text, "from": {"id": chat}}}
    if uid is not None:
        upd["update_id"] = uid
    return upd


def _cb(data, chat=CHAT, cq="cb1", message_id=500, text="orig"):
    return {
        "callback_query": {
            "id": cq,
            "data": data,
            "from": {"id": chat},
            "message": {"chat": {"id": chat}, "message_id": message_id, "text": text},
        }
    }


def _fed_join(daemon, host="peerA", name="NUC", prompt=None, sessions=()):
    daemon.relay = FakeRelay()
    payload = {"name": name, "sessions": list(sessions)}
    if prompt is not None:
        payload["prompt"] = prompt
    assert daemon.federation.update(host, payload) is True


# ---- commands ----


async def test_sessions_merged_view(rig):
    daemon, bot, client, _ = rig
    daemon.registry.set_state("claude", "s1", "wait", "C:/work/repo-x")
    daemon.registry.note_activity("claude", "s1", "needs a decision")
    daemon.registry.add_tokens("claude", "s1", 12300)
    _fed_join(
        daemon,
        sessions=[{"sid": "s9", "agent": "codex", "title": "api", "state": "run",
                   "tok": 500, "last": "building"}],
    )
    await bot._on_update(_msg("/sessions"))
    (payload,) = client.sends()
    text = payload["text"]
    assert "⏳ C [ARIK-PC] repo-x · 12.3kt" in text
    assert "needs a decision" in text
    assert "▶ X [NUC] api" in text  # remote title carries the peer name
    assert payload["chat_id"] == CHAT


async def test_status_header(rig):
    daemon, bot, client, _ = rig
    daemon.registry.set_state("claude", "s1", "run", "C:/w/x")
    await bot._on_update(_msg("/status"))
    text = client.sends()[0]["text"]
    assert text.startswith("ARIK-PC · 1 session(s): 1 running, 0 waiting · stick ✓")
    assert "1 running" in text


async def test_pending_lists_buttons_and_plain_waits(rig):
    daemon, bot, client, _ = rig
    # actionable pending decision
    task = asyncio.create_task(
        daemon.handle_permission_request({
            "event": "permission_request", "agent": "claude", "session_id": "s1",
            "tool_name": "Bash", "tool_use_id": "toolu_777",
            "tool_input": {"command": "git push"},
        })
    )
    await asyncio.sleep(0.02)
    # a waiting session with no wire prompt
    daemon.registry.set_state("claude", "s2", "wait", "C:/w/other")
    # a federated peer's prompt
    _fed_join(daemon, prompt={"id": "p1", "tool": "Bash", "hint": "rm -rf", "sid": "s9", "qn": 0})
    await bot._on_update(_msg("/pending"))
    sends = client.sends()
    texts = [p["text"] for p in sends]
    assert any("wants: Bash" in t and "git push" in t for t in texts)
    assert any("NUC" in t and "rm -rf" in t for t in texts)
    assert any("other" in t and "(no remote action)" in t for t in texts)
    with_buttons = [p for p in sends if "reply_markup" in p]
    datas = [
        b["callback_data"]
        for p in with_buttons
        for row in p["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "d:toolu_777:a" in datas and "d:toolu_777:d" in datas
    assert any(d.startswith("d:r0.") for d in datas)  # namespaced remote id
    await daemon.handle_device_permission(
        {"cmd": "permission", "id": "toolu_777", "decision": "deny"}
    )
    await task


async def test_pending_empty(rig):
    _, bot, client, _ = rig
    await bot._on_update(_msg("/pending"))
    assert "nothing waiting" in client.sends()[0]["text"]


async def test_pending_readonly_note_when_decisions_off(rig):
    daemon, bot, client, cfg = rig
    cfg.telegram_decisions = False
    daemon.registry.set_state("claude", "s2", "wait", "C:/w/other")
    await bot._on_update(_msg("/pending"))
    texts = [p["text"] for p in client.sends()]
    assert any("allow_decisions" in t for t in texts)
    assert all("reply_markup" not in p for p in client.sends())


async def test_help_mute_unmute(rig):
    _, bot, client, _ = rig
    await bot._on_update(_msg("/help"))
    assert "/pending" in client.sends()[0]["text"]
    await bot._on_update(_msg("/mute 15"))
    assert bot.notifier.muted(CHAT) is True
    assert "15min" in client.sends()[-1]["text"]
    await bot._on_update(_msg("/unmute"))
    assert bot.notifier.muted(CHAT) is False


async def test_unknown_command(rig):
    _, bot, client, _ = rig
    await bot._on_update(_msg("/frobnicate"))
    assert "/help" in client.sends()[0]["text"]


# ---- allowlist + rate limit ----


async def test_non_allowlisted_ignored_and_logged_once(rig, caplog):
    _, bot, client, _ = rig
    with caplog.at_level(logging.WARNING, logger="buddy_bridge.notify.telegram_bot"):
        await bot._on_update(_msg("/sessions secret-content", chat=666))
        await bot._on_update(_msg("/pending secret-content", chat=666))
    assert client.sends() == []  # no reply of any kind
    assert caplog.text.count("non-allowlisted chat 666") == 1  # logged once
    assert "secret-content" not in caplog.text  # content never logged


async def test_non_allowlisted_callback_ignored(rig):
    daemon, bot, client, _ = rig
    task = asyncio.create_task(
        daemon.handle_permission_request({
            "event": "permission_request", "agent": "claude", "session_id": "s1",
            "tool_name": "Bash", "tool_use_id": "toolu_1",
            "tool_input": {"command": "x"},
        })
    )
    await asyncio.sleep(0.02)
    await bot._on_update(_cb("d:toolu_1:a", chat=666))
    assert daemon.router.pending_count() == 1  # untouched
    resp = await task  # times out (claude_decision=0.5) and fails open
    assert resp == {"decision": "ask"}


async def test_inbound_rate_limit(rig):
    _, bot, client, _ = rig
    for _ in range(notify.telegram_bot.INBOUND_WINDOW_MAX + 5):
        await bot._on_update(_msg("/help"))
    assert len(client.sends()) == notify.telegram_bot.INBOUND_WINDOW_MAX


# ---- decision callbacks ----


def _gate(daemon, tuid="toolu_777", command="git push origin main"):
    return asyncio.create_task(
        daemon.handle_permission_request({
            "event": "permission_request", "agent": "claude", "session_id": "s1",
            "tool_name": "Bash", "tool_use_id": tuid,
            "tool_input": {"command": command},
        })
    )


async def test_local_decision_allow_resolves_gate_and_audits(rig, home):
    daemon, bot, client, _ = rig
    task = _gate(daemon)
    await asyncio.sleep(0.02)
    await bot._on_update(_cb("d:toolu_777:a"))
    resp = await asyncio.wait_for(task, 1.0)
    assert resp == {"decision": "allow"}
    (record,) = read_audit(home)
    assert record["decision"] == "allow"
    assert record["source"] == "telegram"
    assert record["by"] == str(CHAT)
    assert record["tool"] == "Bash"
    # the pushed message got a resolution edit and lost its keyboard
    (edit,) = client.edits()
    assert edit["message_id"] == 500
    assert "✅ allowed via Telegram" in edit["text"]
    assert "reply_markup" not in edit


async def test_local_decision_deny(rig, home):
    daemon, bot, client, _ = rig
    task = _gate(daemon)
    await asyncio.sleep(0.02)
    await bot._on_update(_cb("d:toolu_777:d"))
    assert (await asyncio.wait_for(task, 1.0)) == {"decision": "deny"}
    (record,) = read_audit(home)
    assert (record["decision"], record["source"], record["by"]) == (
        "deny", "telegram", str(CHAT),
    )


async def test_decision_disabled_flag(rig, home):
    daemon, bot, client, cfg = rig
    cfg.telegram_decisions = False
    task = _gate(daemon)
    await asyncio.sleep(0.02)
    await bot._on_update(_cb("d:toolu_777:a"))
    assert daemon.router.pending_count() == 1
    answers = [p for m, p in client.calls if m == "answerCallbackQuery"]
    assert any("allow_decisions" in (p.get("text") or "") for p in answers)
    daemon.router.resolve("toolu_777", "deny")
    await task


async def test_remote_decision_relays_home_and_audits(rig, home):
    daemon, bot, client, _ = rig
    _fed_join(daemon, prompt={"id": "orig9", "tool": "Bash", "hint": "rm -rf build",
                              "sid": "s9", "qn": 0})
    prompts = daemon.federation.remote_prompts()
    wire_id = prompts[0]["id"]
    assert wire_id.startswith("r0.")
    await bot._on_update(_cb(f"d:{wire_id}:a"))
    await asyncio.sleep(0.02)  # let the spawned relay task run
    assert daemon.relay.decisions == [("peerA", "orig9", "allow")]
    assert daemon.federation.remote_prompts() == []  # dropped from merged view
    (record,) = read_audit(home)
    assert record["source"] == "telegram"
    assert record["by"] == str(CHAT)
    assert record["decision"] == "allow"
    assert record["hint"] == "rm -rf build"


async def test_stale_callback_answers_expired(rig):
    _, bot, client, _ = rig
    await bot._on_update(_cb("d:ghost:a"))
    answers = [p for m, p in client.calls if m == "answerCallbackQuery"]
    assert any("expired" in (p.get("text") or "") for p in answers)
    (edit,) = client.edits()
    assert "⌛ expired" in edit["text"]


# ---- AskUserQuestion answering ----

ASK_MSG = {
    "event": "ask_answer",
    "agent": "claude",
    "session_id": "s1",
    "tool_use_id": "toolu_ask1",
    "questions": [{
        "question": "Which format?",
        "header": "Format",
        "options": [
            {"label": "Summary", "description": "Brief"},
            {"label": "Detailed", "description": "Full"},
        ],
        "multiSelect": False,
    }],
}


async def test_ask_answered_via_callback(rig, home):
    _, bot, client, _ = rig
    task = asyncio.create_task(bot.handle_ask_answer(dict(ASK_MSG)))
    await asyncio.sleep(0.02)
    (send,) = client.sends()
    assert "❓ ARIK-PC · claude/s1 asks:" in send["text"]
    assert "Format: Which format?" in send["text"]
    assert "• Summary — Brief" in send["text"]
    kb = send["reply_markup"]["inline_keyboard"]
    assert kb[0][0] == {"text": "Summary", "callback_data": "q:toolu_ask1:0:0"}
    await bot._on_update(_cb("q:toolu_ask1:0:0"))
    result = await asyncio.wait_for(task, 1.0)
    assert result == {"answers": {"Which format?": "Summary"}}
    (record,) = read_audit(home)
    assert record["tool"] == "AskUserQuestion"
    assert record["decision"] == "answer"
    assert record["source"] == "telegram"
    assert record["by"] == str(CHAT)
    edits = client.edits()
    assert any("answered via Telegram" in e["text"] for e in edits)
    assert bot._asks == {}


async def test_ask_multiselect_toggle_and_submit(rig):
    _, bot, client, _ = rig
    msg = {
        **ASK_MSG,
        "tool_use_id": "toolu_ask2",
        "questions": [{
            "question": "Which sections?",
            "header": "Sections",
            "options": [{"label": "Intro"}, {"label": "Body"}, {"label": "End"}],
            "multiSelect": True,
        }],
    }
    task = asyncio.create_task(bot.handle_ask_answer(msg))
    await asyncio.sleep(0.02)
    # empty submit is refused
    await bot._on_update(_cb("q:toolu_ask2:s:0"))
    answers = [p for m, p in client.calls if m == "answerCallbackQuery"]
    assert any("pick at least one" in (p.get("text") or "") for p in answers)
    # toggle two options (keyboard refresh shows check marks), then submit
    await bot._on_update(_cb("q:toolu_ask2:0:0"))
    await bot._on_update(_cb("q:toolu_ask2:0:2"))
    kb = client.edits()[-1]["reply_markup"]["inline_keyboard"]
    marked = [b["text"] for row in kb for b in row]
    assert "☑ Intro" in marked and "☑ End" in marked and "Body" in marked
    await bot._on_update(_cb("q:toolu_ask2:s:0"))
    result = await asyncio.wait_for(task, 1.0)
    assert result == {"answers": {"Which sections?": ["Intro", "End"]}}


async def test_ask_two_questions_indexed_buttons(rig):
    _, bot, client, _ = rig
    msg = {
        **ASK_MSG,
        "tool_use_id": "toolu_ask3",
        "questions": [
            {"question": "Q one?", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Q two?", "options": [{"label": "C"}]},
        ],
    }
    task = asyncio.create_task(bot.handle_ask_answer(msg))
    await asyncio.sleep(0.02)
    kb = client.sends()[0]["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in kb for b in row]
    assert "1) A" in labels and "2) C" in labels
    await bot._on_update(_cb("q:toolu_ask3:0:1"))
    assert not task.done()  # one question still open
    await bot._on_update(_cb("q:toolu_ask3:1:0"))
    result = await asyncio.wait_for(task, 1.0)
    assert result == {"answers": {"Q one?": "B", "Q two?": "C"}}


async def test_ask_timeout_falls_open(rig):
    _, bot, client, cfg = rig
    cfg.claude_decision = 0.05
    result = await bot.handle_ask_answer(dict(ASK_MSG))
    assert result == {"answers": None}
    edits = client.edits()
    assert any("expired — answer in the terminal" in e["text"] for e in edits)
    assert bot._asks == {}
    # a tap after the timeout answers "expired"
    await bot._on_update(_cb("q:toolu_ask1:0:0"))
    answers = [p for m, p in client.calls if m == "answerCallbackQuery"]
    assert any("expired" in (p.get("text") or "") for p in answers)


async def test_ask_disabled_returns_none_immediately(rig):
    _, bot, client, cfg = rig
    cfg.telegram_answer_asks = False
    result = await bot.handle_ask_answer(dict(ASK_MSG))
    assert result == {"answers": None}
    assert client.sends() == []


async def test_ask_all_chats_muted_fails_open_fast(rig):
    _, bot, client, _ = rig
    bot.notifier.mute(CHAT, minutes=5)
    result = await bot.handle_ask_answer(dict(ASK_MSG))
    assert result == {"answers": None}
    assert client.sends() == []


async def test_ask_malformed_questions_rejected(rig):
    _, bot, client, _ = rig
    for bad in ([], [{"question": "no options"}], "nope", None):
        result = await bot.handle_ask_answer({**ASK_MSG, "questions": bad})
        assert result == {"answers": None}
    assert client.sends() == []


async def test_ask_duplicate_delivery_shares_future(rig):
    _, bot, client, _ = rig
    t1 = asyncio.create_task(bot.handle_ask_answer(dict(ASK_MSG)))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(bot.handle_ask_answer(dict(ASK_MSG)))
    await asyncio.sleep(0.02)
    assert len(client.sends()) == 1  # no second broadcast
    await bot._on_update(_cb("q:toolu_ask1:0:1"))
    r1 = await asyncio.wait_for(t1, 1.0)
    r2 = await asyncio.wait_for(t2, 1.0)
    assert r1 == r2 == {"answers": {"Which format?": "Detailed"}}


# ---- long-poll resilience ----


async def test_poll_primes_by_dropping_backlog(rig):
    _, bot, client, _ = rig
    bot._primed = False
    client.updates = [[{"update_id": 7, "message": {"chat": {"id": CHAT}, "text": "/help"}}]]
    await bot._poll_once()  # priming call: backlog dropped, not dispatched
    assert client.sends() == []
    assert bot._offset == 8
    method, payload = client.calls[0]
    assert method == "getUpdates" and payload["offset"] == -1


async def test_poll_retries_with_backoff_then_recovers(rig, monkeypatch):
    _, bot, client, _ = rig
    monkeypatch.setattr(notify.telegram_bot, "BACKOFF_START_SECS", 0.01)
    bot._backoff = 0.01
    client.updates = [None, None, [_msg("/help", uid=11)["message"] and
                                   {"update_id": 11, "message": _msg("/help")["message"]}]]
    await bot._poll_once()  # error -> backoff grows
    first_backoff = bot._backoff
    await bot._poll_once()  # error again -> grows more
    assert bot._backoff > first_backoff
    await bot._poll_once()  # recovery: dispatched + backoff reset
    assert bot._backoff == 0.01
    assert bot._offset == 12
    assert any("/pending" in p["text"] for p in client.sends())  # /help reply


async def test_poll_survives_handler_crash(rig, monkeypatch):
    daemon, bot, client, _ = rig

    async def boom(message):
        raise RuntimeError("handler crash")

    monkeypatch.setattr(bot, "_on_message", boom)
    client.updates = [[
        {"update_id": 3, "message": {"chat": {"id": CHAT}, "text": "/help"}},
        {"update_id": 4, "message": {"chat": {"id": CHAT}, "text": "/help"}},
    ]]
    await bot._poll_once()  # neither crash escapes; offset still advances
    assert bot._offset == 5


async def test_run_loop_stops_and_never_raises(rig, monkeypatch):
    _, bot, client, _ = rig
    monkeypatch.setattr(notify.telegram_bot, "BACKOFF_START_SECS", 0.01)
    bot._backoff = 0.01
    client.updates = [None]  # one failure, then empty polls
    task = asyncio.create_task(bot.run())
    await asyncio.sleep(0.05)
    bot.stop()
    # unblock the (empty-result) poll loop promptly
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
