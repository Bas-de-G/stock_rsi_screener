"""Tests for folding a fair value back onto already-recorded patterns.

The gate depends on two things at once: the horizon (each has its own margin)
and the direction (a sell is scored against the mirrored rule). Scoring once
and writing the result to every row is the bug this file exists to prevent —
it left every sell permanently unvalued, and with `fire_without_valuation`
set that meant every sell pattern fired ungraded and none could ever be
strong. Offline throughout.
"""

from __future__ import annotations

import pytest

from screener.cli import _rescore_signals, sync_fair_values
from screener.config import load_config
from screener.signals import BUY, SELL
from screener.storage import RsiPoint, Signal, Store

CONFIG_YAML = """
tickers:
  - {symbol: AAPL, tradingview: "NASDAQ:AAPL", morningstar: xnas/aapl, markets: [sp500]}
rsi: {period: 14, threshold: 30, overbought: 70, interval: "1D"}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage: {database: "%(db)s", csv_dir: "%(dir)s", fair_values: "%(fv)s"}
dashboard: {output: "%(html)s", chart_days: 90}
"""


@pytest.fixture()
def config(tmp_path):
    """Everything under tmp_path — never the repo's real files."""
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML % {
        "db": tmp_path / "t.db",
        "dir": tmp_path,
        "fv": tmp_path / "fair_values.yaml",
        "html": tmp_path / "t.html",
    })
    return load_config(path)


def _seed(store: Store, *, price: float) -> None:
    """One daily bar plus a buy and a sell pattern on the 1d horizon."""
    store.upsert_rsi_point(
        RsiPoint("AAPL", "2026-08-05", price, 55.0, "test", horizon="1d")
    )
    for direction in (BUY, SELL):
        store.record_signal(Signal(
            symbol="AAPL",
            up1_date="2026-07-20", down_date="2026-07-25", up2_date="2026-07-30",
            price=None, fair_value=None,
            valuation_known=False, valuation_pass=False, fired=True,
            recorded_at="2026-07-30T00:00:00",
            horizon="1d", direction=direction,
        ))


def test_rescore_reaches_sells_as_well_as_buys(config):
    """The regression: a sell must not be left with an unknown valuation."""
    with Store(config.storage.database) as store:
        _seed(store, price=100.0)
        _rescore_signals(store, config, "AAPL", price=100.0, fair_value=200.0)

        for direction in (BUY, SELL):
            sig = store.all_signals("AAPL", "1d", direction)[0]
            assert sig.valuation_known, f"{direction} left unvalued"
            assert sig.fair_value == 200.0
            assert sig.price == 100.0


def test_the_sell_is_scored_against_the_mirrored_rule(config):
    """Price far *below* fair value confirms a buy and must deny a sell."""
    with Store(config.storage.database) as store:
        _seed(store, price=100.0)
        # 100 vs 200 clears the 1d 30% margin on the buy side by a wide margin.
        _rescore_signals(store, config, "AAPL", price=100.0, fair_value=200.0)

        buy = store.all_signals("AAPL", "1d", BUY)[0]
        sell = store.all_signals("AAPL", "1d", SELL)[0]
        assert buy.valuation_pass is True
        assert sell.valuation_pass is False


def test_the_mirrored_rule_confirms_an_expensive_sell(config):
    """The other direction: price far above fair value is what a sell wants."""
    with Store(config.storage.database) as store:
        _seed(store, price=300.0)
        _rescore_signals(store, config, "AAPL", price=300.0, fair_value=100.0)

        buy = store.all_signals("AAPL", "1d", BUY)[0]
        sell = store.all_signals("AAPL", "1d", SELL)[0]
        assert buy.valuation_pass is False
        assert sell.valuation_pass is True


def test_each_horizon_is_scored_against_its_own_margin(config):
    """A 20% gap clears 1h's 10% margin and fails 1d's 30% one."""
    with Store(config.storage.database) as store:
        for horizon in ("1h", "1d"):
            store.record_signal(Signal(
                symbol="AAPL",
                up1_date="2026-07-20", down_date="2026-07-25", up2_date="2026-07-30",
                price=None, fair_value=None,
                valuation_known=False, valuation_pass=False, fired=True,
                recorded_at="2026-07-30T00:00:00",
                horizon=horizon, direction=BUY,
            ))
        _rescore_signals(store, config, "AAPL", price=100.0, fair_value=120.0)

        assert store.all_signals("AAPL", "1h", BUY)[0].valuation_pass is True
        assert store.all_signals("AAPL", "1d", BUY)[0].valuation_pass is False


def test_sync_fair_values_grades_sells_end_to_end(config):
    """The path the daily run actually takes, from YAML file to signal rows."""
    config.storage.fair_values.write_text("AAPL:\n  fair_value: 100.0\n  checked: '2026-08-05'\n")
    with Store(config.storage.database) as store:
        _seed(store, price=300.0)
        assert sync_fair_values(store, config, quiet=True) == 1

        sell = store.all_signals("AAPL", "1d", SELL)[0]
        assert sell.valuation_known is True
        assert sell.valuation_pass is True, "expensive stock should confirm a sell"


def test_sync_fair_values_counts_symbols_not_rows(config):
    """The return value feeds a 'N fair value(s) loaded' line — one per symbol."""
    config.storage.fair_values.write_text("AAPL:\n  fair_value: 100.0\n  checked: '2026-08-05'\n")
    with Store(config.storage.database) as store:
        _seed(store, price=300.0)
        # Two horizons x two directions = four rows updated, but one symbol.
        assert sync_fair_values(store, config, quiet=True) == 1
