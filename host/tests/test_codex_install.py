import json

import pytest
import tomllib

from buddy_bridge.install import codex_hooks
from buddy_bridge.install._merge import SettingsError

PY = "C:/fake/python.exe"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_writes_all_entries(tmp_path):
    assert codex_hooks.install(tmp_path, python_exe=PY) is True
    data = _read(tmp_path / "hooks.json")
    hooks = data["hooks"]
    assert set(hooks) == {
        "PermissionRequest", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
    }
    perm = hooks["PermissionRequest"][0]
    assert perm["matcher"] == ".*"
    assert perm["hooks"][0]["timeout"] == 115
    assert perm["hooks"][0]["command"] == f'"{PY}" -m buddy_bridge.hooks.codex_hook'
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    assert hooks["PostToolUse"][0]["matcher"] == "*"
    assert "matcher" not in hooks["UserPromptSubmit"][0]
    assert "matcher" not in hooks["Stop"][0]
    # feature flag written into a fresh config.toml
    config = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert config["features"]["codex_hooks"] is True


def test_install_idempotent(tmp_path):
    assert codex_hooks.install(tmp_path, python_exe=PY) is True
    hooks_before = (tmp_path / "hooks.json").read_text(encoding="utf-8")
    config_before = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert codex_hooks.install(tmp_path, python_exe=PY) is False
    assert (tmp_path / "hooks.json").read_text(encoding="utf-8") == hooks_before
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == config_before


FOREIGN = {
    "hooks": {
        "PermissionRequest": [
            {"matcher": ".*", "hooks": [{"type": "command", "command": "other-gate"}]}
        ],
        "TurnComplete": [{"hooks": [{"type": "command", "command": "notifier"}]}],
    },
    "version": 3,
}


def test_merge_preserves_foreign_and_uninstall_removes_only_ours(tmp_path):
    (tmp_path / "hooks.json").write_text(json.dumps(FOREIGN), encoding="utf-8")
    codex_hooks.install(tmp_path, python_exe=PY)
    data = _read(tmp_path / "hooks.json")
    assert data["version"] == 3
    perm = data["hooks"]["PermissionRequest"]
    assert perm[0]["hooks"][0]["command"] == "other-gate"
    assert len(perm) == 2
    assert data["hooks"]["TurnComplete"] == FOREIGN["hooks"]["TurnComplete"]

    assert codex_hooks.uninstall(tmp_path) is True
    assert _read(tmp_path / "hooks.json") == FOREIGN
    assert codex_hooks.uninstall(tmp_path) is False


def test_unparseable_hooks_json_refused(tmp_path):
    (tmp_path / "hooks.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(SettingsError):
        codex_hooks.install(tmp_path, python_exe=PY)
    assert (tmp_path / "hooks.json").read_text(encoding="utf-8") == "{oops"


# ---- config.toml feature-flag editing ----


def _flag(tmp_path):
    return codex_hooks.ensure_codex_hooks_flag(tmp_path / "config.toml")


def test_flag_created_when_missing_file(tmp_path):
    assert _flag(tmp_path) is True
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["features"]["codex_hooks"] is True
    assert _flag(tmp_path) is False  # second run: no-op


def test_flag_appends_section_preserving_rest(tmp_path):
    original = 'model = "gpt-5.4"\napproval_policy = "on-request"\n\n[tools]\nweb_search = true\n'
    (tmp_path / "config.toml").write_text(original, encoding="utf-8")
    assert _flag(tmp_path) is True
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert text.startswith(original)  # existing content byte-for-byte intact
    parsed = tomllib.loads(text)
    assert parsed["features"]["codex_hooks"] is True
    assert parsed["model"] == "gpt-5.4"
    assert parsed["tools"]["web_search"] is True


def test_flag_inserted_into_existing_features_section(tmp_path):
    original = "[features]\nsome_flag = false\n\n[tools]\nweb_search = true\n"
    (tmp_path / "config.toml").write_text(original, encoding="utf-8")
    assert _flag(tmp_path) is True
    parsed = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert parsed["features"] == {"codex_hooks": True, "some_flag": False}
    assert parsed["tools"] == {"web_search": True}


def test_flag_false_replaced_in_place(tmp_path):
    original = "[features]\ncodex_hooks = false  # disabled\nother = 1\n"
    (tmp_path / "config.toml").write_text(original, encoding="utf-8")
    assert _flag(tmp_path) is True
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["features"]["codex_hooks"] is True
    assert parsed["features"]["other"] == 1
    assert "codex_hooks = false" not in text


def test_flag_already_true_untouched(tmp_path):
    original = "# my config\n[features]\ncodex_hooks = true\n"
    (tmp_path / "config.toml").write_text(original, encoding="utf-8")
    assert _flag(tmp_path) is False
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == original


def test_flag_unparseable_config_refused(tmp_path):
    (tmp_path / "config.toml").write_text("[features\nbroken", encoding="utf-8")
    with pytest.raises(SettingsError):
        _flag(tmp_path)
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "[features\nbroken"
