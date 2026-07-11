"""TelegramNotifier: formatting, dedupe, events filter, rate limit, mute,
delivery worker, and secret redaction. NO real network anywhere."""

import asyncio
import io
import json
import logging
import urllib.error

import pytest

from buddy_bridge import notify
from buddy_bridge.cards import Card, clean_card
from buddy_bridge.config import Config
from buddy_bridge.decision import DecisionRouter
from buddy_bridge.notify.telegram import TelegramClient, TelegramNotifier
from buddy_bridge.registry import SessionRegistry

TOKEN = "123456:SECRET-token-value"


class FakeClient:
    """Records acall()s; scripted failures via fail_first."""

    def __init__(self, fail_first: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.fail_first = fail_first
        self._msg_id = 0

    async def acall(self, method, payload, *, timeout=None, retries=1):
        self.calls.append((method, payload))
        if self.fail_first > 0:
            self.fail_first -= 1
            return None
        self._msg_id += 1
        return {"message_id": self._msg_id}


def make_cfg(home, **over):
    base = dict(
        host_id="h1",
        host_name="ARIK-PC",
        telegram_enabled=True,
        telegram_token=TOKEN,
        telegram_chats=(42,),
        telegram_events=("prompt",),
    )
    base.update(over)
    return Config(home=home, **base)


def make_notifier(home, **over):
    cfg = make_cfg(home, **over)
    registry = SessionRegistry()
    registry.upsert("claude", "s1", "C:/work/repo-x")
    fake = FakeClient()
    return TelegramNotifier(cfg, registry, client=fake, send_gap=0.0), fake, registry


async def _pending(router=None, sid="s1", tuid="toolu_1"):
    router = router or DecisionRouter()
    return router.create(
        "claude", sid, tuid, "Bash: git push origin main",
        tool="Bash", detail="git push origin main",
    )


async def drain(notifier):
    task = asyncio.create_task(notifier.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not notifier._queue:
            break
    notifier.stop()
    await asyncio.wait_for(task, 1.0)


async def test_prompt_format_and_delivery(home):
    notifier, fake, _ = make_notifier(home)
    pending = await _pending()
    assert notifier.push_prompt(pending) is True
    await drain(notifier)
    (method, payload), = fake.calls
    assert method == "sendMessage"
    assert payload["chat_id"] == 42
    assert payload["text"] == (
        "⏳ ARIK-PC · claude/repo-x wants: Bash\n  git push origin main"
    )
    assert "reply_markup" not in payload  # allow_decisions off -> no buttons


async def test_prompt_keyboard_when_decisions_enabled(home):
    notifier, fake, _ = make_notifier(home, telegram_decisions=True)
    pending = await _pending()
    notifier.push_prompt(pending)
    await drain(notifier)
    (_, payload), = fake.calls
    rows = payload["reply_markup"]["inline_keyboard"]
    assert rows == [[
        {"text": "✅ Allow once", "callback_data": "d:toolu_1:a"},
        {"text": "❌ Deny", "callback_data": "d:toolu_1:d"},
    ]]


async def test_dedupe_same_content(home):
    notifier, fake, _ = make_notifier(home)
    router = DecisionRouter()
    assert notifier.push_prompt(await _pending(router, tuid="toolu_1")) is True
    # a NEW pending with the same rendered content within the TTL stays quiet
    assert notifier.push_prompt(await _pending(router, tuid="toolu_2")) is False
    await drain(notifier)
    assert len(fake.calls) == 1


async def test_dedupe_expires(home):
    now = [0.0]
    cfg = make_cfg(home)
    notifier = TelegramNotifier(
        cfg, None, client=FakeClient(), send_gap=0.0, dedupe_ttl=10.0,
        now_fn=lambda: now[0],
    )
    pending = await _pending()
    assert notifier.push_prompt(pending) is True
    now[0] = 11.0
    assert notifier.push_prompt(pending) is True


async def test_events_filter(home):
    notifier, fake, _ = make_notifier(home)  # events = ("prompt",) only
    card = clean_card(Card(kind="gh", title="PR #42 merged", body="repo-x"))
    assert notifier.push_card(card) is False
    assert notifier.push_hook_event(
        {"agent": "claude", "name": "Stop", "session_id": "s1"}
    ) is False
    assert notifier.push_hook_event(
        {"agent": "claude", "name": "Notification", "session_id": "s1",
         "message": "needs permission"}
    ) is False
    await drain(notifier)
    assert fake.calls == []


async def test_card_and_update_events(home):
    notifier, fake, _ = make_notifier(home, telegram_events=("card", "update"))
    assert notifier.push_card(clean_card(Card(kind="gh", title="PR #42", body="b"))) is True
    assert notifier.push_card(clean_card(Card(kind="update", title="fw v9", body=""))) is True
    await drain(notifier)
    texts = [p["text"] for _, p in fake.calls]
    assert texts[0].startswith("\U0001f514 ARIK-PC · [gh] PR #42")
    assert "[update] fw v9" in texts[1]


async def test_attention_and_done(home):
    notifier, fake, registry = make_notifier(
        home, telegram_events=("attention", "done")
    )
    registry.note_activity("claude", "s1", "wrote tests")
    assert notifier.push_hook_event(
        {"agent": "claude", "name": "Notification", "session_id": "s1",
         "message": "Claude needs your permission"}
    ) is True
    assert notifier.push_hook_event(
        {"agent": "claude", "name": "Stop", "session_id": "s1"}
    ) is True
    await drain(notifier)
    texts = [p["text"] for _, p in fake.calls]
    assert texts[0] == (
        "⏳ ARIK-PC · claude/repo-x needs you\n  Claude needs your permission"
    )
    assert texts[1] == "✓ ARIK-PC · claude/repo-x done\n  wrote tests"


async def test_remote_prompts_pushed_and_deduped(home):
    notifier, fake, _ = make_notifier(home, telegram_decisions=True)
    prompts = [{
        "id": "r0.p1", "tool": "Bash", "hint": "rm -rf build",
        "sid": "r0:s1", "qn": 0, "name": "NUC", "first_seen": 1.0,
    }]
    assert notifier.push_remote_prompts(prompts) == 1
    assert notifier.push_remote_prompts(prompts) == 0  # steady stream stays quiet
    await drain(notifier)
    (_, payload), = fake.calls
    assert payload["text"] == "⏳ NUC · wants: Bash\n  rm -rf build"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "d:r0.p1:a"


async def test_ask_readonly_display(home):
    notifier, fake, _ = make_notifier(home)
    ok = notifier.push_ask({
        "agent": "claude", "session_id": "s1", "tool_use_id": "toolu_9",
        "questions": [{
            "question": "Which format?", "header": "Format",
            "options": [{"label": "Summary"}, {"label": "Detailed"}],
        }],
    })
    assert ok is True
    await drain(notifier)
    text = fake.calls[0][1]["text"]
    assert "❓ ARIK-PC · claude/repo-x asks:" in text
    assert "Format: Which format?" in text
    assert "• Summary" in text
    assert "(answer on your machine)" in text


async def test_mute_suppresses_pushes(home):
    notifier, fake, _ = make_notifier(home)
    notifier.mute(42, minutes=5)
    assert notifier.muted(42) is True
    assert notifier.mute_remaining_min(42) == 5
    notifier.push_prompt(await _pending())
    await drain(notifier)
    assert fake.calls == []
    notifier.unmute(42)
    assert notifier.muted(42) is False


async def test_mute_expires(home):
    now = [0.0]
    cfg = make_cfg(home)
    fake = FakeClient()
    notifier = TelegramNotifier(cfg, None, client=fake, send_gap=0.0, now_fn=lambda: now[0])
    notifier.mute(42, minutes=1)
    assert notifier.muted(42) is True
    now[0] = 61.0
    assert notifier.muted(42) is False


async def test_per_chat_rate_limit_gap(home):
    """Second send to the same chat waits out the gap."""
    cfg = make_cfg(home)
    fake = FakeClient()
    notifier = TelegramNotifier(cfg, None, client=fake, send_gap=0.15)
    router = DecisionRouter()
    p1 = router.create("claude", "s1", "t1", "Bash: a", tool="Bash", detail="a")
    p2 = router.create("claude", "s1", "t2", "Bash: b", tool="Bash", detail="b")
    notifier.push_prompt(p1)
    notifier.push_prompt(p2)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await drain(notifier)
    assert len(fake.calls) == 2
    assert loop.time() - start >= 0.14


async def test_queue_bounded(home):
    notifier, _, _ = make_notifier(home)
    router = DecisionRouter()
    for i in range(notify.telegram.QUEUE_MAX + 5):
        pending = router.create("claude", "s1", f"t{i}", f"Bash: c{i}",
                                tool="Bash", detail=f"c{i}")
        notifier.push_prompt(pending)
    assert len(notifier._queue) == notify.telegram.QUEUE_MAX


async def test_retry_once_then_quiet(home, monkeypatch):
    monkeypatch.setattr(notify.telegram, "RETRY_DELAY_SECS", 0.0)
    calls = []

    class FlakyClient(TelegramClient):
        def call(self, method, payload, timeout=None):
            calls.append(method)
            return None  # every attempt fails

    notifier = TelegramNotifier(
        make_cfg(home), None, client=FlakyClient(TOKEN), send_gap=0.0
    )
    notifier.push_prompt(await _pending())
    await drain(notifier)
    assert calls == ["sendMessage", "sendMessage"]  # one retry, then quiet


async def test_status_never_contains_token(home):
    notifier, _, _ = make_notifier(home)
    notifier.mute(42)
    status = notifier.status()
    assert status["active"] is True
    assert status["chats"] == 1
    assert status["muted_chats"] == [42]
    assert TOKEN not in json.dumps(status)


def test_client_redacts_on_failure(home, monkeypatch, caplog):
    """URL (token), payload, and chat content never reach the logs."""
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    client = TelegramClient(TOKEN)
    with caplog.at_level(logging.DEBUG, logger="buddy_bridge.notify.telegram"):
        result = client.call("sendMessage", {"chat_id": 42, "text": "secret prompt text"})
    assert result is None
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text
    assert "secret prompt text" not in caplog.text
    assert "sendMessage" in caplog.text  # method name is the only identifier


def test_client_redacts_http_error(home, monkeypatch, caplog):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401,
            "Unauthorized", {}, io.BytesIO(b"{}"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    client = TelegramClient(TOKEN)
    with caplog.at_level(logging.DEBUG, logger="buddy_bridge.notify.telegram"):
        assert client.call("getMe", {}) is None
    assert TOKEN not in caplog.text
    assert "http 401" in caplog.text


def test_client_rejected_response_redacted(home, monkeypatch, caplog):
    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        assert TOKEN in req.full_url  # token IS in the URL (transport)...
        return FakeResp(b'{"ok":false,"error_code":403,"description":"secret"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = TelegramClient(TOKEN)
    with caplog.at_level(logging.DEBUG, logger="buddy_bridge.notify.telegram"):
        assert client.call("sendMessage", {"text": "hello"}) is None
    assert TOKEN not in caplog.text  # ...but never in the logs
    assert "code=403" in caplog.text


async def test_worker_survives_delivery_exception(home):
    class ExplodingClient:
        def __init__(self):
            self.calls = 0

        async def acall(self, method, payload, *, timeout=None, retries=1):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return {"message_id": 1}

    cfg = make_cfg(home)
    client = ExplodingClient()
    notifier = TelegramNotifier(cfg, None, client=client, send_gap=0.0)
    router = DecisionRouter()
    notifier.push_prompt(router.create("claude", "s1", "t1", "h", tool="Bash", detail="a"))
    notifier.push_prompt(router.create("claude", "s1", "t2", "h", tool="Bash", detail="b"))
    await drain(notifier)
    assert client.calls == 2  # second push still delivered after the crash


@pytest.mark.parametrize("event", ["prompt", "attention", "done", "card", "update"])
def test_known_event_names(event):
    assert event in notify.telegram.EVENTS
