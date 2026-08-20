"""Tests for persistence: upserts, dedup, and the live-over-backfill rule."""

from __future__ import annotations

import pytest

import sqlite3

from screener.storage import (
    RsiPoint,
    Signal,
    Store,
    Valuation,
    append_signal_csv,
    export_csv_snapshot,
)


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def test_rsi_roundtrip_is_ordered_by_date(store):
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-03", 100.0, 45.0, "backfill:yahoo"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 98.0, 40.0, "backfill:yahoo"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-02", 99.0, 42.0, "backfill:yahoo"))
    assert [p.date for p in store.rsi_series("NVDA")] == [
        "2026-01-01", "2026-01-02", "2026-01-03",
    ]


def test_rerunning_the_same_day_updates_rather_than_duplicates(store):
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 40.0, "live:tradingview"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 101.0, 44.0, "live:tradingview"))
    series = store.rsi_series("NVDA")
    assert len(series) == 1
    assert series[0].rsi == 44.0


def test_backfill_never_overwrites_a_live_reading(store):
    """A later backfill must not downgrade TradingView's own number."""
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 43.1, "backfill:yahoo"))
    point = store.rsi_series("NVDA")[0]
    assert point.rsi == 44.0
    assert point.source == "live:tradingview"


def test_live_reading_does_overwrite_a_backfilled_one(store):
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 43.1, "backfill:yahoo"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview"))
    assert store.rsi_series("NVDA")[0].source == "live:tradingview"


def test_valuation_roundtrip(store):
    store.upsert_valuation(
        Valuation("IBM", "2026-07-27", 217.24, 225.0, "Jul 23, 2026", "Medium", "Narrow")
    )
    val = store.valuation("IBM", "2026-07-27")
    assert val.price == 217.24
    assert val.fair_value == 225.0
    assert val.moat == "Narrow"


def test_valuation_missing_day_returns_none(store):
    assert store.valuation("IBM", "1999-01-01") is None


def test_latest_valuation_is_the_most_recent_per_symbol(store):
    store.upsert_valuation(Valuation("IBM", "2026-07-25", 210.0, 225.0, None, None, None))
    store.upsert_valuation(Valuation("IBM", "2026-07-27", 217.24, 225.0, None, None, None))
    store.upsert_valuation(Valuation("NVDA", "2026-07-26", 196.0, 250.0, None, None, None))
    latest = {v.symbol: v for v in store.latest_valuations()}
    assert latest["IBM"].date == "2026-07-27"
    assert latest["IBM"].price == 217.24
    assert latest["NVDA"].date == "2026-07-26"


def test_signals_are_recorded_once_per_pattern(store):
    sig = Signal("NVDA", "2026-01-01", "2026-01-03", "2026-01-05",
                 200.0, 190.0, True, True, True, "now")
    store.record_signal(sig)
    store.record_signal(sig)
    assert len(store.all_signals("NVDA")) == 1
    assert store.signal_exists("NVDA", "2026-01-05")
    assert not store.signal_exists("NVDA", "2026-01-06")


def test_recording_a_fair_value_can_promote_a_pattern_to_a_signal(store):
    """A pattern logged before anyone checked Morningstar must be updatable
    in place — not duplicated into a second row for the same pattern."""
    store.record_signal(
        Signal("IBM", "2026-07-16", "2026-07-22", "2026-07-23",
               None, None, False, False, False, "now")
    )
    store.update_signal_valuation("IBM", "2026-07-23", 217.20, 225.00, True, True, True)

    signals = store.all_signals("IBM")
    assert len(signals) == 1
    assert signals[0].fired is True
    assert signals[0].valuation_known is True
    assert signals[0].fair_value == 225.00


def test_a_contradicting_valuation_can_leave_a_signal_firing(store):
    """Fair value grades a signal; with lenient firing it doesn't cancel it."""
    store.record_signal(
        Signal("TSLA", "2026-07-16", "2026-07-22", "2026-07-23",
               None, None, False, False, True, "now")
    )
    # known=True, confirms=False (above fair value), but still fired.
    store.update_signal_valuation("TSLA", "2026-07-23", 307.0, 280.0, True, False, True)
    sig = store.all_signals("TSLA")[0]
    assert sig.fired is True
    assert sig.valuation_known is True
    assert sig.valuation_pass is False


