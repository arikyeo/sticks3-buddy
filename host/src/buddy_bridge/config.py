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
    # exclusive mode only: minutes of local idleness (no running/waiting
    # session, no pending prompt, no queued card) after which the daemon
    # gracefully disconnects so other hosts can grab the stick. 0 = never.
    idle_yield_min: int = 0
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
    # [cards] (info-card pipeline: off by default, no network unless enabled)
    cards_enabled: bool = False
    cards_github: bool = True
    cards_repos: tuple[str, ...] = ()
    cards_weather_lat: float | None = None
    cards_weather_lon: float | None = None
    cards_update_check: bool = True  # firmware update-check card (active once cards_enabled)
    cards_firmware_repo: str = "arikyeo/sticks3-buddy"  # repo polled for the latest release tag
    # [relay] (LAN federation: off by default, zero sockets unless active)
    relay_enabled: bool = False
    relay_port: int = 48901
    relay_token: str = ""  # shared secret; relay stays OFF while empty
    # static peers ("host" or "host:port") get the discovery datagram as
    # unicast too — overlay networks (Tailscale/WireGuard) have no broadcast
    relay_peers: tuple[str, ...] = ()
    relay_name_short: str = ""  # <= 6 chars shown as the [NAME] title prefix
    # [telegram] (push + remote-control bot: off by default, zero network
    # unless active). The bot token is FULL control of the bot — treat it
    # like an SSH key; it is never logged and never printed by the CLI.
    telegram_enabled: bool = False
    telegram_token: str = ""  # BotFather token; telegram stays OFF while empty
    telegram_chats: tuple[int, ...] = ()  # allowlisted chat ids; REQUIRED
    telegram_events: tuple[str, ...] = ("prompt",)  # prompt|attention|done|card|update
    telegram_decisions: bool = False  # inline Allow/Deny buttons answer gates
    telegram_answer_asks: bool = False  # AskUserQuestion answerable from Telegram

    home: Path = field(default_factory=bridge_home)

    @property
    def path(self) -> Path:
        return self.home / CONFIG_FILENAME

    @property
    def relay_active(self) -> bool:
        """The relay requires BOTH the enable flag and a non-empty shared
        token — an unauthenticated LAN listener must be impossible."""
        return self.relay_enabled and bool(self.relay_token.strip())

    @property
    def telegram_active(self) -> bool:
        """Telegram requires the enable flag AND a token AND at least one
        allowlisted chat — an open (answer-anyone) bot must be impossible."""
        return (
            self.telegram_enabled
            and bool(self.telegram_token.strip())
            and bool(self.telegram_chats)
        )


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


def _get_num_opt(table: dict, key: str) -> float | None:
    val = table.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _get_str_list(table: dict, key: str) -> tuple[str, ...]:
    val = table.get(key)
    if not isinstance(val, list):
        return ()
    return tuple(v for v in val if isinstance(v, str) and v.strip())


