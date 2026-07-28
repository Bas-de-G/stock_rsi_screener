"""Tests for which tickers `scrape` decides to visit.

This selection is what keeps the scraper cheap and inconspicuous. A fair value
only changes anything when a pattern has fired — it's what upgrades a plain buy
to a strong one — so fetching all 35 subscriber pages to answer a question about
three of them is wasted requests against a logged-in account on a paid product.

These run offline: no browser, no session, no network.
"""

from __future__ import annotations

import argparse

import pytest

from screener.cli import _resolve_scrape_targets, _signalled_symbols
from screener.config import load_config
from screener.storage import RsiPoint, Signal, Store

CONFIG_YAML = """
tickers:
  - {symbol: NVDA, tradingview: "NASDAQ:NVDA", morningstar: xnas/nvda}
  - {symbol: IBM,  tradingview: "NYSE:IBM",    morningstar: xnys/ibm}
  - {symbol: TXN,  tradingview: "NASDAQ:TXN",  morningstar: xnas/txn}
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


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def history(store, symbol, start_day=1, days=25):
    """Daily RSI points from 2026-04-01.

    Kept short deliberately: `upsert_rsi_point` commits per row, and the window
    assertions below only need the series to start after the out-of-window
    signal date and before the in-window one.
    """
    import datetime as dt

    base = dt.date(2026, 4, 1)
    for i in range(start_day - 1, start_day - 1 + days):
        date = (base + dt.timedelta(days=i)).isoformat()
        store.upsert_rsi_point(RsiPoint(symbol, date, 100.0, 45.0, "backfill:yahoo"))


def signal(symbol, up2, fired=True):
    return Signal(
        symbol=symbol, up1_date="2026-01-01", down_date="2026-01-05", up2_date=up2,
        price=None, fair_value=None, valuation_known=False, valuation_pass=False,
        fired=fired, recorded_at="now",
    )


def args(all=False, symbols=None):
    return argparse.Namespace(all=all, symbols=symbols)


# --------------------------------------------------- the default selection


def test_no_signals_means_nothing_to_scrape(store, config):
    history(store, "NVDA")
    history(store, "IBM")
    assert _signalled_symbols(store, config) == []


def test_only_symbols_with_a_fired_signal_are_selected(store, config):
    history(store, "NVDA")
    history(store, "IBM")
    store.record_signal(signal("NVDA", "2026-07-01"))
    assert _signalled_symbols(store, config) == ["NVDA"]


def test_an_unfired_pattern_does_not_earn_a_scrape(store, config):
    """A pattern that never fired isn't waiting on a fair value."""
    history(store, "NVDA")
    store.record_signal(signal("NVDA", "2026-07-01", fired=False))
    assert _signalled_symbols(store, config) == []


def test_a_signal_older_than_the_chart_window_is_ignored(store, config):
    """Mirrors the dashboard rule: a signal that's aged off the page shouldn't
    drag a scrape along with it. This is the TXN case from the live database."""
    history(store, "TXN")
    store.record_signal(signal("TXN", "2025-11-05"))
    assert _signalled_symbols(store, config) == []


def test_a_symbol_with_no_history_is_skipped(store, config):
    """No history means no chart window to compare against."""
    store.record_signal(signal("NVDA", "2026-07-01"))
    assert _signalled_symbols(store, config) == []


def test_selection_follows_config_order_not_database_order(store, config):
    history(store, "IBM")
    history(store, "NVDA")
    store.record_signal(signal("IBM", "2026-07-01"))
    store.record_signal(signal("NVDA", "2026-07-01"))
    assert _signalled_symbols(store, config) == ["NVDA", "IBM"]


# ------------------------------------------------------- flag resolution


def test_default_resolves_to_the_signalled_set(store, config):
    history(store, "NVDA")
    history(store, "IBM")
    store.record_signal(signal("IBM", "2026-07-01"))
    targets = _resolve_scrape_targets(store, config, args())
    assert [t.symbol for t in targets] == ["IBM"]


def test_all_flag_takes_every_configured_ticker(store, config):
    history(store, "NVDA")
    targets = _resolve_scrape_targets(store, config, args(all=True))
    assert [t.symbol for t in targets] == ["NVDA", "IBM", "TXN"]


def test_all_works_even_with_no_signals_and_no_history(store, config):
    targets = _resolve_scrape_targets(store, config, args(all=True))
    assert len(targets) == 3


def test_explicit_symbols_win_over_the_signal_rule(store, config):
    """--symbols is how you check a ticker the screener hasn't flagged."""
    history(store, "NVDA")
    store.record_signal(signal("NVDA", "2026-07-01"))
    targets = _resolve_scrape_targets(store, config, args(symbols="TXN"))
    assert [t.symbol for t in targets] == ["TXN"]


def test_explicit_symbols_are_case_and_space_insensitive(store, config):
    targets = _resolve_scrape_targets(store, config, args(symbols=" ibm , nvda "))
    assert [t.symbol for t in targets] == ["IBM", "NVDA"]


def test_an_unknown_symbol_is_reported_and_skipped(store, config, capsys):
    """A typo shouldn't abort the tickers that were spelled correctly."""
    targets = _resolve_scrape_targets(store, config, args(symbols="IBM,WIBBLE"))
    assert [t.symbol for t in targets] == ["IBM"]
    assert "WIBBLE" in capsys.readouterr().out


def test_symbols_flag_beats_all_flag(store, config):
    targets = _resolve_scrape_targets(store, config, args(all=True, symbols="IBM"))
    assert [t.symbol for t in targets] == ["IBM"]
