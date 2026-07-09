"""Daemon-level gate flow: matcher routing, device round-trip, audit trail."""

import asyncio
import json

import pytest

from buddy_bridge.audit import read_audit
from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon
from buddy_bridge.matchers import Matchers


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def ensure_connected(self, timeout):
        return self._connected

    async def stop(self):
        pass


@pytest.fixture
def daemon(home):
    cfg = load_config(home)
    cfg.claude_decision = 0.15
    d = Daemon(cfg)
    d.link = FakeLink(connected=True)
    # NB: always_ask outranks auto_allow, so keep the patterns disjoint here
    d.matchers = Matchers(
        auto_allow=("Bash:git status*",),
        always_ask=("Bash:make *",),
    )
    return d


def _req(tool="Bash", command="make deploy", sid="s1", tuid="toolu_777"):
    return {
        "event": "permission_request",
        "agent": "claude",
        "session_id": sid,
        "tool_name": tool,
        "tool_use_id": tuid,
        "tool_input": {"command": command} if command else {},
    }


async def test_safe_tool_skipped_no_audit(daemon, home):
    resp = await daemon.handle_permission_request(_req(tool="TodoWrite", command=""))
    assert resp == {"decision": "ask"}
    assert read_audit(home) == []


async def test_auto_allow_instant_and_audited(daemon, home):
    resp = await daemon.handle_permission_request(_req(command="git status --short"))
    assert resp == {"decision": "allow"}
    (record,) = read_audit(home)
    assert record["decision"] == "allow"
    assert record["source"] == "auto_allow"
    assert record["tool"] == "Bash"
    assert record["hint"] == "Bash: git status --short"
    assert record["agent"] == "claude"
    assert record["sid"] == "s1"
    assert record["host"] == daemon.cfg.host_id


async def test_default_unmatched_asks_immediately(daemon, home):
    daemon.matchers = Matchers()  # nothing configured -> everything default
    resp = await daemon.handle_permission_request(_req(command="ls"))
    assert resp == {"decision": "ask"}
    assert read_audit(home) == []
    assert daemon.router.pending_count() == 0


async def test_device_decides_deny(daemon, home):
    task = asyncio.create_task(daemon.handle_permission_request(_req(command="make deploy")))
    await asyncio.sleep(0.02)  # let it register + push the prompt snapshot
    assert daemon.router.pending_count() == 1
    assert daemon.pending_prompt() == "Bash: make deploy"
    snap = json.loads(daemon.link.sent[-1])
    assert snap["prompt"] == "Bash: make deploy"

    await daemon.handle_device_permission(
        {"cmd": "permission", "id": "toolu_777", "decision": "deny"}
    )
    resp = await task
    assert resp == {"decision": "deny"}
    (record,) = read_audit(home)
    assert record["decision"] == "deny"
    assert record["source"] == "device"
    # prompt cleared from the follow-up snapshot
    assert "prompt" not in json.loads(daemon.link.sent[-1])


async def test_device_timeout_falls_back_to_ask(daemon, home):
    resp = await daemon.handle_permission_request(_req(command="make deploy"))
    assert resp == {"decision": "ask"}
    (record,) = read_audit(home)
    assert record["decision"] == "ask"
    assert record["source"] == "timeout_fallback"


async def test_ble_disconnected_asks_daemon_down(daemon, home):
    daemon.link = FakeLink(connected=False)
    resp = await daemon.handle_permission_request(_req(command="make deploy"))
    assert resp == {"decision": "ask"}
    (record,) = read_audit(home)
    assert record["source"] == "daemon_down"


async def test_ble_mode_off_asks_daemon_down(daemon, home):
    daemon.cfg.ble_mode = "off"
    resp = await daemon.handle_permission_request(_req(command="make deploy"))
    assert resp == {"decision": "ask"}
    (record,) = read_audit(home)
    assert record["source"] == "daemon_down"


async def test_session_end_cancels_pending(daemon, home):
    task = asyncio.create_task(daemon.handle_permission_request(_req(command="make deploy")))
    await asyncio.sleep(0.02)
    daemon.handle_hook_mirror(
        {"event": "hook", "agent": "claude", "name": "SessionEnd", "session_id": "s1"}
    )
    resp = await task
    assert resp == {"decision": "ask"}


async def test_stale_device_decision_ignored(daemon):
    await daemon.handle_device_permission({"cmd": "permission", "id": "ghost", "decision": "allow"})
    assert daemon.router.pending_count() == 0