def test_manual_valuation_roundtrips_its_source(store):
    store.upsert_valuation(Valuation("IBM", "2026-07-27", 217.2, 225.0, source="manual"))
    assert store.valuation("IBM", "2026-07-27").source == "manual"


def test_valuation_source_defaults_to_morningstar(store):
    store.upsert_valuation(Valuation("IBM", "2026-07-27", 217.2, 225.0))
    assert store.valuation("IBM", "2026-07-27").source == "morningstar"


def test_symbols_covers_both_tables(store):
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview"))
    store.upsert_valuation(Valuation("IBM", "2026-01-01", 210.0, 225.0, None, None, None))
    assert store.symbols() == ["IBM", "NVDA"]


def test_schema_survives_reopening_the_same_file(tmp_path):
    path = tmp_path / "persist.db"
    with Store(path) as s:
        s.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview"))
    with Store(path) as s:
        assert len(s.rsi_series("NVDA")) == 1


# ------------------------------------------------- earnings growth roundtrip


def test_earnings_growth_roundtrips(store):
    store.upsert_rsi_point(
        RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview", 32.6, "ttm")
    )
    point = store.rsi_series("NVDA")[0]
    assert (point.earnings_growth, point.earnings_growth_period) == (32.6, "ttm")


def test_earnings_growth_defaults_to_unknown(store):
    """Backfilled rows have no source for this -- must not silently become 0."""
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "backfill:yahoo"))
    point = store.rsi_series("NVDA")[0]
    assert (point.earnings_growth, point.earnings_growth_period) == (None, None)


def test_a_live_update_can_add_earnings_growth_to_a_backfilled_day(store):
    """Same date, first written by backfill (no growth data), then overwritten
    by a live run the day it happens to run -- earnings growth should attach."""
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "backfill:yahoo"))
    store.upsert_rsi_point(
        RsiPoint("NVDA", "2026-01-01", 101.0, 46.0, "live:tradingview", 10.0, "fy")
    )
    point = store.rsi_series("NVDA")[0]
    assert (point.earnings_growth, point.earnings_growth_period) == (10.0, "fy")


# --------------------------------------------- signal earnings-growth fields


def test_signal_earnings_growth_roundtrips(store):
    sig = Signal(
        "NVDA", "2026-01-01", "2026-01-03", "2026-01-05",
        200.0, 190.0, True, True, True, "now",
        earnings_growth=32.6, earnings_growth_known=True, earnings_growth_pass=True,
    )
    store.record_signal(sig)
    stored = store.all_signals("NVDA")[0]
    assert stored.earnings_growth == 32.6
    assert stored.earnings_growth_known is True
    assert stored.earnings_growth_pass is True


def test_signal_earnings_growth_defaults_to_unknown(store):
    """Old-style positional Signal(...) construction must keep working."""
    sig = Signal("NVDA", "2026-01-01", "2026-01-03", "2026-01-05",
                 200.0, 190.0, True, True, True, "now")
    store.record_signal(sig)
    stored = store.all_signals("NVDA")[0]
    assert stored.earnings_growth is None
    assert stored.earnings_growth_known is False
    assert stored.earnings_growth_pass is False


def test_update_signal_valuation_does_not_touch_earnings_growth(store):
    """Re-scoring a signal against a newly-checked fair value must not
    clobber an earnings-growth snapshot captured independently."""
    store.record_signal(Signal(
        "IBM", "2026-07-16", "2026-07-22", "2026-07-23",
        None, None, False, False, False, "now",
        earnings_growth=15.0, earnings_growth_known=True, earnings_growth_pass=True,
    ))
    store.update_signal_valuation("IBM", "2026-07-23", 217.20, 225.00, True, True, True)
    stored = store.all_signals("IBM")[0]
    assert stored.earnings_growth == 15.0
    assert stored.earnings_growth_known is True
    assert stored.earnings_growth_pass is True


# ------------------------------------------------------------- migrations


