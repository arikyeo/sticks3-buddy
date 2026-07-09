from buddy_bridge.config import Config, bridge_home, load_config, save_config


def test_host_id_minted_once_and_stable(home):
    cfg = load_config(home)
    assert len(cfg.host_id) == 6
    assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in cfg.host_id)
    assert cfg.path.exists()
    cfg2 = load_config(home)
    assert cfg2.host_id == cfg.host_id
    cfg3 = load_config(home)
    assert cfg3.host_id == cfg.host_id


def test_defaults(home):
    cfg = load_config(home)
    assert cfg.ble_mode == "exclusive"
    assert cfg.ble_name_prefix == "Claude"
    assert cfg.ble_address == ""
    assert cfg.ipc_port == 0
    assert cfg.claude_enabled is True
    assert cfg.claude_gate is False
    assert cfg.claude_gate_tools == "Bash"
    assert cfg.codex_enabled is True
    assert cfg.codex_gate is True
    assert cfg.claude_decision == 300.0
    assert cfg.codex_decision == 105.0
    assert cfg.host_name  # hostname filled in


def test_roundtrip(home):
    cfg = load_config(home)
    cfg.owner = "Arik"
    cfg.ble_mode = "ondemand"
    cfg.ble_address = "AA:BB:CC:DD:EE:FF"
    cfg.claude_gate = True
    cfg.claude_gate_tools = "Bash|Write"
    cfg.codex_gate = False
    cfg.claude_decision = 120.0
    save_config(cfg)

    cfg2 = load_config(home)
    assert cfg2.owner == "Arik"
    assert cfg2.ble_mode == "ondemand"
    assert cfg2.ble_address == "AA:BB:CC:DD:EE:FF"
    assert cfg2.claude_gate is True
    assert cfg2.claude_gate_tools == "Bash|Write"
    assert cfg2.codex_gate is False
    assert cfg2.claude_decision == 120.0
    assert cfg2.host_id == cfg.host_id


def test_tolerant_load(home):
    (home / "config.toml").write_text(
        """
[host]
id = "abc123"
name = 42
future_key = "ignored"

[ble]
mode = "warpspeed"

[unknown_section]
x = 1

[timeouts]
claude_decision = "soon"
""",
        encoding="utf-8",
    )
    cfg = load_config(home)
    assert cfg.host_id == "abc123"
    assert cfg.host_name  # bad type replaced with hostname
    assert cfg.ble_mode == "exclusive"  # invalid enum falls back
    assert cfg.claude_decision == 300.0  # bad type falls back


def test_corrupt_config_survives(home):
    (home / "config.toml").write_text("not [valid toml ===", encoding="utf-8")
    cfg = load_config(home)
    assert len(cfg.host_id) == 6


def test_env_home_override(home):
    # `home` fixture sets BUDDY_BRIDGE_HOME
    assert bridge_home() == home
    assert Config().home == home
