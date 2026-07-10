"""Info cards: ntfy composer, dedupe, log rotation, pollers (no live
network — gh runs and weather fetches are injected), daemon gating."""

import asyncio
import contextlib
import json

from buddy_bridge.adapters.github_cards import (
    GithubCardsPoller,
    latest_run,
    notification_cards,
)
from buddy_bridge.adapters.weather_cards import WeatherCardsPoller, parse_current
from buddy_bridge.cards import (
    CARD_QUEUE_MAX,
    Card,
    CardDeduper,
    CardLog,
    CardPipeline,
    build_ntfy_line,
    clean_card,
)
from buddy_bridge.config import load_config, save_config
from buddy_bridge.daemon import Daemon


# ---- composer ----


def test_ntfy_line_spec_shape():
    line = build_ntfy_line(
        Card(kind="gh", title="PR #42 merged", body="sticks3-buddy: track-i landed",
             ts=1775731234)
    )
    assert json.loads(line) == {
        "evt": "ntfy",
        "kind": "gh",
        "title": "PR #42 merged",
        "body": "sticks3-buddy: track-i landed",
        "ts": 1775731234,
    }


def test_ntfy_sanitized_and_capped():
    card = clean_card(Card(kind="gh", title="T—itle" + "x" * 40, body="日本語" * 60, ts=5))
    assert len(card.title.encode()) <= 24
    assert card.title.startswith("T-itle")  # em dash mapped
    assert len(card.body.encode()) <= 80
    assert all(ord(c) < 128 for c in card.body)
    stamped = clean_card(Card(kind="gh", title="t"))
    assert stamped.ts > 0  # ts filled when missing


def test_ntfy_budget_degrades_body_first():
    line = build_ntfy_line(Card(kind="ci", title="t" * 24, body="b" * 80, ts=1), budget=120)
    assert len(line.encode()) <= 120
    evt = json.loads(line)
    assert evt["title"] == "t" * 24  # body shrank first
    assert len(evt["body"]) < 80


# ---- dedupe + log + pipeline ----


def test_deduper_ttl():
    now = [1000.0]
    dedupe = CardDeduper(ttl=100.0, now_fn=lambda: now[0])
    card = Card(kind="gh", title="t", body="b", ts=1)
    assert dedupe.accept(card)
    assert not dedupe.accept(card)  # repeat suppressed
    assert dedupe.accept(Card(kind="gh", title="t", body="other", ts=1))
    now[0] += 101.0
    assert dedupe.accept(card)  # TTL expired: same content passes again


def test_card_log_rotates_once(home):
    log = CardLog(home / "cards.log", max_bytes=200)
    for i in range(20):
        log.append(Card(kind="gh", title=f"card {i}", body="x" * 40, ts=1_700_000_000))
    assert (home / "cards.log").exists()
    assert (home / "cards.log.1").exists()
    assert (home / "cards.log").stat().st_size < 400  # rotated, not unbounded
    text = (home / "cards.log").read_text(encoding="utf-8")
    assert "[gh] card 19" in text


def test_pipeline_dedupes_logs_and_bounds(home):
    pipeline = CardPipeline(deduper=CardDeduper(), log=CardLog(home / "cards.log"))
    assert pipeline.submit(Card(kind="gh", title="t", body="b", ts=1))
    assert not pipeline.submit(Card(kind="gh", title="t", body="b", ts=2))  # dupe
    for i in range(CARD_QUEUE_MAX + 5):
        pipeline.submit(Card(kind="ci", title=f"c{i}", body="", ts=1))
    assert len(pipeline.queue) == CARD_QUEUE_MAX  # bounded, oldest dropped
    assert pipeline.pop().title != "t"  # the very first card fell off
    assert (home / "cards.log").read_text(encoding="utf-8").count("\n") >= CARD_QUEUE_MAX


# ---- github poller ----


