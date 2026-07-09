"""Shared managed-merge helpers for hook config files (Claude + Codex).

Both ``~/.claude/settings.json`` and ``~/.codex/hooks.json`` use the same
shape: ``{"hooks": {"<Event>": [ {matcher?, hooks:[{type,command,...}]} ]}}``.

We identify our entries by a marker substring inside the command string and
never touch anything else: foreign hook groups, unknown top-level keys and
unknown event names all survive install/uninstall byte-for-byte.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


class SettingsError(RuntimeError):
    """Raised instead of clobbering a config file we cannot safely parse."""


def load_json_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsError(f"refusing to modify unparseable JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"refusing to modify {path}: top level is not an object")
    return data


def backup_file(path: Path, suffix: str = ".buddy-bridge.bak") -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(path.name + suffix)
    shutil.copy2(path, dest)
    return dest


def write_json_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def group_is_ours(group: object, marker: str) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and marker in str(h.get("command", "")) for h in hooks
    )


def merge_hook_groups(data: dict, desired: dict[str, list[dict]], marker: str) -> tuple[dict, bool]:
    """Merge our desired groups into ``data['hooks']``, replacing only ours.

    ``desired`` maps event name -> list of our groups for that event (empty
    list means "make sure none of ours remain"). Returns (new_data, changed).
    """
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise SettingsError("existing 'hooks' key is not an object; refusing to modify")

    new_hooks: dict = {}
    changed = False
    events = list(hooks.keys()) + [e for e in desired if e not in hooks]
    for event in events:
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            # foreign shape for an event we don't manage: keep untouched
            if event in desired and desired[event]:
                raise SettingsError(
                    f"existing hooks[{event!r}] is not a list; refusing to modify"
                )
            new_hooks[event] = existing
            continue
        foreign = [g for g in existing if not group_is_ours(g, marker)]
        ours = desired.get(event, [])
        merged = foreign + ours
        if merged != existing:
            changed = True
        if merged:
            new_hooks[event] = merged
    if new_hooks != (data.get("hooks") or {}):
        changed = True

    out = dict(data)
    if new_hooks:
        out["hooks"] = new_hooks
    else:
        out.pop("hooks", None)
    return out, changed


def remove_our_groups(data: dict, marker: str) -> tuple[dict, bool]:
    return merge_hook_groups(data, {}, marker)
