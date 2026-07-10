"""Firmware update-check card: GitHub releases/latest poll, semver-ish
compare (garbage tolerant), per-tag dedupe, daemon wiring. No live
network — the default urllib fetch is exercised once with ``urlopen``
mocked; everywhere else a fake fetcher is injected, same convention as
the github/weather card pollers."""

import asyncio
import contextlib
import json

from buddy_bridge.adapters.update_cards import (
    DEFAULT_FIRMWARE_REPO,
    UpdateCardsPoller,
    _default_fetch_sync,
    is_newer,
    latest_tag,
    parse_version,
)
from buddy_bridge.cards import KIND_UPDATE
from buddy_bridge.config import load_config, save_config
from buddy_bridge.daemon import Daemon

# ---- version compare ----


def test_parse_version_semverish():
    assert parse_version("v2.1.3") == (2, 1, 3)
    assert parse_version("2.1.3") == (2, 1, 3)
    assert parse_version("V2.1") == (2, 1)
    assert parse_version("2") == (2,)
    assert parse_version("2.1.3-rc1") == (2, 1, 3)
    assert parse_version("2.1.3+build5") == (2, 1, 3)


def test_parse_version_garbage_tolerant():
    assert parse_version("latest") is None
    assert parse_version("") is None
    assert parse_version("v") is None
    assert parse_version(None) is None
    assert parse_version(123) is None


def test_is_newer_component_wise_not_lexical():
    assert is_newer("v2.1.0", "2.0.9")
    assert is_newer("v2.10.0", "2.9.0")  # numeric: 10 > 9, not "10" < "9"
    assert not is_newer("v2.0.9", "2.1.0")
    assert not is_newer("v2.1.0", "2.1.0")  # equal is not "newer"
    assert not is_newer("v2.1", "2.1.0")  # missing components pad to 0: equal


def test_is_newer_garbage_never_triggers_update():
    assert not is_newer("latest", "2.1.0")  # unparseable remote tag
    assert not is_newer("v2.1.0", "unknown")  # unparseable/garbage device fw
    assert not is_newer("", "")


def test_latest_tag_extraction_tolerant():
    assert latest_tag({"tag_name": "v3.0.0"}) == "v3.0.0"
    assert latest_tag({"tag_name": 5}) == ""  # wrong type
    assert latest_tag({"message": "API rate limit exceeded"}) == ""  # error body shape
    assert latest_tag(None) == ""
    assert latest_tag("garbage") == ""


def test_default_repo_constant():
    assert DEFAULT_FIRMWARE_REPO == "arikyeo/sticks3-buddy"


# ---- poller: dedupe + fw-unknown skip ----


async def test_poller_emits_once_then_dedupes_same_tag():
    async def fetch(repo):
        return {"tag_name": "v2.1.0"}

    emitted = []
    poller = UpdateCardsPoller(emitted.append, get_device_fw=lambda: "2.0.0", fetch=fetch)
    await poller.poll_once()
    await poller.poll_once()
    await poller.poll_once()
    assert len(emitted) == 1
    card = emitted[0]
    assert card.kind == KIND_UPDATE
    assert card.title == "fw 2.1.0 available"
    assert card.body == "menu > update to install"
    assert card.ts > 0


async def test_poller_cards_again_only_on_a_newer_tag():
    tags = ["v2.1.0", "v2.1.0", "v2.2.0"]

    async def fetch(repo):
        return {"tag_name": tags.pop(0)}

    emitted = []
    poller = UpdateCardsPoller(emitted.append, get_device_fw=lambda: "2.0.0", fetch=fetch)
    await poller.poll_once()
    await poller.poll_once()  # same tag: deduped, no second card
    await poller.poll_once()  # new tag: cards again
    assert [c.title for c in emitted] == ["fw 2.1.0 available", "fw 2.2.0 available"]


async def test_poller_silent_when_device_already_current_or_ahead():
    async def fetch(repo):
        return {"tag_name": "v2.0.0"}

    emitted = []
    poller = UpdateCardsPoller(emitted.append, get_device_fw=lambda: "2.0.0", fetch=fetch)
    await poller.poll_once()
    assert emitted == []


async def test_poller_skips_fetch_when_device_fw_unknown():
    calls = []

    async def fetch(repo):
        calls.append(repo)
        return {"tag_name": "v9.9.9"}

    emitted = []
    poller = UpdateCardsPoller(emitted.append, get_device_fw=lambda: "", fetch=fetch)
    await poller.poll_once()
    assert emitted == []
    assert calls == []  # never touches the network with nothing to compare against


async def test_poller_uses_configured_repo():
    seen = []

    async def fetch(repo):
        seen.append(repo)
        return {"tag_name": "v1.0.0"}

    poller = UpdateCardsPoller(
        lambda _card: None, get_device_fw=lambda: "0.9.0", repo="someone/fork", fetch=fetch
    )
    await poller.poll_once()
    assert seen == ["someone/fork"]


async def test_poller_no_release_payload_is_silent():
    async def fetch(repo):
        return None  # fetch failure / bad JSON

    emitted = []
    poller = UpdateCardsPoller(emitted.append, get_device_fw=lambda: "1.0.0", fetch=fetch)
    await poller.poll_once()
    assert emitted == []


# ---- default fetch: urllib mocked, no live network ----


def test_default_fetch_sync_parses_via_urllib(monkeypatch):
    import urllib.request

    calls = []

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResp(json.dumps({"tag_name": "v9.9.9"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = _default_fetch_sync("arikyeo/sticks3-buddy")
    assert payload == {"tag_name": "v9.9.9"}
    assert calls == ["https://api.github.com/repos/arikyeo/sticks3-buddy/releases/latest"]


def test_default_fetch_sync_swallows_errors(monkeypatch):
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _default_fetch_sync("arikyeo/sticks3-buddy") is None


# ---- config ----


def test_update_check_config_defaults_and_roundtrip(home):
    cfg = load_config(home)
    assert cfg.cards_update_check is True  # on whenever [cards] enabled
    assert cfg.cards_firmware_repo == "arikyeo/sticks3-buddy"

    cfg.cards_update_check = False
    cfg.cards_firmware_repo = "someone/fork"
    save_config(cfg)
    cfg2 = load_config(home)
    assert cfg2.cards_update_check is False
    assert cfg2.cards_firmware_repo == "someone/fork"


# ---- daemon wiring: gated on cards_enabled AND cards_update_check ----


async def test_update_poller_started_only_when_enabled(home):
    cfg = load_config(home)
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    daemon = Daemon(cfg)
    assert await daemon.start_background() == []  # cards disabled by default: no task

    cfg.cards_enabled = True  # cards_update_check defaults True
    tasks = await daemon.start_background()
    names = sorted(t.get_name() for t in tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "update-cards" in names

    cfg.cards_update_check = False
    tasks2 = await daemon.start_background()
    names2 = sorted(t.get_name() for t in tasks2)
    for task in tasks2:
        task.cancel()
    for task in tasks2:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "update-cards" not in names2


async def test_device_fw_version_exposes_last_hello_ack(home):
    daemon = Daemon(load_config(home))
    assert daemon.device_fw_version() == ""  # nothing negotiated yet
    await daemon._on_hello_ack(
        {
            "ack": "hello",
            "ok": True,
            "proto": 2,
            "data": {"fw": "2.3.1", "maxLine": 4608, "maxSessions": 6, "sel": True},
        }
    )
    assert daemon.device_fw_version() == "2.3.1"
