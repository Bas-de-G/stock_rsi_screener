"""Tests for `backfill`'s skip-if-already-seeded logic.

This is what makes it safe for CI to call `backfill` unconditionally on every
scheduled run (see .github/workflows/daily.yml) instead of only once, guarded
by "does the database file exist at all" — a guard that meant a ticker added
to config.yaml *after* that first run would never get backfilled and would
trickle in one live row a day instead.

Network is mocked (`fetch_daily_closes`), matching the project's offline-tests
convention — no real Yahoo call happens here.
"""

from __future__ import annotations

import argparse

import pytest

from screener import cli as cli_module
from screener.cli import cmd_backfill
from screener.config import load_config
from screener.storage import RsiPoint, Store


def write_config(tmp_path) -> "Config":  # noqa: F821 - just for readability
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
tickers:
  - {{symbol: SNDK, tradingview: "NASDAQ:SNDK", morningstar: xnas/sndk}}
  - {{symbol: IBM,  tradingview: "NYSE:IBM",    morningstar: xnys/ibm}}
rsi: {{period: 14, threshold: 30, interval: "1D"}}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
storage:
  database: {tmp_path / "t.db"}
  csv_dir: {tmp_path}
  fair_values: {tmp_path / "fair_values.yaml"}
morningstar:
  state_file: {tmp_path / "auth.json"}
dashboard:
  output: {tmp_path / "t.html"}
  chart_days: 90
"""
    )
    return load_config(path)


@pytest.fixture()
def config(tmp_path):
    return write_config(tmp_path)


def args(range_="1y", force=False):
    return argparse.Namespace(range=range_, force=force)


def fake_closes(n=100, start_price=100.0):
    """n days of gently rising closes -- enough for RSI(14) to compute."""
    import datetime as dt

    base = dt.date(2025, 1, 1)
    return [
        ((base + dt.timedelta(days=i)).isoformat(), start_price + i * 0.1)
        for i in range(n)
    ]


def test_a_ticker_with_a_full_chart_of_history_is_skipped(monkeypatch, config):
    """SNDK already has 90+ days -- like a ticker that went through a real
    backfill already. Re-running backfill must not refetch it."""
    with Store(config.storage.database) as store:
        for date, close in fake_closes(100):
            store.upsert_rsi_point(RsiPoint("SNDK", date, close, 45.0, "backfill:yahoo"))

    calls = []

    def spy(yahoo_symbol, range_="1y"):
        calls.append(yahoo_symbol)
        return fake_closes()

    monkeypatch.setattr(cli_module, "fetch_daily_closes", spy)
    cmd_backfill(config, args())

    assert "SNDK" not in calls
    assert "IBM" in calls  # IBM has no history yet -- must still be fetched


def test_a_brand_new_ticker_is_backfilled(monkeypatch, config):
    """The actual SanDisk case: zero rows stored, config.yaml already has it."""
    calls = []

    def spy(yahoo_symbol, range_="1y"):
        calls.append(yahoo_symbol)
        return fake_closes()

    monkeypatch.setattr(cli_module, "fetch_daily_closes", spy)
    cmd_backfill(config, args())

    assert calls.count("SNDK") == 1
    with Store(config.storage.database) as store:
        assert len(store.rsi_series("SNDK")) > 0


def test_force_refetches_even_a_fully_seeded_ticker(monkeypatch, config):
    with Store(config.storage.database) as store:
        for date, close in fake_closes(100):
            store.upsert_rsi_point(RsiPoint("SNDK", date, close, 45.0, "backfill:yahoo"))

    calls = []
    monkeypatch.setattr(
        cli_module, "fetch_daily_closes",
        lambda yahoo_symbol, range_="1y": (calls.append(yahoo_symbol), fake_closes())[1],
    )
    cmd_backfill(config, args(force=True))

    assert "SNDK" in calls
