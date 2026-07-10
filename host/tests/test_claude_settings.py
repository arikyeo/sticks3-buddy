import json

import pytest

from buddy_bridge.install import claude_settings
from buddy_bridge.install._merge import SettingsError

PY = "C:/fake/python.exe"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_into_missing_file(tmp_path):
    settings = tmp_path / "settings.json"
    assert claude_settings.install(settings, python_exe=PY) is True
    data = _read(settings)
    events = set(data["hooks"].keys())
    assert events == {
        "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "PostToolUse",
        "Notification", "PreToolUse",
    }
    # gate off by default: PreToolUse carries only the non-blocking ask
    # forwarder (AskUserQuestion display), no timeout
    (ask_group,) = data["hooks"]["PreToolUse"]
    assert ask_group["matcher"] == "AskUserQuestion"
    assert "timeout" not in ask_group["hooks"][0]
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == f'"{PY}" -m buddy_bridge.hooks.claude_hook'
    assert "\\" not in cmd  # forward slashes only
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "*"


def test_install_gate_entry(tmp_path):
    settings = tmp_path / "settings.json"
    claude_settings.install(settings, gate=True, gate_tools="Bash|Write", python_exe=PY)
    data = _read(settings)
    ask_group, gate_group = data["hooks"]["PreToolUse"]
    assert ask_group["matcher"] == "AskUserQuestion"
    assert gate_group["matcher"] == "Bash|Write"
    hook = gate_group["hooks"][0]
    assert hook["timeout"] == 330
    assert "buddy_bridge.hooks.claude_hook" in hook["command"]

    # flipping the gate off removes the gate entry; the ask forwarder stays
    assert claude_settings.install(settings, gate=False, python_exe=PY) is True
    (only,) = _read(settings)["hooks"]["PreToolUse"]
    assert only["matcher"] == "AskUserQuestion"


def test_install_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    assert claude_settings.install(settings, gate=True, python_exe=PY) is True
    first = settings.read_text(encoding="utf-8")
    assert claude_settings.install(settings, gate=True, python_exe=PY) is False
    assert settings.read_text(encoding="utf-8") == first


FOREIGN = {
    "model": "opus",
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "other-tool --start"}]}
        ],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "security-scan"}]}
        ],
        "SomeFutureEvent": [
            {"hooks": [{"type": "command", "command": "future-thing"}]}
        ],
    },
}


def test_merge_preserves_foreign_and_uninstall_removes_only_ours(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(FOREIGN), encoding="utf-8")

    assert claude_settings.install(settings, gate=True, python_exe=PY) is True
    data = _read(settings)
    assert data["model"] == "opus"
    # foreign SessionStart group survives, ours appended after it
    ss = data["hooks"]["SessionStart"]
    assert ss[0]["hooks"][0]["command"] == "other-tool --start"
    assert any("buddy_bridge.hooks.claude_hook" in h["command"] for g in ss[1:] for h in g["hooks"])
    # foreign PreToolUse survives alongside our ask + gate entries
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "security-scan"
    assert len(pre) == 3
    assert {g.get("matcher") for g in pre[1:]} == {"AskUserQuestion", "Bash"}
    assert data["hooks"]["SomeFutureEvent"] == FOREIGN["hooks"]["SomeFutureEvent"]

    assert claude_settings.uninstall(settings) is True
    after = _read(settings)
    assert after == FOREIGN  # byte-for-byte structural restore
    # uninstalling again is a no-op
    assert claude_settings.uninstall(settings) is False


def test_uninstall_missing_file(tmp_path):
    assert claude_settings.uninstall(tmp_path / "settings.json") is False


def test_backup_created(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(FOREIGN), encoding="utf-8")
    claude_settings.install(settings, python_exe=PY)
    backup = tmp_path / "settings.json.buddy-bridge.bak"
    assert backup.exists()
    assert _read(backup) == FOREIGN


def test_unparseable_settings_refused(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError):
        claude_settings.install(settings, python_exe=PY)
    assert settings.read_text(encoding="utf-8") == "{not json"  # untouched


def test_installed_probe(tmp_path):
    settings = tmp_path / "settings.json"
    assert claude_settings.installed(settings) is False
    claude_settings.install(settings, python_exe=PY)
    assert claude_settings.installed(settings) is True
