"""[telegram] config section: parsing, round-trip, and the active gate."""

from buddy_bridge.config import Config, load_config, save_config


def test_defaults_off(home):
    cfg = load_config(home)
    assert cfg.telegram_enabled is False
    assert cfg.telegram_token == ""
    assert cfg.telegram_chats == ()
    assert cfg.telegram_events == ("prompt",)
    assert cfg.telegram_decisions is False
    assert cfg.telegram_answer_asks is False
    assert cfg.telegram_active is False


def test_parse_full_section(home):
    (home / "config.toml").write_text(
        "[telegram]\n"
        'enabled = true\n'
        'bot_token = "123456:ABC-secret"\n'
        "allowed_chats = [111, -100222]\n"
        'events = ["prompt", "card"]\n'
        "allow_decisions = true\n"
        "answer_asks = true\n",
        encoding="utf-8",
    )
    cfg = load_config(home, create=False)
    assert cfg.telegram_enabled is True
    assert cfg.telegram_token == "123456:ABC-secret"
    assert cfg.telegram_chats == (111, -100222)
    assert cfg.telegram_events == ("prompt", "card")
    assert cfg.telegram_decisions is True
    assert cfg.telegram_answer_asks is True
    assert cfg.telegram_active is True


def test_active_requires_token_and_chats(home):
    cfg = Config(home=home, telegram_enabled=True)
    assert cfg.telegram_active is False  # no token, no chats
    cfg.telegram_token = "123:abc"
    assert cfg.telegram_active is False  # still no allowlisted chat
    cfg.telegram_chats = (42,)
    assert cfg.telegram_active is True
    cfg.telegram_enabled = False
    assert cfg.telegram_active is False


def test_bad_values_tolerated(home):
    (home / "config.toml").write_text(
        "[telegram]\n"
        "enabled = 1\n"  # not a bool -> default
        "bot_token = 99\n"  # not a str -> default
        'allowed_chats = ["nope", true, 7]\n'  # only real ints survive
        "events = []\n"  # explicit empty list = push nothing
        "\n[host]\n"
        'id = "abc123"\n',
        encoding="utf-8",
    )
    cfg = load_config(home, create=False)
    assert cfg.telegram_enabled is False
    assert cfg.telegram_token == ""
    assert cfg.telegram_chats == (7,)
    assert cfg.telegram_events == ()


def test_roundtrip_survives_save(home):
    cfg = load_config(home)
    cfg.telegram_enabled = True
    cfg.telegram_token = "42:token-value"
    cfg.telegram_chats = (7, 8)
    cfg.telegram_events = ("prompt", "done")
    cfg.telegram_decisions = True
    cfg.telegram_answer_asks = True
    save_config(cfg)
    again = load_config(home, create=False)
    assert again.telegram_active is True
    assert again.telegram_token == "42:token-value"
    assert again.telegram_chats == (7, 8)
    assert again.telegram_events == ("prompt", "done")
    assert again.telegram_decisions is True
    assert again.telegram_answer_asks is True
