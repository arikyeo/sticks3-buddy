"""buddy-bridge telegram setup/test/status — mocked getpass/input/client,
token never printed, no real network."""

import builtins
import json

import pytest

from buddy_bridge import cli
from buddy_bridge.config import load_config, save_config
from buddy_bridge.notify.telegram import TelegramClient

TOKEN = "777888:CLI-SECRET-token"


@pytest.fixture
def scripted_io(monkeypatch):
    """Feed getpass + input answers in order."""
    def scripted(getpass_value, inputs):
        answers = iter(inputs)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": getpass_value)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers, ""))
    return scripted


def test_setup_writes_config(home, scripted_io, capsys):
    scripted_io(TOKEN, ["42, -100777", "y", "y"])
    assert cli.main(["telegram", "setup"]) == 0
    cfg = load_config(home, create=False)
    assert cfg.telegram_enabled is True
    assert cfg.telegram_token == TOKEN
    assert cfg.telegram_chats == (42, -100777)
    assert cfg.telegram_decisions is True
    assert cfg.telegram_answer_asks is True
    out = capsys.readouterr().out
    assert TOKEN not in out  # never echoed
    assert "hooks install claude" in out  # answer_asks changed -> reminder
    assert "telegram test" in out


def test_setup_warns_when_incomplete(home, scripted_io, capsys):
    scripted_io("", ["", "n", "n"])  # keep (empty) token, no chats
    assert cli.main(["telegram", "setup"]) == 0
    out = capsys.readouterr().out
    assert "stays OFF" in out
    cfg = load_config(home, create=False)
    assert cfg.telegram_active is False


def test_setup_ignores_bad_chat_ids(home, scripted_io, capsys):
    scripted_io(TOKEN, ["42, abc", "n", "n"])
    cli.main(["telegram", "setup"])
    cfg = load_config(home, create=False)
    assert cfg.telegram_chats == (42,)
    assert "ignored non-numeric" in capsys.readouterr().out


def _activate(home, **over):
    cfg = load_config(home)
    cfg.telegram_enabled = True
    cfg.telegram_token = TOKEN
    cfg.telegram_chats = (42, 43)
    for k, v in over.items():
        setattr(cfg, k, v)
    save_config(cfg)
    return cfg


def test_test_sends_to_every_chat(home, monkeypatch, capsys):
    _activate(home)
    calls = []

    def fake_call(self, method, payload, timeout=None):
        calls.append((method, payload))
        if method == "getMe":
            return {"username": "my_buddy_bot"}
        return {"message_id": 1} if payload.get("chat_id") == 42 else None

    monkeypatch.setattr(TelegramClient, "call", fake_call)
    assert cli.main(["telegram", "test"]) == 1  # one chat failed
    out = capsys.readouterr().out
    assert "bot @my_buddy_bot ok" in out
    assert "chat 42: sent" in out
    assert "chat 43: FAILED" in out
    assert TOKEN not in out
    assert [m for m, _ in calls] == ["getMe", "sendMessage", "sendMessage"]


def test_test_all_ok_exit_zero(home, monkeypatch):
    _activate(home)
    monkeypatch.setattr(
        TelegramClient, "call",
        lambda self, m, p, timeout=None: {"username": "b"} if m == "getMe" else {"message_id": 1},
    )
    assert cli.main(["telegram", "test"]) == 0


def test_test_refuses_when_inactive(home, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        TelegramClient, "call", lambda self, m, p, timeout=None: called.append(m)
    )
    assert cli.main(["telegram", "test"]) == 1
    out = capsys.readouterr().out
    assert "not active" in out
    assert called == []  # no network attempt without config


def test_test_bad_token_redacted(home, monkeypatch, capsys):
    _activate(home)
    monkeypatch.setattr(TelegramClient, "call", lambda self, m, p, timeout=None: None)
    assert cli.main(["telegram", "test"]) == 1
    out = capsys.readouterr().out
    assert "getMe failed" in out
    assert TOKEN not in out


def test_status_redacts_token(home, capsys):
    _activate(home, telegram_answer_asks=True)
    assert cli.main(["telegram", "status"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "set (hidden)" in out
    assert "42, 43" in out
    assert "answer_asks   : True" in out
    assert "daemon        : not running" in out


def test_status_empty_config(home, capsys):
    assert cli.main(["telegram", "status"]) == 0
    out = capsys.readouterr().out
    assert "EMPTY" in out
    assert "(none)" in out


def test_hooks_install_uses_answer_asks_config(home, monkeypatch, tmp_path):
    _activate(home, telegram_answer_asks=True)
    seen = {}

    def fake_install(path, *, gate, gate_tools, answer_asks):
        seen.update(gate=gate, answer_asks=answer_asks)
        return True

    monkeypatch.setattr("buddy_bridge.install.claude_settings.install", fake_install)
    assert cli.main(["hooks", "install", "claude"]) == 0
    assert seen["answer_asks"] is True


def test_status_json_from_daemon_section(home, monkeypatch, capsys):
    _activate(home)
    monkeypatch.setattr(
        cli, "_daemon_request",
        lambda payload, timeout=3.0: {
            "telegram": {"active": True, "queued": 2, "muted_chats": [43]}
        },
    )
    cli.main(["telegram", "status"])
    out = capsys.readouterr().out
    assert "queued=2" in out
    assert "muted=[43]" in out
    assert TOKEN not in json.dumps(out)
