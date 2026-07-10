"""Ask origination: the AskUserQuestion payload travels hook -> daemon ->
{"evt":"ask"} on a v2 stick, and clears via PostToolUse/Stop/etc. Display
only — the hook never blocks and never prints."""

import io
import json

import pytest

from buddy_bridge.config import load_config
from buddy_bridge.daemon import Daemon, convert_ask_questions
from buddy_bridge.hooks import claude_hook
from buddy_bridge.protocol import PROTO_V2, HelloAck

QUESTIONS = [
    {
        "question": "Which approach should we take?",
        "header": "Approach",
        "options": [
            {"label": "Option A", "description": "the safe one"},
            {"label": "Option B", "description": "the fast one"},
        ],
        "multiSelect": False,
    }
]


_DEFAULT = object()


def _pretooluse(tool="AskUserQuestion", tool_input=_DEFAULT):
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "cwd": "C:/proj",
            "tool_name": tool,
            "tool_use_id": "toolu_ask1",
            "tool_input": {"questions": QUESTIONS} if tool_input is _DEFAULT else tool_input,
        }
    )


# ---- hook side ----


def test_hook_forwards_ask_nonblocking(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)

    def no_request(msg, t):
        raise AssertionError("AskUserQuestion must never gate/block")

    monkeypatch.setattr(claude_hook.client, "request", no_request)
    out = io.StringIO()
    assert claude_hook.run(_pretooluse(), out) == 0
    assert out.getvalue() == ""  # never prints: native question UI untouched
    (msg,) = sent
    assert msg["event"] == "ask_pending"
    assert msg["agent"] == "claude"
    assert msg["session_id"] == "s1"
    assert msg["tool_use_id"] == "toolu_ask1"
    assert msg["questions"] == QUESTIONS
    assert msg["multiSelect"] is False


def test_hook_ask_tolerates_malformed_tool_input(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    out = io.StringIO()
    for tool_input in ({}, {"questions": []}, {"questions": "nope"}, "garbage", None):
        assert claude_hook.run(_pretooluse(tool_input=tool_input), out) == 0
    assert sent == [] and out.getvalue() == ""


def test_hook_ask_respects_nogate_env(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    monkeypatch.setenv("BUDDY_BRIDGE_NOGATE", "1")
    assert claude_hook.run(_pretooluse(), io.StringIO()) == 0
    assert sent == []


def test_hook_posttooluse_mirror_carries_tool_use_id(monkeypatch):
    sent = []
    monkeypatch.setattr(claude_hook.client, "send_event", lambda msg: sent.append(msg) or True)
    stdin = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "cwd": "C:/proj",
            "tool_name": "AskUserQuestion",
            "tool_use_id": "toolu_ask1",
        }
    )
    assert claude_hook.run(stdin, io.StringIO()) == 0
    assert sent[0]["tool_use_id"] == "toolu_ask1"


# ---- daemon side ----


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def drop_connection(self):
        self._connected = False

    def wake(self):
        pass

    async def stop(self):
        pass


@pytest.fixture
def daemon(home):
    cfg = load_config(home)
    d = Daemon(cfg)
    d.link = FakeLink(connected=True)
    d.negotiated_proto = PROTO_V2
    d.hello_ack = HelloAck(proto=2, caps=("sessions", "ask", "cancel"))
    return d


def _ask_pending(sid="s1", tuid="toolu_ask1", questions=_DEFAULT, multi=False):
    return {
        "event": "ask_pending",
        "agent": "claude",
        "session_id": sid,
        "tool_use_id": tuid,
        "questions": QUESTIONS if questions is _DEFAULT else questions,
        "multiSelect": multi,
    }


def _sent_asks(daemon):
    return [json.loads(line) for line in daemon.link.sent if '"evt":"ask"' in line]


async def test_daemon_emits_ask_evt(daemon):
    await daemon.handle_ask_pending(_ask_pending())
    (evt,) = _sent_asks(daemon)
    assert evt["evt"] == "ask"
    assert evt["id"] == "toolu_ask1"  # wire-safe tool_use_id reused verbatim
    assert evt["sid"] == "cli:1"
    assert evt["multiSelect"] is False
    (question,) = evt["questions"]
    assert question["header"] == "Approach"
    assert question["text"] == "Which approach should we take?"
    assert question["options"] == [
        {"label": "Option A", "desc": "the safe one"},
        {"label": "Option B", "desc": "the fast one"},
    ]
    assert "toolu_ask1" in daemon._live_asks


