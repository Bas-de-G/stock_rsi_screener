"""Tests for the multi-horizon model: intervals, margins, leverage, storage.

The four timeframes aren't just four copies of the same thing — the pattern
window, the valuation margin and the suggested leverage all move with the
holding period, and intraday bars are keyed by timestamp rather than date.
Offline throughout.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screener.config import (
    DEFAULT_HORIZON,
    DEFAULT_HORIZONS,
    MARKETS,
    SignalConfig,
    load_config,
)
from screener.signals import _moment, _span, find_cross_pairs, valuation_passes
from screener.storage import RsiPoint, Signal, Store

CONFIG_YAML = """
tickers:
  - {symbol: AAPL, tradingview: "NASDAQ:AAPL", morningstar: xnas/aapl, markets: [sp500, nasdaq]}
  - {symbol: ASML, tradingview: "EURONEXT:ASML", morningstar: xams/asml, yahoo: ASML.AS, currency: EUR, markets: [europe]}
  - {symbol: PLUG, tradingview: "NASDAQ:PLUG", morningstar: xnas/plug, markets: [penny, nasdaq]}
rsi: {period: 14, threshold: 30, interval: "1D"}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage: {database: data/t.db, csv_dir: data}
dashboard: {output: data/t.html, chart_days: 90}
"""


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return load_config(path)


# ------------------------------------------------------- horizon defaults


def test_the_four_horizons_are_configured():
    assert [h.key for h in DEFAULT_HORIZONS] == ["1h", "4h", "1d", "1w"]


@pytest.mark.parametrize(
    "key,margin,leverage",
    [("1h", 0.10, 10), ("4h", 0.20, 5), ("1d", 0.30, 2), ("1w", 0.50, 1)],
)
def test_margin_and_leverage_ladder(config, key, margin, leverage):
    h = config.horizon(key)
    assert h.margin == pytest.approx(margin)
    assert h.leverage == leverage


def test_margin_rises_and_leverage_falls_with_the_holding_period(config):
    """The two ladders move in opposite directions by design: a longer hold
    demands more headroom and takes less leverage."""
    margins = [h.margin for h in config.horizons]
    leverages = [h.leverage for h in config.horizons]
    assert margins == sorted(margins), "margin should increase with horizon"
    assert leverages == sorted(leverages, reverse=True), "leverage should decrease"


def test_only_the_intraday_horizons_are_flagged_intraday(config):
    assert [h.key for h in config.horizons if h.intraday] == ["1h", "4h"]


def test_an_unknown_horizon_is_rejected(config):
    with pytest.raises(KeyError, match="not a configured horizon"):
        config.horizon("30m")


def test_horizon_overrides_are_applied(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML + '\nhorizons:\n  "1h": {margin: 0.05, leverage: 3}\n')
    h = load_config(path).horizon("1h")
    assert (h.margin, h.leverage) == (0.05, 3)
    assert h.window_days == 2, "unspecified keys keep their default"


def test_a_bogus_horizon_key_in_config_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML + '\nhorizons:\n  "3h": {margin: 0.05}\n')
    with pytest.raises(ValueError, match="Unknown horizon"):
        load_config(path)


@pytest.mark.parametrize("field,bad", [("margin", -0.1), ("leverage", 0), ("window_days", 0)])
def test_nonsensical_horizon_values_are_rejected(tmp_path, field, bad):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML + f'\nhorizons:\n  "1d": {{{field}: {bad}}}\n')
    with pytest.raises(ValueError):
        load_config(path)


# ----------------------------------------------------- the valuation margin


def test_the_margin_ladder_filters_a_single_fair_value(config):
    """One stock 25% below fair value clears the short horizons and fails the
    long ones -- the whole point of a per-horizon margin."""
    verdicts = {}
    for h in config.horizons:
        _, confirms = valuation_passes(100.0, 125.0, config.signal, h.margin)
        verdicts[h.key] = confirms
    assert verdicts == {"1h": True, "4h": True, "1d": False, "1w": False}


def test_a_zero_margin_reproduces_the_original_gate(config):
    """Anyone zeroing the margins out gets exactly the old behaviour back,
    including 'exactly equal does not pass'."""
    assert valuation_passes(100.0, 100.01, config.signal, 0.0) == (True, True)
    assert valuation_passes(100.0, 100.0, config.signal, 0.0) == (True, False)


def test_the_margin_applies_to_the_inverted_rule_too():
    cfg = SignalConfig(valuation_rule="fair_value_below_price")
    # fair value must sit 30% *below* the price
    assert valuation_passes(100.0, 70.0, cfg, 0.30)[1] is True
    assert valuation_passes(100.0, 80.0, cfg, 0.30)[1] is False


# ------------------------------------------------- intraday bar timestamps


def test_moment_parses_both_bar_label_forms():
    assert _moment("2026-08-04") == dt.datetime(2026, 8, 4)
    assert _moment("2026-08-04T18:49") == dt.datetime(2026, 8, 4, 18, 49)


def test_span_keeps_sub_day_resolution_on_intraday_bars():
    """Regression: `date.fromisoformat` rejects an intraday label outright, so
    every intraday pattern raised ValueError and no signal was ever recorded.
    Truncating to whole days would also widen a 2-day window by nearly a day."""
    series = [
        RsiPoint("X", "2026-08-04T10:00", 100.0, 25.0, "t", horizon="1h"),
        RsiPoint("X", "2026-08-04T22:00", 100.0, 35.0, "t", horizon="1h"),
    ]
    assert _span(series, 0, 1, SignalConfig()) == pytest.approx(0.5)


def test_an_intraday_pattern_is_detected():
    """The 1h case end to end: below 30, up through, back below, up again --
    all inside a 2-day window."""
    labels = ["2026-08-04T10:00", "2026-08-04T14:00", "2026-08-04T18:00", "2026-08-05T10:00"]
    series = [
        RsiPoint("X", lbl, 100.0, rsi, "t", horizon="1h")
        for lbl, rsi in zip(labels, [25.0, 34.0, 27.0, 33.0])
    ]
    pairs = find_cross_pairs(series, 30.0, SignalConfig(window_days=2))
    assert len(pairs) == 1
    assert pairs[0].up2_date == "2026-08-05T10:00"
    assert pairs[0].span_days < 2


def test_an_intraday_pattern_spanning_too_long_is_rejected():
    labels = ["2026-08-01T10:00", "2026-08-01T14:00", "2026-08-01T18:00", "2026-08-09T10:00"]
    series = [
        RsiPoint("X", lbl, 100.0, rsi, "t", horizon="1h")
        for lbl, rsi in zip(labels, [25.0, 34.0, 27.0, 33.0])
    ]
    assert find_cross_pairs(series, 30.0, SignalConfig(window_days=2)) == []


# --------------------------------------------------- horizon-scoped storage


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def test_the_same_symbol_holds_separate_series_per_horizon(store):
    store.upsert_rsi_point(RsiPoint("AAPL", "2026-08-04", 100.0, 45.0, "t", horizon="1d"))
    store.upsert_rsi_point(RsiPoint("AAPL", "2026-08-04T10:00", 101.0, 25.0, "t", horizon="1h"))
    assert len(store.rsi_series("AAPL", "1d")) == 1
    assert len(store.rsi_series("AAPL", "1h")) == 1
    assert store.rsi_series("AAPL", "1d")[0].rsi == 45.0
    assert store.rsi_series("AAPL", "1h")[0].rsi == 25.0
    assert store.rsi_series("AAPL", "4h") == []


def test_intraday_bars_on_the_same_day_do_not_collide(store):
    """The old primary key was (symbol, date) -- every bar in a day would have
    overwritten the last."""
    for hour in (10, 11, 12):
        store.upsert_rsi_point(
            RsiPoint("AAPL", f"2026-08-04T{hour}:00", 100.0, 40.0 + hour, "t", horizon="1h")
        )
    assert len(store.rsi_series("AAPL", "1h")) == 3


def test_rsi_series_defaults_to_the_daily_horizon(store):
    store.upsert_rsi_point(RsiPoint("AAPL", "2026-08-04", 100.0, 45.0, "t"))
    assert len(store.rsi_series("AAPL")) == 1


def test_signals_are_scoped_per_horizon(store):
    for hz in ("1h", "1d"):
        store.record_signal(Signal(
            "AAPL", "2026-08-01", "2026-08-02", "2026-08-03",
            None, None, False, False, True, "now", horizon=hz,
        ))
    assert len(store.all_signals("AAPL")) == 2
    assert len(store.all_signals("AAPL", "1h")) == 1
    assert store.all_signals("AAPL", "1h")[0].horizon == "1h"


def test_the_same_pattern_date_can_exist_on_two_horizons(store):
    """Old PK was (symbol, up2_date), so the second horizon's signal would
    have been silently dropped by INSERT OR IGNORE."""
    for hz in ("1h", "4h", "1d", "1w"):
        store.record_signal(Signal(
            "AAPL", "2026-08-01", "2026-08-02", "2026-08-03",
            None, None, False, False, True, "now", horizon=hz,
        ))
    assert len(store.all_signals("AAPL")) == 4


def test_updating_a_valuation_touches_only_its_own_horizon(store):
    for hz in ("1h", "1w"):
        store.record_signal(Signal(
            "AAPL", "2026-08-01", "2026-08-02", "2026-08-03",
            None, None, False, False, True, "now", horizon=hz,
        ))
    store.update_signal_valuation("AAPL", "2026-08-03", 100.0, 125.0, True, True, True, "1h")
    by_hz = {s.horizon: s for s in store.all_signals("AAPL")}
    assert by_hz["1h"].valuation_pass is True
    assert by_hz["1w"].valuation_pass is False, "1w needs 50% headroom, 25% is not enough"
    assert by_hz["1w"].fair_value is None


# ------------------------------------------------------------- migration


def test_a_pre_horizon_database_migrates_to_daily(tmp_path):
    """Everything collected before horizons existed was the daily bar."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE rsi_history (
             symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
             rsi REAL NOT NULL, source TEXT NOT NULL,
             earnings_growth REAL, earnings_growth_period TEXT,
             PRIMARY KEY (symbol, date));
           CREATE TABLE signals (
             symbol TEXT NOT NULL, up1_date TEXT NOT NULL, down_date TEXT NOT NULL,
             up2_date TEXT NOT NULL, price REAL, fair_value REAL,
             valuation_known INTEGER NOT NULL, valuation_pass INTEGER NOT NULL,
             earnings_growth REAL, earnings_growth_known INTEGER NOT NULL DEFAULT 0,
             earnings_growth_pass INTEGER NOT NULL DEFAULT 0,
             fired INTEGER NOT NULL, recorded_at TEXT NOT NULL,
             PRIMARY KEY (symbol, up2_date));"""
    )
    conn.execute("INSERT INTO rsi_history VALUES ('IBM','2026-07-01',200.0,45.0,'backfill:yahoo',NULL,NULL)")
    conn.execute(
        "INSERT INTO signals VALUES "
        "('IBM','2026-06-01','2026-06-02','2026-06-03',200.0,250.0,1,1,5.0,1,1,1,'then')"
    )
    conn.commit()
    conn.close()

    with Store(path) as s:
        points = s.rsi_series("IBM", "1d")
        assert len(points) == 1 and points[0].horizon == "1d"
        assert points[0].close == 200.0
        sigs = s.all_signals("IBM", "1d")
        assert len(sigs) == 1 and sigs[0].fired is True
        assert sigs[0].fair_value == 250.0
        # and the widened key now accepts a second horizon for the same date
        s.record_signal(Signal(
            "IBM", "2026-06-01", "2026-06-02", "2026-06-03",
            None, None, False, False, True, "now", horizon="1h",
        ))
        assert len(s.all_signals("IBM")) == 2


# ----------------------------------------------------------- market groups


def test_every_ticker_carries_at_least_one_market(config):
    assert all(t.markets for t in config.tickers)


def test_tickers_in_filters_by_market(config):
    assert [t.symbol for t in config.tickers_in("europe")] == ["ASML"]
    assert [t.symbol for t in config.tickers_in("penny")] == ["PLUG"]


def test_a_ticker_can_belong_to_several_markets(config):
    """PLUG is both a sub-$10 name and Nasdaq-listed."""
    assert "PLUG" in [t.symbol for t in config.tickers_in("nasdaq")]
    assert "PLUG" in [t.symbol for t in config.tickers_in("penny")]


def test_active_markets_are_ordered_and_non_empty(config):
    assert config.active_markets == ("sp500", "nasdaq", "europe", "penny")
    for m in config.active_markets:
        assert config.tickers_in(m)


def test_an_unknown_market_tag_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML.replace("markets: [sp500, nasdaq]", "markets: [ftse100]"))
    with pytest.raises(ValueError, match="unknown market"):
        load_config(path)


def test_markets_constant_matches_what_config_accepts():
    assert MARKETS == ("sp500", "nasdaq", "europe", "penny")


def test_default_horizon_is_one_of_the_configured_ones():
    assert DEFAULT_HORIZON in [h.key for h in DEFAULT_HORIZONS]


# ------------------------------------------- earnings growth is graded live


from screener.cli import _latest_earnings_growth, sync_earnings_growth


def test_latest_earnings_growth_skips_backfilled_bars():
    """Only live bars carry the figure, and the newest bar is frequently a
    backfilled one -- so this scans back rather than reading series[-1]."""
    series = [
        RsiPoint("X", "2026-08-01", 100.0, 45.0, "live:tradingview", 12.5, "ttm"),
        RsiPoint("X", "2026-08-02", 100.0, 45.0, "backfill:yahoo"),
        RsiPoint("X", "2026-08-03", 100.0, 45.0, "backfill:yahoo"),
    ]
    assert _latest_earnings_growth(series) == 12.5


def test_latest_earnings_growth_prefers_the_most_recent_reading():
    series = [
        RsiPoint("X", "2026-08-01", 100.0, 45.0, "live:tradingview", 12.5, "ttm"),
        RsiPoint("X", "2026-08-02", 100.0, 45.0, "live:tradingview", -3.0, "ttm"),
    ]
    assert _latest_earnings_growth(series) == -3.0


def test_latest_earnings_growth_is_none_when_nothing_is_live():
    series = [RsiPoint("X", "2026-08-01", 100.0, 45.0, "backfill:yahoo")]
    assert _latest_earnings_growth(series) is None
    assert _latest_earnings_growth([]) is None


def test_a_signal_on_a_backfilled_bar_still_grades_on_earnings(config, tmp_path):
    """Regression: earnings growth was read from the bar at the pattern's own
    second cross. That bar is almost always backfilled and carries no figure,
    so the factor came back unknown on every signal and never graded anything
    -- 0 of 72 signals on the live database. It's a quarterly fundamental
    describing the company now, not a property of one historical bar."""
    import dataclasses

    cfg = dataclasses.replace(
        config, storage=dataclasses.replace(config.storage, database=tmp_path / "eg.db")
    )
    with Store(cfg.storage.database) as store:
        # pattern completes on a backfilled bar...
        store.upsert_rsi_point(
            RsiPoint("AAPL", "2026-04-30", 100.0, 33.0, "backfill:yahoo", horizon="1d")
        )
        # ...and a much later live bar carries the growth figure
        store.upsert_rsi_point(
            RsiPoint("AAPL", "2026-08-04", 110.0, 58.0, "live:tradingview", 32.6, "ttm",
                     horizon="1d")
        )
        store.record_signal(Signal(
            "AAPL", "2026-04-21", "2026-04-25", "2026-04-30",
            None, None, False, False, True, "now", horizon="1d",
        ))
        assert store.all_signals("AAPL", "1d")[0].earnings_growth_known is False

        sync_earnings_growth(store, cfg)

        sig = store.all_signals("AAPL", "1d")[0]
        assert sig.earnings_growth_known is True
        assert sig.earnings_growth == 32.6
        assert sig.earnings_growth_pass is True


def test_shrinking_earnings_withhold_the_rocket_after_sync(config, tmp_path):
    """The LCID case: fair value confirms, but earnings are shrinking, so the
    two known factors disagree and the signal stays a plain buy."""
    import dataclasses

    from screener.signals import is_strong

    cfg = dataclasses.replace(
        config, storage=dataclasses.replace(config.storage, database=tmp_path / "eg.db")
    )
    with Store(cfg.storage.database) as store:
        store.upsert_rsi_point(
            RsiPoint("AAPL", "2026-08-04", 7.62, 58.0, "live:tradingview", -8.5, "ttm",
                     horizon="1d")
        )
        store.record_signal(Signal(
            "AAPL", "2026-04-21", "2026-04-25", "2026-04-30",
            7.0, 12.0, True, True, True, "now", horizon="1d",
        ))
        before = store.all_signals("AAPL", "1d")[0]
        assert is_strong((before.valuation_known, before.valuation_pass),
                         (before.earnings_growth_known, before.earnings_growth_pass))

        sync_earnings_growth(store, cfg)

        after = store.all_signals("AAPL", "1d")[0]
        assert after.earnings_growth_pass is False
        assert not is_strong((after.valuation_known, after.valuation_pass),
                             (after.earnings_growth_known, after.earnings_growth_pass))


def test_sync_earnings_growth_is_scoped_per_horizon(config, tmp_path):
    import dataclasses

    cfg = dataclasses.replace(
        config, storage=dataclasses.replace(config.storage, database=tmp_path / "eg.db")
    )
    with Store(cfg.storage.database) as store:
        store.upsert_rsi_point(
            RsiPoint("AAPL", "2026-08-04", 100.0, 45.0, "live:tradingview", 20.0, "ttm",
                     horizon="1d")
        )
        for hz in ("1d", "1w"):
            store.record_signal(Signal(
                "AAPL", "2026-04-21", "2026-04-25", "2026-04-30",
                None, None, False, False, True, "now", horizon=hz,
            ))
        sync_earnings_growth(store, cfg)
        by_hz = {s.horizon: s for s in store.all_signals("AAPL")}
        assert by_hz["1d"].earnings_growth_known is True
        assert by_hz["1w"].earnings_growth_known is False, "1w has no bars, so nothing to grade on"
