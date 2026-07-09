import json
import time

from buddy_bridge.usage import UsageLedger, day_key


def _ts(y, m, d, hh, mm):
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


def test_day_key_is_local_calendar_date():
    assert day_key(_ts(2026, 7, 9, 12, 0)) == "2026-07-09"
    assert day_key(_ts(2026, 7, 9, 23, 59)) == "2026-07-09"
    assert day_key(_ts(2026, 7, 10, 0, 1)) == "2026-07-10"


def test_midnight_rollover(tmp_path, monkeypatch):
    now = {"t": _ts(2026, 7, 9, 23, 50)}
    ledger = UsageLedger(tmp_path / "ledger.json", now_fn=lambda: now["t"])

    assert ledger.add("claude", "s1", 100) == 100
    now["t"] = _ts(2026, 7, 9, 23, 59)
    ledger.add("claude", "s1", 50)
    assert ledger.totals() == (150, 150)

    # cross local midnight: today resets, lifetime keeps counting
    now["t"] = _ts(2026, 7, 10, 0, 1)
    assert ledger.totals() == (150, 0)  # rollover happens even before any add
    ledger.add("claude", "s1", 30)
    assert ledger.totals() == (180, 30)
    assert ledger.session_tokens("claude", "s1") == 180


def test_rollover_persists_across_reload(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = UsageLedger(path, now_fn=lambda: _ts(2026, 7, 9, 22, 0))
    ledger.add("claude", "s1", 500)
    ledger.save()

    reloaded = UsageLedger(path, now_fn=lambda: _ts(2026, 7, 9, 23, 0))
    assert reloaded.totals() == (500, 500)  # same day: today survives restart

    next_day = UsageLedger(path, now_fn=lambda: _ts(2026, 7, 10, 9, 0))
    assert next_day.totals() == (500, 0)  # restart across midnight: today reset


def test_add_cumulative_baselines(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger.json", now_fn=lambda: _ts(2026, 7, 9, 10, 0))
    # first sighting only re-baselines: history before the bridge existed
    assert ledger.add_cumulative("codex", "c1", 1000) == 0
    assert ledger.totals() == (0, 0)
    assert ledger.add_cumulative("codex", "c1", 1250) == 250
    assert ledger.add_cumulative("codex", "c1", 1250) == 0  # no growth
    # counter went backwards (session restart): re-baseline, count nothing
    assert ledger.add_cumulative("codex", "c1", 40) == 0
    assert ledger.add_cumulative("codex", "c1", 100) == 60
    assert ledger.totals() == (310, 310)
    assert ledger.session_tokens("codex", "c1") == 310


def test_tolerant_load(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "lifetime": 42,
                "tokens_today": "many",  # bad type -> default
                "day": "2026-07-09",
                "sessions": {"claude:s1": 40, "bad": "x"},
                "baselines": {"codex:c1": 7},
                "some_future_key": {"ignored": True},
            }
        ),
        encoding="utf-8",
    )
    ledger = UsageLedger(path, now_fn=lambda: _ts(2026, 7, 9, 10, 0))
    assert ledger.lifetime == 42
    assert ledger.tokens_today == 0
    assert ledger.sessions == {"claude:s1": 40}
    assert ledger.baselines == {"codex:c1": 7}

    path.write_text("not json at all", encoding="utf-8")
    fresh = UsageLedger(path)
    assert fresh.totals()[0] == 0


def test_save_if_dirty(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = UsageLedger(path)
    assert ledger.save_if_dirty() is False
    ledger.add("claude", "s", 5)
    assert ledger.save_if_dirty() is True
    assert ledger.save_if_dirty() is False
    assert json.loads(path.read_text(encoding="utf-8"))["lifetime"] == 5
