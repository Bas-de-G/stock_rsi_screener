"""Tests for the append-only record of what was recommended.

This file exists so "is any of this working?" has an answer. That needs the
verdict as it stood *at the time*, which is exactly what the `signals` table
cannot supply: `_rescore_signals` rewrites every pattern's valuation whenever a
fair value is recorded, so a March signal carries August's numbers and any hit
rate computed from it reads the future.

Offline throughout.
"""

from __future__ import annotations

import csv
import datetime as dt

import pytest

from screener.cli import _journal_recommendations
from screener.journal import Journal, Recommendation, verdict_for
from screener.storage import RsiPoint, Signal, Store, Valuation

NOW = dt.datetime.now()


def _rec(symbol="PTON", horizon="1h", direction="buy", up2="2026-08-19T12:55",
         verdict="strong", **kw) -> Recommendation:
    base = dict(
        decided_at=NOW.isoformat(timespec="seconds"), symbol=symbol, horizon=horizon,
        direction=direction, up2_date=up2, verdict=verdict, fresh=1, price=5.45,
        currency="USD", rsi=33.3, fair_value=7.81, discount=0.434,
        valuation_known=1, valuation_pass=1, earnings_growth=12.0,
        earnings_growth_known=1, earnings_growth_pass=1, earnings_state="clear",
        earnings_sessions=None, margin=0.1, leverage=10,
    )
    base.update(kw)
    return Recommendation(**base)


# ----------------------------------------------------------- the ledger


def test_a_recommendation_is_written_once(tmp_path):
    path = tmp_path / "r.csv"
    journal = Journal(path)
    assert journal.record(_rec()) is True
    assert journal.record(_rec()) is False
    assert journal.added == 1


def test_the_same_pattern_is_not_relogged_on_the_next_run(tmp_path):
    """A signal sits on the page for hours and runs land every 30 minutes."""
    path = tmp_path / "r.csv"
    Journal(path).record(_rec())
    assert Journal(path).record(_rec()) is False
    assert path.read_text().count("PTON") == 1


def test_a_row_is_never_rewritten_when_the_verdict_changes(tmp_path):
    """The whole point. A fair value recorded next week re-scores the signals
    table; the ledger must still say what was published today."""
    path = tmp_path / "r.csv"
    journal = Journal(path)
    journal.record(_rec(verdict="signal", fair_value=None, valuation_known=0))
    journal.record(_rec(verdict="strong", fair_value=7.81))

    rows = list(csv.DictReader(path.open(newline="")))
    assert len(rows) == 1
    assert rows[0]["verdict"] == "signal", "the original verdict stands"


def test_different_patterns_on_one_symbol_are_separate_rows(tmp_path):
    path = tmp_path / "r.csv"
    journal = Journal(path)
    journal.record(_rec(up2="2026-08-19T12:55"))
    journal.record(_rec(up2="2026-08-20T09:30"))
    assert journal.added == 2


def test_a_buy_and_a_sell_on_one_symbol_are_separate_rows(tmp_path):
    """A ticker can carry both at once on different horizons."""
    path = tmp_path / "r.csv"
    journal = Journal(path)
    journal.record(_rec(direction="buy"))
    journal.record(_rec(direction="sell", verdict="sell"))
    assert journal.added == 2


def test_the_same_pattern_on_two_horizons_is_two_rows(tmp_path):
    path = tmp_path / "r.csv"
    journal = Journal(path)
    journal.record(_rec(horizon="1h"))
    journal.record(_rec(horizon="4h"))
    assert journal.added == 2


# ------------------------------------------------------------ the file


def test_the_file_opens_as_a_spreadsheet(tmp_path):
    """It gets committed and read by a human before it is ever read by pandas,
    so the columns are flat rather than a JSON blob."""
    path = tmp_path / "r.csv"
    Journal(path).record(_rec())
    rows = list(csv.DictReader(path.open(newline="")))
    assert rows[0]["symbol"] == "PTON"
    assert rows[0]["verdict"] == "strong"
    assert float(rows[0]["discount"]) == pytest.approx(0.434)
    assert rows[0]["earnings_state"] == "clear"


def test_the_header_is_written_once(tmp_path):
    path = tmp_path / "r.csv"
    journal = Journal(path)
    journal.record(_rec(up2="a"))
    journal.record(_rec(up2="b"))
    assert path.read_text().count("decided_at") == 1


def test_a_corrupt_ledger_does_not_stop_the_run(tmp_path, capsys):
    """Losing a day of evidence is worse than a duplicate row, which analysis
    can drop.

    Undecodable bytes rather than merely malformed text, because that is the
    case that actually raises — and it raises UnicodeDecodeError, which is a
    ValueError and not the csv.Error or OSError you would think to catch.
    """
    path = tmp_path / "r.csv"
    path.write_bytes(bytes(range(256)))
    journal = Journal(path)
    assert journal.record(_rec()) is True
    assert "unreadable" in capsys.readouterr().out