NOTIFICATIONS = [
    {
        "unread": True,
        "updated_at": "2026-07-10T03:04:05Z",
        "subject": {"title": "Fix the flux capacitor", "type": "PullRequest"},
        "repository": {"name": "sticks3-buddy", "full_name": "arikyeo/sticks3-buddy"},
    },
    {
        "unread": False,  # read: skipped
        "subject": {"title": "old news"},
        "repository": {"name": "other"},
    },
]


def test_notification_cards_parse_and_filter():
    cards = notification_cards(NOTIFICATIONS)
    assert len(cards) == 1
    assert cards[0].kind == "gh"
    assert cards[0].title == "sticks3-buddy"
    assert cards[0].body == "Fix the flux capacitor"
    assert cards[0].ts > 0
    assert notification_cards("garbage") == []
    assert notification_cards([{"unread": True}])[0].body == "notification"


def test_latest_run_picks_first_completed():
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "success", "displayTitle": "ci: build"},
    ]
    assert latest_run(runs)["conclusion"] == "success"
    assert latest_run([]) is None
    assert latest_run(None) is None


async def test_github_poller_emits_and_tracks_ci_changes():
    emitted = []
    calls = []

    async def fake_gh(args):
        calls.append(list(args))
        if args[0] == "api":
            return 0, json.dumps(NOTIFICATIONS)
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "failure",
              "displayTitle": "ci: tests", "updatedAt": "2026-07-10T00:00:00Z"}]
        )

    poller = GithubCardsPoller(
        emitted.append, repos=("arikyeo/sticks3-buddy",), run_gh=fake_gh
    )
    await poller.poll_once()
    assert [c.kind for c in emitted] == ["gh", "ci"]
    assert emitted[1].title == "sticks3-buddy"
    assert emitted[1].body.startswith("failure:")

    await poller.poll_once()  # same conclusion -> no new ci card
    assert [c.kind for c in emitted] == ["gh", "ci", "gh"]
    assert calls[0][:2] == ["api", "notifications"]
    assert calls[1][:2] == ["run", "list"]


async def test_github_poller_gh_missing_disables():
    emitted = []

    async def missing_gh(args):
        return None  # FileNotFoundError path

    poller = GithubCardsPoller(emitted.append, repos=("a/b",), run_gh=missing_gh)
    await poller.poll_once()
    assert poller.disabled
    assert emitted == []
    # run() exits promptly once disabled
    await asyncio.wait_for(poller.run(), timeout=1.0)


async def test_github_poller_unauth_skips_cycle_but_stays_enabled():
    async def unauth_gh(args):
        return 1, ""  # gh present but errored (e.g. not logged in)

    emitted = []
    poller = GithubCardsPoller(emitted.append, repos=("a/b",), run_gh=unauth_gh)
    await poller.poll_once()
    assert not poller.disabled
    assert emitted == []


# ---- weather poller ----


def _meteo(temp, code):
    return {"current": {"temperature_2m": temp, "weather_code": code}}


def test_parse_current_tolerant():
    assert parse_current(_meteo(21.4, 3)) == (21.4, 3)
    assert parse_current(_meteo(21.4, "x")) == (21.4, -1)
    assert parse_current({"current": {"weather_code": 3}}) is None
    assert parse_current({}) is None
    assert parse_current(None) is None


async def test_weather_poller_first_then_significant_changes_only():
    readings = [_meteo(20.0, 0), _meteo(20.9, 0), _meteo(20.9, 61), _meteo(18.0, 61)]
    emitted = []

    async def fake_fetch(lat, lon):
        return readings.pop(0)

    poller = WeatherCardsPoller(emitted.append, lat=1.35, lon=103.8, fetch=fake_fetch)
    await poller.poll_once()  # first reading -> card
    await poller.poll_once()  # +0.9C, same code -> silent
    await poller.poll_once()  # code flips to rain -> card
    await poller.poll_once()  # -2.9C -> card
    assert [c.body for c in emitted] == ["20C clear", "21C rain", "18C rain"]
    assert all(c.kind == "weather" and c.title == "Weather" for c in emitted)