def test_migrating_an_old_rsi_history_table_adds_earnings_columns(tmp_path):
    """A database from before this factor existed has no earnings columns at
    all; opening it with the current Store must add them without losing rows."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE rsi_history (
             symbol TEXT NOT NULL, date TEXT NOT NULL,
             close REAL NOT NULL, rsi REAL NOT NULL, source TEXT NOT NULL,
             PRIMARY KEY (symbol, date)
           );"""
    )
    conn.execute(
        "INSERT INTO rsi_history VALUES (?, ?, ?, ?, ?)",
        ("NVDA", "2026-01-01", 100.0, 44.0, "backfill:yahoo"),
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        points = s.rsi_series("NVDA")
        assert len(points) == 1
        assert points[0].close == 100.0
        assert points[0].earnings_growth is None


def test_migrating_an_old_signals_table_adds_earnings_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE signals (
             symbol TEXT NOT NULL, up1_date TEXT NOT NULL, down_date TEXT NOT NULL,
             up2_date TEXT NOT NULL, price REAL, fair_value REAL,
             valuation_known INTEGER NOT NULL, valuation_pass INTEGER NOT NULL,
             fired INTEGER NOT NULL, recorded_at TEXT NOT NULL,
             PRIMARY KEY (symbol, up2_date)
           );"""
    )
    conn.execute(
        "INSERT INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("NVDA", "2026-01-01", "2026-01-03", "2026-01-05", 200.0, 190.0, 1, 1, 1, "now"),
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        signals = s.all_signals("NVDA")
        assert len(signals) == 1
        assert signals[0].fired is True
        assert signals[0].earnings_growth_known is False
        assert signals[0].earnings_growth_pass is False


# ------------------------------------------------------------- CSV export


def test_signal_csv_includes_earnings_growth(tmp_path):
    sig = Signal(
        "NVDA", "2026-01-01", "2026-01-03", "2026-01-05",
        200.0, 190.0, True, True, True, "now",
        earnings_growth=32.6, earnings_growth_known=True, earnings_growth_pass=True,
    )
    path = append_signal_csv(tmp_path, sig)
    text = path.read_text()
    assert "earnings_growth" in text.splitlines()[0]
    assert "32.6" in text


def test_appending_a_signal_preserves_rows_written_under_an_older_header(tmp_path):
    """Regression test: data/signals.csv already exists in the wild under the
    pre-earnings-growth header. A blind append would misalign every column
    on the new row; this must rewrite under the current header instead,
    keeping the old row's values (blank for the new columns)."""
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        "symbol,up1_date,down_date,up2_date,price,fair_value,"
        "valuation_known,valuation_pass,fired,recorded_at\n"
        "AAPL,2026-01-13,2026-01-23,2026-01-26,,,False,False,True,2026-07-27T19:36:06\n"
    )
    sig = Signal(
        "NVDA", "2026-01-01", "2026-01-03", "2026-01-05",
        200.0, 190.0, True, True, True, "now",
        earnings_growth=32.6, earnings_growth_known=True, earnings_growth_pass=True,
    )
    append_signal_csv(tmp_path, sig)

    lines = csv_path.read_text().splitlines()
    assert lines[0].split(",") == [
        "symbol", "up1_date", "down_date", "up2_date", "price", "fair_value",
        "valuation_known", "valuation_pass", "earnings_growth",
        "earnings_growth_known", "earnings_growth_pass", "fired", "recorded_at",
        "horizon", "direction",
    ]
    assert len(lines) == 3  # header + old row + new row
    assert lines[1].startswith("AAPL,")
    assert lines[2].startswith("NVDA,")
    assert "32.6" in lines[2]


def test_csv_snapshot_includes_earnings_growth(store, tmp_path):
    store.upsert_rsi_point(
        RsiPoint("NVDA", "2026-01-01", 100.0, 44.0, "live:tradingview", 32.6, "ttm")
    )
    path = export_csv_snapshot(store, tmp_path)
    text = path.read_text()
    assert "earnings_growth" in text.splitlines()[0]
    assert "32.6" in text


# ------------------------------------------------------- pruning intraday bars


def _hourly(store, symbol, days, per_day=7, horizon="1h"):
    for day in days:
        for hour in range(13, 13 + per_day):
            store.upsert_rsi_point(
                RsiPoint(symbol, f"{day}T{hour:02d}:30", 100.0, 50.0, "live:tradingview",
                         horizon=horizon)
            )


