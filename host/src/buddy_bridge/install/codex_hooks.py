"""Install buddy-bridge hooks for Codex CLI.

Two touch points:
  * ``~/.codex/hooks.json`` — managed merge, same marker discipline as the
    Claude installer (marker: buddy_bridge.hooks.codex_hook). Entries:
      PermissionRequest  matcher ".*"                          timeout 115
      SessionStart       matcher "startup|resume|clear|compact"
      UserPromptSubmit
      PostToolUse        matcher "*"
      Stop
  * ``~/.codex/config.toml`` — ensure ``codex_hooks = true`` under
    ``[features]``. TOML-preserving edit: naive but careful — the existing
    text is kept byte-for-byte except for the one inserted/replaced line
    (or an appended section), the result is validated with tomllib before
    it is written, and an unparseable existing file is refused rather than
    clobbered.

Uninstall removes exactly our hooks.json entries; the feature flag is left
alone (other tools may rely on codex hooks).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from ._merge import (
    SettingsError,
    backup_file,
    load_json_config,
    merge_hook_groups,
    remove_our_groups,
    write_json_config,
)

__all__ = [
    "MARKER",
    "SettingsError",
    "ensure_codex_hooks_flag",
    "hook_command",
    "install",
    "installed",
    "uninstall",
]

MARKER = "buddy_bridge.hooks.codex_hook"
PERMISSION_TIMEOUT_SECS = 115
SESSION_START_MATCHER = "startup|resume|clear|compact"

_FEATURES_HEADER = re.compile(r"^\s*\[features\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_CODEX_HOOKS_KEY = re.compile(r"^\s*codex_hooks\s*=")


def hook_command(python_exe: str | None = None) -> str:
    py = (python_exe or sys.executable).replace("\\", "/")
    return f'"{py}" -m buddy_bridge.hooks.codex_hook'


def _desired_groups(python_exe: str | None) -> dict[str, list[dict]]:
    command = hook_command(python_exe)

    def group(matcher: str | None, timeout: int | None = None) -> dict:
        hook: dict = {"type": "command", "command": command}
        if timeout is not None:
            hook["timeout"] = timeout
        out: dict = {}
        if matcher is not None:
            out["matcher"] = matcher
        out["hooks"] = [hook]
        return out

    return {
        "PermissionRequest": [group(".*", PERMISSION_TIMEOUT_SECS)],
        "SessionStart": [group(SESSION_START_MATCHER)],
        "UserPromptSubmit": [group(None)],
        "PostToolUse": [group("*")],
        "Stop": [group(None)],
    }


def install(codex_dir: Path, python_exe: str | None = None) -> bool:
    """Merge our hooks into hooks.json + set the feature flag. True if changed."""
    hooks_path = codex_dir / "hooks.json"
    data = load_json_config(hooks_path)
    merged, changed = merge_hook_groups(data, _desired_groups(python_exe), MARKER)
    if changed:
        backup_file(hooks_path)
        write_json_config(hooks_path, merged)
    flag_changed = ensure_codex_hooks_flag(codex_dir / "config.toml")
    return changed or flag_changed


def uninstall(codex_dir: Path) -> bool:
    hooks_path = codex_dir / "hooks.json"
    data = load_json_config(hooks_path)
    if not data:
        return False
    cleaned, changed = remove_our_groups(data, MARKER)
    if not changed:
        return False
    backup_file(hooks_path)
    write_json_config(hooks_path, cleaned)
    return True


def installed(codex_dir: Path) -> bool:
    try:
        return MARKER in (codex_dir / "hooks.json").read_text(encoding="utf-8")
    except OSError:
        return False


def ensure_codex_hooks_flag(config_path: Path) -> bool:
    """Ensure ``codex_hooks = true`` under ``[features]``. True if edited."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise SettingsError(f"cannot read {config_path}: {exc}") from exc

    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"refusing to modify unparseable TOML in {config_path}: {exc}"
        ) from exc
    features = current.get("features")
    if isinstance(features, dict) and features.get("codex_hooks") is True:
        return False

    lines = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if _FEATURES_HEADER.match(line)), None
    )
    if header_idx is None:
        suffix = "" if (not text or text.endswith("\n")) else "\n"
        new_text = text + suffix + ("\n" if text else "") + "[features]\ncodex_hooks = true\n"
    else:
        section_end = next(
            (
                j
                for j in range(header_idx + 1, len(lines))
                if _ANY_HEADER.match(lines[j])
            ),
            len(lines),
        )
        key_idx = next(
            (
                j
                for j in range(header_idx + 1, section_end)
                if _CODEX_HOOKS_KEY.match(lines[j])
            ),
            None,
        )
        if key_idx is not None:
            lines[key_idx] = "codex_hooks = true"
        else:
            lines.insert(header_idx + 1, "codex_hooks = true")
        new_text = "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")

    # validate BEFORE touching the file
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - safety net
        raise SettingsError(f"internal error editing {config_path}: {exc}") from exc
    if parsed.get("features", {}).get("codex_hooks") is not True:  # pragma: no cover
        raise SettingsError(f"internal error editing {config_path}: flag not set")

    backup_file(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(config_path)
    return True