async def test_daemon_ask_requires_cap(daemon):
    daemon.hello_ack = HelloAck(proto=2, caps=("sessions",))  # no ask cap
    await daemon.handle_ask_pending(_ask_pending())
    assert _sent_asks(daemon) == [] and daemon._live_asks == {}


async def test_daemon_ask_requires_v2_link(daemon):
    daemon.negotiated_proto = 1
    await daemon.handle_ask_pending(_ask_pending())
    assert _sent_asks(daemon) == []
    daemon.negotiated_proto = PROTO_V2
    daemon.link._connected = False
    await daemon.handle_ask_pending(_ask_pending())
    assert _sent_asks(daemon) == []


async def test_daemon_ask_dedupes_tool_use_id(daemon):
    await daemon.handle_ask_pending(_ask_pending())
    await daemon.handle_ask_pending(_ask_pending())  # two matching hook groups fired
    assert len(_sent_asks(daemon)) == 1


async def test_daemon_ask_mints_id_for_unsafe_tool_use_id(daemon):
    await daemon.handle_ask_pending(_ask_pending(tuid="x" * 60))
    (evt,) = _sent_asks(daemon)
    assert evt["id"] == "a1"


async def test_daemon_ask_truncates_long_fields(daemon):
    questions = [
        {
            "question": "q" * 500,
            "header": "h" * 100,
            "options": [{"label": "L" * 90, "description": "d" * 200}] * 9,
        }
    ]
    await daemon.handle_ask_pending(_ask_pending(questions=questions))
    (evt,) = _sent_asks(daemon)
    (question,) = evt["questions"]
    assert len(question["header"].encode()) <= 32  # protocol cap
    assert len(question["text"].encode()) <= 96
    assert len(question["options"]) <= 4
    assert all(len(o["label"].encode()) <= 32 for o in question["options"])


async def test_daemon_ask_malformed_questions_ignored(daemon):
    for questions in ([], ["nope"], [{}], "garbage", None, [{"options": "x"}]):
        await daemon.handle_ask_pending(_ask_pending(questions=questions))
    assert _sent_asks(daemon) == [] and daemon._live_asks == {}


async def _cancel_lines(daemon):
    return [json.loads(line) for line in daemon.link.sent if '"evt":"ask_cancel"' in line]


async def test_posttooluse_cancels_displayed_ask(daemon):
    await daemon.handle_ask_pending(_ask_pending())
    await daemon._on_hook_followups(
        {"agent": "claude", "name": "PostToolUse", "session_id": "s1",
         "tool_use_id": "toolu_ask1"}
    )
    assert daemon._live_asks == {}
    (cancel,) = await _cancel_lines(daemon)
    assert cancel == {"evt": "ask_cancel", "id": "toolu_ask1"}


@pytest.mark.parametrize("name", ["Stop", "SessionEnd", "UserPromptSubmit"])
async def test_session_level_events_cancel_asks(daemon, name):
    await daemon.handle_ask_pending(_ask_pending())
    await daemon._on_hook_followups({"agent": "claude", "name": name, "session_id": "s1"})
    assert daemon._live_asks == {}
    assert len(await _cancel_lines(daemon)) == 1


async def test_other_sessions_asks_survive_cancel(daemon):
    await daemon.handle_ask_pending(_ask_pending(sid="s1", tuid="toolu_a"))
    await daemon.handle_ask_pending(_ask_pending(sid="s2", tuid="toolu_b"))
    await daemon._on_hook_followups({"agent": "claude", "name": "Stop", "session_id": "s1"})
    assert list(daemon._live_asks) == ["toolu_b"]


async def test_reconnect_clears_live_asks(daemon):
    await daemon.handle_ask_pending(_ask_pending())
    assert daemon._live_asks
    await daemon._on_ble_connect()
    assert daemon._live_asks == {}


# ---- conversion unit ----


def test_convert_ask_questions_shapes():
    assert convert_ask_questions(None) == []
    assert convert_ask_questions("x") == []
    assert convert_ask_questions([{"question": "", "header": "", "options": []}]) == []
    out = convert_ask_questions(
        [{"question": "Pick", "options": ["plain-string", {"label": "obj"}]}]
    )
    assert out == [
        {"header": "", "text": "Pick",
         "options": [{"label": "plain-string", "desc": ""}, {"label": "obj", "desc": ""}]}
    ]
    many = convert_ask_questions([{"question": f"q{i}"} for i in range(9)])
    assert len(many) == 3  # question count capped
