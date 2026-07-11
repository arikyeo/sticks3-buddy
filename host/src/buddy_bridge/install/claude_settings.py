"""Idempotent managed merge of buddy-bridge hooks into ``~/.claude/settings.json``.

Marker: any hook whose command contains ``buddy_bridge.hooks.claude_hook`` is
ours; everything else is foreign and preserved byte-for-byte. Install backs
up the settings file first and only rewrites when something changes.
Uninstall removes exactly our entries.

The hook command uses the current interpreter (sys.executable) with forward
slashes so the JSON stays readable on Windows:
    "C:/path/to/python.exe" -m buddy_bridge.hooks.claude_hook

PreToolUse gets two kinds of entries:
  * an always-present ``AskUserQuestion`` matcher. Default: non-blocking,
    the hook just forwards the question payload so a v2 stick can display
    it. With ``answer_asks=True`` ([telegram] answer_asks) the command
    gains ``--answer-asks`` and the same 330s timeout as the gate: the
    hook then blocks so a Telegram-chosen option can answer the question
    (timeout falls through to the terminal dialog);
  * the gate entry, written ONLY when [claude] gate=true, with the matcher
    taken from [claude] gate_tools and a 330s timeout (the hook itself
    gives up at 320s, the daemon decides by 300s).
"""

from __future__ import annotations

import sys
from pathlib import Path

from ._merge import (
    SettingsError,
    backup_file,
    load_json_config,
    merge_hook_groups,
    remove_our_groups,
    write_json_config,
)

__all__ = ["MARKER", "SettingsError", "hook_command", "install", "uninstall", "installed"]

MARKER = "buddy_bridge.hooks.claude_hook"

MIRROR_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "PostToolUse",
    "Notification",
)
GATE_TIMEOUT_SECS = 330


def hook_command(python_exe: str | None = None) -> str:
    py = (python_exe or sys.executable).replace("\\", "/")
    return f'"{py}" -m buddy_bridge.hooks.claude_hook'


def _desired_groups(
    gate: bool, gate_tools: str, python_exe: str | None, answer_asks: bool = False
) -> dict[str, list[dict]]:
    command = hook_command(python_exe)
    desired: dict[str, list[dict]] = {}
    for event in MIRROR_EVENTS:
        group: dict = {"hooks": [{"type": "command", "command": command}]}
        if event == "PostToolUse":
            group = {"matcher": "*", **group}
        desired[event] = [group]
    # Ask origination first (always installed; blocking only with
    # answer_asks), then the gate entry when enabled. `desired` always
    # carries the full PreToolUse list so gate=False / answer_asks=False
    # actively remove a stale variant.
    ask_hook: dict = {"type": "command", "command": command}
    if answer_asks:
        ask_hook = {
            "type": "command",
            "command": f"{command} --answer-asks",
            "timeout": GATE_TIMEOUT_SECS,
        }
    pre: list[dict] = [{"matcher": "AskUserQuestion", "hooks": [ask_hook]}]
    if gate:
        pre.append(
            {
                "matcher": gate_tools,
                "hooks": [
                    {"type": "command", "command": command, "timeout": GATE_TIMEOUT_SECS}
                ],
            }
        )
    desired["PreToolUse"] = pre
    return desired


def install(
    settings_path: Path,
    *,
    gate: bool = False,
    gate_tools: str = "Bash",
    python_exe: str | None = None,
    answer_asks: bool = False,
) -> bool:
    """Merge our hooks in. Returns True when the file was modified."""
    data = load_json_config(settings_path)
    merged, changed = merge_hook_groups(
        data, _desired_groups(gate, gate_tools, python_exe, answer_asks), MARKER
    )
    if not changed:
        return False
    backup_file(settings_path)
    write_json_config(settings_path, merged)
    return True


def uninstall(settings_path: Path) -> bool:
    """Remove exactly our entries. Returns True when the file was modified."""
    try:
        data = load_json_config(settings_path)
    except SettingsError:
        raise
    if not data:
        return False
    cleaned, changed = remove_our_groups(data, MARKER)
    if not changed:
        return False
    backup_file(settings_path)
    write_json_config(settings_path, cleaned)
    return True


def installed(settings_path: Path) -> bool:
    try:
        return MARKER in settings_path.read_text(encoding="utf-8")
    except OSError:
        return False