def _dates(store, symbol, horizon):
    return [p.date for p in store.rsi_series(symbol, horizon)]


def test_intraday_older_than_the_daily_history_goes(store):
    """The bars nothing can reach.

    `forward_outcomes` refuses to measure a signal the daily series doesn't
    reach back to, so an intraday bar from before the first daily bar prices a
    pattern whose outcome is unknowable -- and the chart never plots back that
    far either.
    """
    _hourly(store, "NVDA", ["2024-01-02", "2024-01-03", "2026-01-05"])
    for day in ("2026-01-05", "2026-01-06"):
        store.upsert_rsi_point(RsiPoint("NVDA", day, 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=7) == 14
    assert all(d.startswith("2026-01-05") for d in _dates(store, "NVDA", "1h"))


def test_the_chart_window_is_never_pruned(store):
    """SPCX listed recently enough that its 4h history predates its daily
    history. Testing the daily line alone shortened its chart from 90 bars to
    76 -- a visible regression on a page nobody would have thought to check."""
    _hourly(store, "SPCX", ["2026-01-02", "2026-01-03"], horizon="4h")
    store.upsert_rsi_point(RsiPoint("SPCX", "2026-01-04", 100.0, 50.0, "live:tradingview", horizon="1d"))
    # Every 4h bar predates the daily history, but they are all the chart has.
    assert store.prune_unmeasurable_intraday(keep_bars=90) == 0
    assert len(_dates(store, "SPCX", "4h")) == 14


def test_daily_and_weekly_bars_are_left_alone(store):
    """The daily series is the ruler every forward return is measured with, and
    the weekly one is 5 years deep by design."""
    for day in ("2021-01-04", "2024-06-03"):
        store.upsert_rsi_point(RsiPoint("NVDA", day, 100.0, 50.0, "live:tradingview", horizon="1w"))
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=1) == 0
    assert len(_dates(store, "NVDA", "1w")) == 2


def test_a_symbol_with_no_daily_history_keeps_its_chart(store):
    """A ticker seeded on the hourly chart but not yet on the daily one has no
    line to measure against. Dropping everything would blank its card."""
    _hourly(store, "NEW", ["2026-01-02", "2026-01-03"])
    assert store.prune_unmeasurable_intraday(keep_bars=90) == 0
    assert len(_dates(store, "NEW", "1h")) == 14


def test_pruning_twice_removes_nothing_the_second_time(store):
    """It runs on a schedule, so it has to be a no-op once the backlog is gone."""
    _hourly(store, "NVDA", ["2024-01-02", "2026-01-05"])
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=7) == 7
    assert store.prune_unmeasurable_intraday(keep_bars=7) == 0


def test_a_bar_on_the_first_daily_day_survives(store):
    """The boundary. The daily series covers that day, so the outcome of a
    pattern completing on it is measurable."""
    _hourly(store, "NVDA", ["2026-01-05"])
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=0) == 0
    assert len(_dates(store, "NVDA", "1h")) == 7


def test_one_symbol_does_not_set_the_line_for_another(store):
    """The daily line is per symbol -- a long-listed name must not license
    pruning a young one's history."""
    _hourly(store, "OLD", ["2024-01-02"])
    _hourly(store, "NEW", ["2024-01-02"])
    store.upsert_rsi_point(RsiPoint("OLD", "2026-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    store.upsert_rsi_point(RsiPoint("NEW", "2023-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=0) == 7
    assert len(_dates(store, "NEW", "1h")) == 7
    assert _dates(store, "OLD", "1h") == []


def test_the_floor_keeps_exactly_the_bars_it_says(store):
    """It was off by one -- `keep_bars=90` kept 91, because the offset counts
    from zero. Harmless on a chart, wrong in a function that names a number."""
    _hourly(store, "NVDA", ["2024-01-02", "2024-01-03"])  # 14 bars, all unmeasurable
    store.upsert_rsi_point(RsiPoint("NVDA", "2026-01-05", 100.0, 50.0, "live:tradingview", horizon="1d"))
    assert store.prune_unmeasurable_intraday(keep_bars=5) == 9
    assert len(_dates(store, "NVDA", "1h")) == 5
