"""buddy-bridge configuration.

TOML config lives at ``<home>/config.toml`` where ``<home>`` is
``~/.buddy-bridge`` unless overridden by the ``BUDDY_BRIDGE_HOME`` env var.
Runtime state (endpoint.json, ledger.json, audit.jsonl, matchers.toml,
offsets.json) always lives in the same home directory — never repo-relative.

The ``[host] id`` is a random 6-char base36 string minted exactly once on
first load and persisted; every later load returns the same id.
"""

from __future__ import annotations

import os
import secrets
import socket
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_HOME = "BUDDY_BRIDGE_HOME"
CONFIG_FILENAME = "config.toml"

_BASE36 = string.digits + string.ascii_lowercase
_BLE_MODES = ("exclusive", "ondemand", "off")


def bridge_home() -> Path:
    """Resolve the buddy-bridge home directory (env override wins)."""
    env = os.environ.get(ENV_HOME, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".buddy-bridge"


def mint_host_id() -> str:
    """Random 6-char base36 host id."""
    return "".join(secrets.choice(_BASE36) for _ in range(6))


@dataclass
class Config:
    # [host]
    host_id: str = ""
    host_name: str = ""
    owner: str = ""
    # [ble]
    ble_mode: str = "exclusive"  # exclusive | ondemand | off
    ble_name_prefix: str = "Claude"
    ble_address: str = ""
    # [ipc]
    ipc_port: int = 0  # 0 = ephemeral
    # [claude]
    claude_enabled: bool = True
    claude_gate: bool = False
    claude_gate_tools: str = "Bash"
    # [codex]
    codex_enabled: bool = True
    codex_gate: bool = True
    # [timeouts] (seconds the daemon waits for a device decision)
    claude_decision: float = 300.0
    codex_decision: float = 105.0

    home: Path = field(default_factory=bridge_home)

    @property
    def path(self) -> Path:
        return self.home / CONFIG_FILENAME


def _get_str(table: dict, key: str, default: str) -> str:
    val = table.get(key, default)
    return val if isinstance(val, str) else default


def _get_bool(table: dict, key: str, default: bool) -> bool:
    val = table.get(key, default)
    return val if isinstance(val, bool) else default


def _get_int(table: dict, key: str, default: int) -> int:
    val = table.get(key, default)
    return val if isinstance(val, int) and not isinstance(val, bool) else default


def _get_num(table: dict, key: str, default: float) -> float:
    val = table.get(key, default)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return default
    return float(val)


def load_config(home: Path | None = None, create: bool = True) -> Config:
    """Load config from ``<home>/config.toml``, tolerant of unknown/bad keys.

    When ``create`` is true (default), a missing host id is minted and the
    config is persisted so later loads see the same id.
    """
    cfg = Config(home=home if home is not None else bridge_home())
    data: dict = {}
    try:
        data = tomllib.loads(cfg.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError):
        # Unreadable/corrupt config: run with defaults rather than crash.
        data = {}

    host = data.get("host", {}) if isinstance(data.get("host"), dict) else {}
    ble = data.get("ble", {}) if isinstance(data.get("ble"), dict) else {}
    ipc = data.get("ipc", {}) if isinstance(data.get("ipc"), dict) else {}
    claude = data.get("claude", {}) if isinstance(data.get("claude"), dict) else {}
    codex = data.get("codex", {}) if isinstance(data.get("codex"), dict) else {}
    timeouts = data.get("timeouts", {}) if isinstance(data.get("timeouts"), dict) else {}

    cfg.host_id = _get_str(host, "id", "")
    cfg.host_name = _get_str(host, "name", "")
    cfg.owner = _get_str(host, "owner", "")

    cfg.ble_mode = _get_str(ble, "mode", "exclusive")
    if cfg.ble_mode not in _BLE_MODES:
        cfg.ble_mode = "exclusive"
    cfg.ble_name_prefix = _get_str(ble, "name_prefix", "Claude") or "Claude"
    cfg.ble_address = _get_str(ble, "address", "")

    cfg.ipc_port = _get_int(ipc, "port", 0)

    cfg.claude_enabled = _get_bool(claude, "enabled", True)
    cfg.claude_gate = _get_bool(claude, "gate", False)
    cfg.claude_gate_tools = _get_str(claude, "gate_tools", "Bash") or "Bash"

    cfg.codex_enabled = _get_bool(codex, "enabled", True)
    cfg.codex_gate = _get_bool(codex, "gate", True)

    cfg.claude_decision = _get_num(timeouts, "claude_decision", 300.0)
    cfg.codex_decision = _get_num(timeouts, "codex_decision", 105.0)

    changed = False
    if not cfg.host_id:
        cfg.host_id = mint_host_id()
        changed = True
    if not cfg.host_name:
        cfg.host_name = socket.gethostname() or "host"
        changed = True

    if changed and create:
        save_config(cfg)
    return cfg


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def to_toml(cfg: Config) -> str:
    b = lambda v: "true" if v else "false"  # noqa: E731
    return (
        "# buddy-bridge configuration\n"
        "\n[host]\n"
        f"id = {_toml_str(cfg.host_id)}\n"
        f"name = {_toml_str(cfg.host_name)}\n"
        f"owner = {_toml_str(cfg.owner)}\n"
        "\n[ble]\n"
        f"mode = {_toml_str(cfg.ble_mode)}\n"
        f"name_prefix = {_toml_str(cfg.ble_name_prefix)}\n"
        f"address = {_toml_str(cfg.ble_address)}\n"
        "\n[ipc]\n"
        f"port = {cfg.ipc_port}\n"
        "\n[claude]\n"
        f"enabled = {b(cfg.claude_enabled)}\n"
        f"gate = {b(cfg.claude_gate)}\n"
        f"gate_tools = {_toml_str(cfg.claude_gate_tools)}\n"
        "\n[codex]\n"
        f"enabled = {b(cfg.codex_enabled)}\n"
        f"gate = {b(cfg.codex_gate)}\n"
        "\n[timeouts]\n"
        f"claude_decision = {_toml_num(cfg.claude_decision)}\n"
        f"codex_decision = {_toml_num(cfg.codex_decision)}\n"
    )


def save_config(cfg: Config) -> Path:
    cfg.home.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.with_name(cfg.path.name + ".tmp")
    tmp.write_text(to_toml(cfg), encoding="utf-8")
    os.replace(tmp, cfg.path)
    return cfg.path