# ---- config ----


def test_cards_config_defaults_and_roundtrip(home):
    cfg = load_config(home)
    assert cfg.cards_enabled is False  # off by default: no network
    assert cfg.cards_github is True
    assert cfg.cards_repos == ()
    assert cfg.cards_weather_lat is None and cfg.cards_weather_lon is None

    cfg.cards_enabled = True
    cfg.cards_repos = ("arikyeo/sticks3-buddy", "arikyeo/other")
    cfg.cards_weather_lat = 1.3521
    cfg.cards_weather_lon = 103.8198
    save_config(cfg)
    cfg2 = load_config(home)
    assert cfg2.cards_enabled is True
    assert cfg2.cards_repos == ("arikyeo/sticks3-buddy", "arikyeo/other")
    assert cfg2.cards_weather_lat == 1.3521
    assert cfg2.cards_weather_lon == 103.8198


# ---- daemon wiring ----


class FakeLink:
    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.mode = "exclusive"
        self.backoff_floor = 0.0

    @property
    def connected(self):
        return self._connected

    async def send_line(self, line):
        self.sent.append(line)
        return self._connected

    async def stop(self):
        pass


NTFY_ACK = {
    "ack": "hello",
    "ok": True,
    "proto": 2,
    "data": {"caps": ["sessions", "rxack", "ntfy"], "maxLine": 4608, "sel": True},
}


async def test_cards_flush_only_with_ntfy_cap(home):
    daemon = Daemon(load_config(home))
    daemon.link = FakeLink()
    assert daemon.submit_card(Card(kind="gh", title="hello", body="world", ts=1))

    # v1 link: card stays queued
    await daemon._flush_cards()
    assert not [x for x in daemon.link.sent if "ntfy" in x]
    assert len(daemon.cards.queue) == 1

    # v2 but no ntfy cap: still queued
    await daemon._on_ble_connect()
    await daemon._on_ble_line(
        {"ack": "hello", "ok": True, "proto": 2, "data": {"caps": ["sessions"], "sel": True}}
    )
    await daemon._flush_cards()
    assert not [x for x in daemon.link.sent if '"evt":"ntfy"' in x]

    # ntfy negotiated: one card per flush
    await daemon._on_ble_connect()
    await daemon._on_ble_line(NTFY_ACK)
    assert daemon.submit_card(Card(kind="ci", title="second", body="", ts=2))
    await daemon._flush_cards()
    await daemon._flush_cards()
    ntfy = [json.loads(x) for x in daemon.link.sent if '"evt":"ntfy"' in x]
    assert [n["title"] for n in ntfy] == ["hello", "second"]
    assert (home / "cards.log").exists()  # logged locally, separate from audit
    assert not (home / "audit.jsonl").exists()


async def test_cards_dedupe_through_daemon(home):
    daemon = Daemon(load_config(home))
    daemon.link = FakeLink()
    assert daemon.submit_card(Card(kind="gh", title="same", body="thing", ts=1))
    assert not daemon.submit_card(Card(kind="gh", title="same", body="thing", ts=99))
    assert len(daemon.cards.queue) == 1


async def test_card_pollers_started_only_when_enabled(home):
    cfg = load_config(home)
    cfg.claude_enabled = False
    cfg.codex_enabled = False
    daemon = Daemon(cfg)
    daemon.link = FakeLink()
    assert await daemon.start_background() == []  # cards disabled by default

    cfg.cards_enabled = True
    cfg.cards_repos = ("a/b",)
    cfg.cards_weather_lat = 1.0
    cfg.cards_weather_lon = 2.0
    tasks = await daemon.start_background()
    names = sorted(t.get_name() for t in tasks)
    # cancel before the pollers get a chance to run (no subprocess/network)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert names == ["github-cards", "weather-cards"]