def test_a_merely_malformed_ledger_is_read_as_best_it_can_be(tmp_path):
    """A truncated final row is not corruption the csv module minds, and the
    keys it does recover still dedupe correctly."""
    path = tmp_path / "r.csv"
    Journal(path).record(_rec())
    path.write_text(path.read_text().rstrip("\n") + "\nPTON,1h")  # torn write
    assert Journal(path).record(_rec()) is False


def test_an_empty_value_round_trips_as_empty(tmp_path):
    path = tmp_path / "r.csv"
    Journal(path).record(_rec(fair_value=None, discount=None, earnings_sessions=None))
    rows = list(csv.DictReader(path.open(newline="")))
    assert rows[0]["fair_value"] == ""
    assert rows[0]["earnings_sessions"] == ""


# ----------------------------------------------------------- the verdict


def test_a_confirmed_buy_is_strong():
    assert verdict_for(None, "buy", strong=True, suspended=False) == "strong"


def test_a_confirmed_sell_is_its_own_label():
    assert verdict_for(None, "sell", strong=True, suspended=False) == "sell_strong"


def test_suspension_beats_everything():
    """What was actually published, not what would have been."""
    assert verdict_for(None, "buy", strong=True, suspended=True) == "suspended"


# ------------------------------------------------- wired into the run


@pytest.fixture()
def config(tmp_path):
    from screener.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: PTON, tradingview: "NASDAQ:PTON", morningstar: xnas/pton, markets: [nasdaq]}}
rsi: {{period: 14, threshold: 30, overbought: 70, interval: "1D"}}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage:
  database: "{tmp_path / 't.db'}"
  csv_dir: "{tmp_path}"
  fair_values: "{tmp_path / 'fv.yaml'}"
  notifications: "{tmp_path / 'n.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
""")
    return load_config(path)


def _seed(store, rsi=33.3, direction="buy", reports_in_days=None):
    stamps = [(NOW - dt.timedelta(hours=30 - i)).isoformat(timespec="minutes") for i in range(30)]
    for horizon in ("1h", "1d"):
        for stamp in stamps:
            label = stamp if horizon == "1h" else stamp[:10]
            store.upsert_rsi_point(RsiPoint("PTON", label, 5.45, rsi, "test", horizon=horizon))
    store.record_signal(Signal(
        "PTON", stamps[0], stamps[10], stamps[-2], 5.45, 7.81,
        True, True, True, "now", horizon="1h", direction=direction,
    ))
    store.upsert_valuation(Valuation("PTON", "2026-08-10", 5.45, 7.81, "2026-08-10", "manual"))
    if reports_in_days is not None:
        release = dt.date.today() + dt.timedelta(days=reports_in_days)
        store.upsert_earnings("PTON", release.isoformat(), None, None)


def test_a_published_verdict_lands_in_the_ledger(config):
    with Store(config.storage.database) as store:
        _seed(store)
        assert _journal_recommendations(store, config) == 1
        assert _journal_recommendations(store, config) == 0

    rows = list(csv.DictReader(config.storage.recommendations.open(newline="")))
    assert rows[0]["symbol"] == "PTON"
    assert rows[0]["verdict"] == "strong"
    assert rows[0]["horizon"] == "1h"


def test_a_suspended_verdict_is_recorded_too(config):
    """Nobody was told about it, and that is exactly why it has to be on the
    record — otherwise "did suspending them help?" has no sample."""
    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=1)
        assert _journal_recommendations(store, config) == 1

    rows = list(csv.DictReader(config.storage.recommendations.open(newline="")))
    assert rows[0]["verdict"] == "suspended"
    assert rows[0]["earnings_state"] == "before"
    assert rows[0]["earnings_sessions"] != ""


def test_a_sell_is_recorded_as_a_sell(config):
    with Store(config.storage.database) as store:
        _seed(store, rsi=65.0, direction="sell")
        assert _journal_recommendations(store, config) == 1

    rows = list(csv.DictReader(config.storage.recommendations.open(newline="")))
    assert rows[0]["direction"] == "sell"
    assert rows[0]["verdict"] == "sell_strong"


def test_nothing_is_logged_when_nothing_fired(config):
    with Store(config.storage.database) as store:
        assert _journal_recommendations(store, config) == 0
    assert not config.storage.recommendations.exists()


def test_the_ledger_outlives_the_database(config):
    """The failure mode `notifications.json` was moved out of the database to
    escape: CI only commits the database on the last run of the day, so an
    intraday run's writes are gone by the next one."""
    with Store(config.storage.database) as store:
        _seed(store)
        assert _journal_recommendations(store, config) == 1

    config.storage.database.unlink()
    with Store(config.storage.database) as fresh:
        _seed(fresh)
        assert _journal_recommendations(fresh, config) == 0, "already on the record"

    rows = list(csv.DictReader(config.storage.recommendations.open(newline="")))
    assert len(rows) == 1
