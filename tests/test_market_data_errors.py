"""Tests for telling "too young" apart from "the request broke".

TradingView answers a scanner query for a recently listed stock with a real
price and a null RSI: a 14-period weekly RSI needs fifteen weekly bars, so a
stock that listed two months ago has a good 1h RSI and no 1W one. Treating
that as a fetch failure turned one young ticker into a red scheduled run,
which skipped the commit and publish steps behind it — so the other 129
tickers never reached the dashboard. Offline throughout.
"""

from __future__ import annotations

import pytest

from screener import tradingview
from screener.tradingview import MarketDataError, NoHistoryYet, fetch_live_rsi


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def served(monkeypatch):
    """Serve one canned scanner payload to fetch_live_rsi."""

    def _serve(payload):
        monkeypatch.setattr(
            tradingview.requests, "get", lambda *a, **kw: _Response(payload)
        )

    return _serve


# The real shape of a Bending Spoons weekly query: price present, RSI null.
_TOO_YOUNG = {
    "RSI|1W": None,
    "close|1W": 43.22,
    "earnings_per_share_diluted_yoy_growth_fy": -100.14,
    "earnings_per_share_diluted_yoy_growth_ttm": None,
}


def test_a_price_with_no_rsi_is_reported_as_too_young(served):
    served(_TOO_YOUNG)
    with pytest.raises(NoHistoryYet):
        fetch_live_rsi("NASDAQ:BSP", interval="1W")


def test_too_young_is_still_a_market_data_error(served):
    """Subclassing keeps every existing `except MarketDataError` working."""
    served(_TOO_YOUNG)
    with pytest.raises(MarketDataError):
        fetch_live_rsi("NASDAQ:BSP", interval="1W")


def test_the_message_names_the_interval_and_the_price(served):
    served(_TOO_YOUNG)
    with pytest.raises(NoHistoryYet, match="43.22"):
        fetch_live_rsi("NASDAQ:BSP", interval="1W")


def test_no_price_and_no_rsi_stays_a_plain_error(served):
    """Both fields missing means the response is broken, not the listing young."""
    served({"RSI|1W": None, "close|1W": None})
    with pytest.raises(MarketDataError) as caught:
        fetch_live_rsi("NASDAQ:NOPE", interval="1W")
    assert not isinstance(caught.value, NoHistoryYet)


def test_a_complete_response_still_parses(served):
    served({"RSI": 55.8, "close": 145.74,
            "earnings_per_share_diluted_yoy_growth_ttm": 34.2})
    quote = fetch_live_rsi("NYSE:ORCL")
    assert quote.rsi == 55.8
    assert quote.close == 145.74


def test_a_request_failure_is_not_mistaken_for_a_young_listing(monkeypatch):
    import requests

    def _boom(*a, **kw):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(tradingview.requests, "get", _boom)
    with pytest.raises(MarketDataError) as caught:
        fetch_live_rsi("NYSE:ORCL")
    assert not isinstance(caught.value, NoHistoryYet)


# ------------------------------------------- what it means for `run`


def test_a_young_ticker_does_not_fail_the_run(tmp_path, monkeypatch, capsys):
    """The regression: `run` must still exit 0 when one listing is too young.

    A non-zero exit here fails the scheduled job, and the commit and publish
    steps sit behind it — so one young ticker withheld the dashboard from
    every other one.
    """
    from argparse import Namespace

    from screener.cli import cmd_run
    from screener.config import load_config
    from screener import cli as cli_module

    config_yaml = f"""
tickers:
  - {{symbol: ORCL, tradingview: "NYSE:ORCL", morningstar: xnys/orcl, markets: [sp500]}}
  - {{symbol: BSP,  tradingview: "NASDAQ:BSP", morningstar: xnas/bsp, markets: [nasdaq]}}
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
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
"""
    path = tmp_path / "config.yaml"
    path.write_text(config_yaml)
    config = load_config(path)

    def _fetch(tv_symbol, period=14, interval="1D"):
        if tv_symbol == "NASDAQ:BSP" and interval == "1W":
            raise NoHistoryYet("NASDAQ:BSP has no RSI|1W yet (close is 43.22)")
        return tradingview.LiveQuote(
            symbol=tv_symbol, close=100.0, rsi=55.0,
            earnings_growth=None, earnings_growth_period=None,
        )

    monkeypatch.setattr(cli_module, "fetch_live_rsi", _fetch)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(config, args) == 0

    out = capsys.readouterr().out
    assert "no 1 week RSI yet" in out, "the skip should still be visible in the log"


def test_a_broken_fetch_still_fails_the_run(tmp_path, monkeypatch):
    """The other half: a genuine outage must not be silently swallowed."""
    from argparse import Namespace

    from screener.cli import cmd_run
    from screener.config import load_config
    from screener import cli as cli_module

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: ORCL, tradingview: "NYSE:ORCL", morningstar: xnys/orcl, markets: [sp500]}}
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
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
""")

    def _boom(*a, **kw):
        raise MarketDataError("TradingView request failed: 500")

    monkeypatch.setattr(cli_module, "fetch_live_rsi", _boom)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(config := load_config(path), args) == 1
