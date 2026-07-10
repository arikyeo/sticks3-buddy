"""Yield-when-idle: idle timer, instant wake on activity, no yield while busy."""

import asyncio
import json
import time

import pytest

from buddy_bridge.ble.link import BACKOFF_BASE_SECS, SLOW_RETRY_SECS, LinkManager
from buddy_bridge.cards import Card
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.registry import WAIT


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0
        self.drops = 0
        self.wakes = 0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def drop_connection(self):
        self.drops += 1
        self._connected = False

    def wake(self):
        self.wakes += 1

    async def set_mode(self, mode):
        self.mode = mode

    async def stop(self):
        pass


@pytest.fixture
def daemon(home):
    cfg = load_config(home)
    cfg.idle_yield_min = 1
    d = Daemon(cfg)
    d.link = FakeLink(connected=True)
    return d


def _age_idle(daemon, secs=61.0):
    daemon._idle_since = time.monotonic() - secs


async def test_idle_timer_starts_then_yields(daemon):
    await daemon._maybe_idle_yield()
    assert daemon._idle_since > 0 and not daemon.yielding  # clock armed
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert daemon.yielding
    assert daemon.link.drops == 1
    assert daemon.link.backoff_floor == SLOW_RETRY_SECS
    # a final clean snapshot went out before the disconnect
    snap = json.loads(daemon.link.sent[-1])
    assert "total" in snap and "prompt" not in snap


async def test_no_yield_when_disabled(daemon):
    daemon.cfg.idle_yield_min = 0
    _age_idle(daemon, 10_000)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon.link.drops == 0


async def test_no_yield_outside_exclusive(daemon):
    daemon.cfg.ble_mode = "ondemand"
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon.link.drops == 0


async def test_no_yield_while_disconnected(daemon):
    daemon.link = FakeLink(connected=False)
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon._idle_since == 0.0


async def test_no_yield_while_session_waiting(daemon):
    daemon.registry.set_state("claude", "s1", WAIT)
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon._idle_since == 0.0  # clock reset


async def test_no_yield_with_pending_prompt(daemon):
    daemon.router.create("claude", "s1", "toolu_1", "Bash: make deploy")
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon.link.drops == 0


async def test_no_yield_with_queued_card(daemon):
    daemon.cards.queue.append(Card(kind="gh", title="PR #1"))
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert not daemon.yielding and daemon.link.drops == 0


async def test_wake_on_hook_event(daemon):
    daemon.yielding = True
    daemon.link.backoff_floor = SLOW_RETRY_SECS
    daemon.handle_hook_mirror(
        {"event": "hook", "agent": "claude", "name": "UserPromptSubmit", "session_id": "s1"}
    )
    assert not daemon.yielding
    assert daemon.link.backoff_floor == 0.0
    assert daemon.link.wakes == 1


async def test_wake_on_permission_request(daemon):
    daemon.yielding = True
    daemon.link.backoff_floor = SLOW_RETRY_SECS
    # safe tool: gate answers "ask" immediately, but the wake fired first
    resp = await daemon.handle_permission_request(
        {"event": "permission_request", "agent": "claude", "session_id": "s1",
         "tool_name": "TodoWrite", "tool_use_id": "t1", "tool_input": {}}
    )
    assert resp == {"decision": "ask"}
    assert not daemon.yielding and daemon.link.wakes == 1


async def test_wake_on_card_submit(daemon):
    daemon.yielding = True
    assert daemon.submit_card(Card(kind="gh", title="PR #2"))
    assert not daemon.yielding and daemon.link.wakes == 1
    # a deduped (suppressed) card is not activity
    daemon.yielding = True
    assert not daemon.submit_card(Card(kind="gh", title="PR #2"))
    assert daemon.yielding and daemon.link.wakes == 1


async def test_wake_noop_when_not_yielding(daemon):
    daemon.wake_from_yield()
    assert daemon.link.wakes == 0


async def test_reconnect_exits_yield_and_restarts_clock(daemon):
    daemon.yielding = True
    daemon.link.backoff_floor = SLOW_RETRY_SECS
    daemon._idle_since = 123.0
    await daemon._on_ble_connect()
    assert not daemon.yielding
    assert daemon.link.backoff_floor == 0.0
    assert daemon._idle_since == 0.0


async def test_mode_switch_clears_yield(daemon):
    daemon.yielding = True
    daemon.link.backoff_floor = SLOW_RETRY_SECS
    daemon._ble_task = asyncio.create_task(asyncio.sleep(3600))  # placate _ensure_ble_task
    try:
        await daemon._apply_mode("ondemand")
        assert not daemon.yielding and daemon.link.backoff_floor == 0.0
    finally:
        daemon._ble_task.cancel()


async def test_status_shows_yield_state(daemon):
    payload = daemon.status_payload()
    assert payload["ble_yielding"] is False
    assert payload["idle_yield_min"] == 1
    daemon.yielding = True
    assert daemon.status_payload()["ble_yielding"] is True


async def test_second_yield_call_is_noop(daemon):
    _age_idle(daemon)
    await daemon._maybe_idle_yield()
    assert daemon.yielding and daemon.link.drops == 1
    await daemon._maybe_idle_yield()  # already yielding: no second drop
    assert daemon.link.drops == 1


# ---- LinkManager wake plumbing ----


async def _noop_line(obj):
    pass


async def test_link_wake_interrupts_backoff_sleep():
    link = LinkManager(_noop_line, mode="exclusive")
    task = asyncio.create_task(link._sleep(30.0))
    await asyncio.sleep(0.05)
    assert not task.done()
    link.wake()
    await asyncio.wait_for(task, 1.0)  # returned immediately, not after 30s


async def test_link_wake_resets_backoff():
    link = LinkManager(_noop_line, mode="exclusive")
    link._backoff = 60.0
    link.wake()
    assert link._backoff == BACKOFF_BASE_SECS


async def test_link_stop_still_interrupts_sleep():
    link = LinkManager(_noop_line, mode="exclusive")
    task = asyncio.create_task(link._sleep(30.0))
    await asyncio.sleep(0.05)
    await link.stop()
    await asyncio.wait_for(task, 1.0)