def _get_int_list(table: dict, key: str) -> tuple[int, ...]:
    val = table.get(key)
    if not isinstance(val, list):
        return ()
    return tuple(v for v in val if isinstance(v, int) and not isinstance(v, bool))


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
    cards = data.get("cards", {}) if isinstance(data.get("cards"), dict) else {}
    relay = data.get("relay", {}) if isinstance(data.get("relay"), dict) else {}
    telegram = data.get("telegram", {}) if isinstance(data.get("telegram"), dict) else {}

    cfg.host_id = _get_str(host, "id", "")
    cfg.host_name = _get_str(host, "name", "")
    cfg.owner = _get_str(host, "owner", "")

    cfg.ble_mode = _get_str(ble, "mode", "exclusive")
    if cfg.ble_mode not in _BLE_MODES:
        cfg.ble_mode = "exclusive"
    cfg.ble_name_prefix = _get_str(ble, "name_prefix", "Claude") or "Claude"
    cfg.ble_address = _get_str(ble, "address", "")
    cfg.idle_yield_min = max(0, _get_int(ble, "idle_yield_min", 0))

    cfg.ipc_port = _get_int(ipc, "port", 0)

    cfg.claude_enabled = _get_bool(claude, "enabled", True)
    cfg.claude_gate = _get_bool(claude, "gate", False)
    cfg.claude_gate_tools = _get_str(claude, "gate_tools", "Bash") or "Bash"

    cfg.codex_enabled = _get_bool(codex, "enabled", True)
    cfg.codex_gate = _get_bool(codex, "gate", True)

    cfg.claude_decision = _get_num(timeouts, "claude_decision", 300.0)
    cfg.codex_decision = _get_num(timeouts, "codex_decision", 105.0)

    cfg.cards_enabled = _get_bool(cards, "enabled", False)
    cfg.cards_github = _get_bool(cards, "github", True)
    cfg.cards_repos = _get_str_list(cards, "repos")
    cfg.cards_weather_lat = _get_num_opt(cards, "weather_lat")
    cfg.cards_weather_lon = _get_num_opt(cards, "weather_lon")
    cfg.cards_update_check = _get_bool(cards, "update_check", True)
    cfg.cards_firmware_repo = (
        _get_str(cards, "firmware_repo", "arikyeo/sticks3-buddy") or "arikyeo/sticks3-buddy"
    )

    cfg.relay_enabled = _get_bool(relay, "enabled", False)
    cfg.relay_port = _get_int(relay, "port", 48901)
    if not (0 < cfg.relay_port < 65536):
        cfg.relay_port = 48901
    cfg.relay_token = _get_str(relay, "token", "")
    cfg.relay_peers = _get_str_list(relay, "peers")
    cfg.relay_name_short = _get_str(relay, "name_short", "")

    cfg.telegram_enabled = _get_bool(telegram, "enabled", False)
    cfg.telegram_token = _get_str(telegram, "bot_token", "")
    cfg.telegram_chats = _get_int_list(telegram, "allowed_chats")
    events = _get_str_list(telegram, "events")
    cfg.telegram_events = events if "events" in telegram else ("prompt",)
    cfg.telegram_decisions = _get_bool(telegram, "allow_decisions", False)
    cfg.telegram_answer_asks = _get_bool(telegram, "answer_asks", False)

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
    repos = ", ".join(_toml_str(r) for r in cfg.cards_repos)
    relay_peers = ", ".join(_toml_str(p) for p in cfg.relay_peers)
    tg_chats = ", ".join(str(c) for c in cfg.telegram_chats)
    tg_events = ", ".join(_toml_str(e) for e in cfg.telegram_events)
    weather = ""
    if cfg.cards_weather_lat is not None and cfg.cards_weather_lon is not None:
        weather = (
            f"weather_lat = {_toml_num(cfg.cards_weather_lat)}\n"
            f"weather_lon = {_toml_num(cfg.cards_weather_lon)}\n"
        )
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
        f"idle_yield_min = {int(cfg.idle_yield_min)}\n"
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
        "\n[cards]\n"
        f"enabled = {b(cfg.cards_enabled)}\n"
        f"github = {b(cfg.cards_github)}\n"
        f"update_check = {b(cfg.cards_update_check)}\n"
        f"firmware_repo = {_toml_str(cfg.cards_firmware_repo)}\n"
        f"repos = [{repos}]\n"
        f"{weather}"
        "\n[relay]\n"
        f"enabled = {b(cfg.relay_enabled)}\n"
        f"port = {cfg.relay_port}\n"
        f"token = {_toml_str(cfg.relay_token)}\n"
        f"peers = [{relay_peers}]\n"
        f"name_short = {_toml_str(cfg.relay_name_short)}\n"
        "\n[telegram]\n"
        f"enabled = {b(cfg.telegram_enabled)}\n"
        f"bot_token = {_toml_str(cfg.telegram_token)}\n"
        f"allowed_chats = [{tg_chats}]\n"
        f"events = [{tg_events}]\n"
        f"allow_decisions = {b(cfg.telegram_decisions)}\n"
        f"answer_asks = {b(cfg.telegram_answer_asks)}\n"
    )


def save_config(cfg: Config) -> Path:
    cfg.home.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.with_name(cfg.path.name + ".tmp")
    tmp.write_text(to_toml(cfg), encoding="utf-8")
    os.replace(tmp, cfg.path)
    return cfg.path
