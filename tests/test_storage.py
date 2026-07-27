"""Tests for persistence: upserts, dedup, and the live-over-backfill rule."""

from __future__ import annotations

import pytest

from screener.storage import RsiPoint, Signal, Store, Valuation


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
