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

from screener.cli import (
    _cap_targets,
    _ranked_targets,
    _resolve_scrape_targets,
    _signalled_symbols,
)
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
storage:
  database: TMP/t.db
  csv_dir: TMP
  fair_values: TMP/fair_values.yaml
dashboard: {output: TMP/t.html, chart_days: 90}
"""


@pytest.fixture()
def config(tmp_path):
    # Every storage path is templated to tmp_path. Without an explicit
    # `fair_values` key the loader falls back to the repository's own
    # fair_values.yaml, and tests that write one would clobber real data.
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML.replace("TMP", str(tmp_path)))
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


def args(all=False, symbols=None, limit=None):
    return argparse.Namespace(all=all, symbols=symbols, limit=limit)


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


# ---------------------------------------------------------- the ordering


def live_history(store, symbol, rsi=35.0, days=25):
    """Daily bars ending today, so freshness and liveness can be judged.

    `history` above starts in April 2026 and is deliberately stale; these
    ordering tests need a series whose last bar is recent, because
    `signal_is_fresh` refuses to call anything fresh when the data itself has
    stopped updating.
    """
    import datetime as dt

    today = dt.date.today()
    for i in range(days, 0, -1):
        date = (today - dt.timedelta(days=i - 1)).isoformat()
        store.upsert_rsi_point(RsiPoint(symbol, date, 100.0, rsi, "backfill:yahoo"))
    return today


def test_a_pattern_that_just_fired_outranks_one_that_is_merely_live(store, config):
    """What a capped session must spend its first pages on: the signal that
    becomes a strong buy today, not one that has been sitting there a week."""
    import datetime as dt

    today = live_history(store, "NVDA")
    live_history(store, "IBM")
    store.record_signal(signal("NVDA", (today - dt.timedelta(days=8)).isoformat()))
    store.record_signal(signal("IBM", today.isoformat()))

    assert _signalled_symbols(store, config) == ["IBM", "NVDA"]
    assert [u for _, u, _ in _ranked_targets(store, config)] == [0, 1]


def test_a_signal_no_longer_live_sorts_last_but_is_not_dropped(store, config):
    """A fair value is cached for a fortnight, so reading one early often pays
    off later — it just must not crowd out today's."""
    import datetime as dt

    today = live_history(store, "NVDA", rsi=25.0)   # RSI back under 30: not live
    live_history(store, "IBM", rsi=35.0)
    store.record_signal(signal("NVDA", (today - dt.timedelta(days=3)).isoformat()))
    store.record_signal(signal("IBM", (today - dt.timedelta(days=3)).isoformat()))

    ordered = _signalled_symbols(store, config)
    assert ordered == ["IBM", "NVDA"], "the live one first, the other still present"


def test_within_a_tier_the_most_recent_cross_leads(store, config):
    import datetime as dt

    today = live_history(store, "NVDA")
    live_history(store, "IBM")
    store.record_signal(signal("NVDA", (today - dt.timedelta(days=9)).isoformat()))
    store.record_signal(signal("IBM", (today - dt.timedelta(days=6)).isoformat()))
    assert _signalled_symbols(store, config) == ["IBM", "NVDA"]


def test_a_sell_pattern_also_earns_a_scrape(store, config):
    """The same Morningstar number grades a sell against the mirrored rule."""
    today = live_history(store, "NVDA", rsi=75.0)
    sell = Signal(
        symbol="NVDA", up1_date="2026-01-01", down_date="2026-01-05",
        up2_date=today.isoformat(), price=None, fair_value=None,
        valuation_known=False, valuation_pass=False, fired=True,
        recorded_at="now", direction="sell",
    )
    store.record_signal(sell)
    assert _signalled_symbols(store, config) == ["NVDA"]


# ------------------------------------------------------------- the cap


def test_no_limit_takes_everything(config):
    tickers = list(config.tickers)
    assert _cap_targets(tickers, args()) == (tickers, [])


def test_the_limit_splits_the_list_in_order(config):
    tickers = list(config.tickers)
    taken, deferred = _cap_targets(tickers, args(limit=2))
    assert [t.symbol for t in taken] == ["NVDA", "IBM"]
    assert [t.symbol for t in deferred] == ["TXN"]


def test_a_limit_larger_than_the_list_defers_nothing(config):
    tickers = list(config.tickers)
    assert _cap_targets(tickers, args(limit=99)) == (tickers, [])


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


# ------------------------------------------- skip fair values already fresh


import datetime as dt

from screener.cli import (
    DEFAULT_MAX_FAIR_VALUE_AGE_DAYS,
    _drop_recently_checked,
    _fresh_fair_values,
)
from screener.fairvalues import save_fair_value


def days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def scrape_args(force=False, max_age=None):
    return argparse.Namespace(force=force, max_age=max_age)


def test_a_value_checked_today_is_fresh(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(0))
    assert _fresh_fair_values(config, 14) == {"IBM": 0}


def test_a_value_checked_within_the_window_is_fresh(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(13))
    assert _fresh_fair_values(config, 14) == {"IBM": 13}


def test_a_value_older_than_the_window_is_stale(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(14))
    assert _fresh_fair_values(config, 14) == {}


def test_a_value_with_no_checked_date_counts_as_stale(config):
    """Hand-written without a date — re-reading beats assuming it's current."""
    config.storage.fair_values.write_text("IBM: 225.0\n")
    assert _fresh_fair_values(config, 14) == {}


def test_an_unparseable_checked_date_counts_as_stale(config):
    config.storage.fair_values.write_text("IBM:\n  fair_value: 225.0\n  checked: last Tuesday\n")
    assert _fresh_fair_values(config, 14) == {}


def test_a_broken_yaml_file_does_not_crash_the_filter(config):
    config.storage.fair_values.write_text("IBM: [unclosed\n")
    assert _fresh_fair_values(config, 14) == {}


def test_fresh_tickers_are_dropped_from_the_scrape_list(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(3))
    tickers = [config.ticker("IBM"), config.ticker("NVDA")]
    kept, skipped = _drop_recently_checked(config, tickers, scrape_args())
    assert [t.symbol for t in kept] == ["NVDA"]
    assert skipped == [("IBM", 3)]


def test_force_keeps_everything(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(1))
    tickers = [config.ticker("IBM")]
    kept, skipped = _drop_recently_checked(config, tickers, scrape_args(force=True))
    assert [t.symbol for t in kept] == ["IBM"]
    assert skipped == []


def test_max_age_narrows_the_window(config):
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(5))
    tickers = [config.ticker("IBM")]
    assert _drop_recently_checked(config, tickers, scrape_args(max_age=3))[0] != []
    assert _drop_recently_checked(config, tickers, scrape_args(max_age=10))[0] == []


def test_the_default_window_is_two_weeks():
    assert DEFAULT_MAX_FAIR_VALUE_AGE_DAYS == 14


def test_the_freshness_filter_is_separate_from_symbol_resolution(store, config):
    """A typo and a still-fresh value must stay distinguishable: the first is
    a non-zero exit, the second is the feature doing its job."""
    save_fair_value(config.storage.fair_values, "IBM", 225.0, checked=days_ago(1))
    resolved = _resolve_scrape_targets(store, config, args(symbols="IBM"))
    assert [t.symbol for t in resolved] == ["IBM"], "resolution ignores freshness"
    kept, skipped = _drop_recently_checked(config, resolved, scrape_args())
    assert kept == [] and skipped == [("IBM", 1)]
