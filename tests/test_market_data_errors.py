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


# --------------------------------------------------- the batch scan endpoint


@pytest.fixture()
def scanned(monkeypatch):
    """Serve one canned /global/scan payload, and capture what was asked for."""
    sent = {}

    def _serve(payload):
        def _post(url, json, headers, timeout):
            sent.update(url=url, body=json)
            return _Response(payload)

        monkeypatch.setattr(tradingview.requests, "post", _post)
        return sent

    return _serve


def test_one_request_carries_every_horizon(scanned):
    """The whole point: four horizons are four more columns, not four more
    round trips."""
    sent = scanned({"data": [
        {"s": "NYSE:ORCL", "d": [31.9, 142.79, 47.3, 142.79, 49.4, 142.79, 43.6, 142.79, 12.0, None]},
    ]})
    rows = tradingview.fetch_live_batch(["NYSE:ORCL"], ["60", "240", "1D", "1W"])

    assert sent["body"]["columns"][:8] == [
        "RSI|60", "close|60", "RSI|240", "close|240", "RSI", "close", "RSI|1W", "close|1W",
    ]
    assert rows["NYSE:ORCL"]["RSI|60"] == 31.9
    assert rows["NYSE:ORCL"]["RSI|1W"] == 43.6


def test_the_daily_interval_is_not_asked_for_twice(scanned):
    """`1D` and `D` both mean the bare `RSI` column; asking for both would send
    a duplicate and misalign every value after it against its column."""
    assert tradingview.quote_fields(["1D", "D", ""]).count("RSI") == 1


def test_a_row_decodes_per_horizon(scanned):
    scanned({"data": [{"s": "NYSE:ORCL", "d": [31.9, 142.79, 43.6, 140.0, 12.0, None]}]})
    rows = tradingview.fetch_live_batch(["NYSE:ORCL"], ["60", "1W"])
    hourly = tradingview.decode_quote("NYSE:ORCL", rows["NYSE:ORCL"], interval="60")
    weekly = tradingview.decode_quote("NYSE:ORCL", rows["NYSE:ORCL"], interval="1W")
    assert (hourly.rsi, hourly.close) == (31.9, 142.79)
    assert (weekly.rsi, weekly.close) == (43.6, 140.0)
    assert hourly.earnings_growth == 12.0


def test_a_young_listing_in_a_batch_is_still_too_young(scanned):
    """The null-means-too-young rule must survive the move to batching -- it is
    what keeps one recent listing from reddening the run."""
    scanned({"data": [{"s": "NASDAQ:BSP", "d": [None, 43.22, None, -100.14]}]})
    rows = tradingview.fetch_live_batch(["NASDAQ:BSP"], ["1W"])
    with pytest.raises(NoHistoryYet, match="43.22"):
        tradingview.decode_quote("NASDAQ:BSP", rows["NASDAQ:BSP"], interval="1W")


def test_a_symbol_with_no_row_is_simply_absent(scanned):
    scanned({"data": [{"s": "NYSE:ORCL", "d": [55.0, 142.79, None, None]}]})
    rows = tradingview.fetch_live_batch(["NYSE:ORCL", "NASDAQ:NOPE"], ["1D"])
    assert "NASDAQ:NOPE" not in rows


def test_a_failed_scan_is_a_market_data_error(monkeypatch):
    """A whole request failing is an outage, not one ticker's problem."""
    import requests

    def _boom(*a, **kw):
        raise requests.RequestException("503")

    monkeypatch.setattr(tradingview.requests, "post", _boom)
    with pytest.raises(MarketDataError):
        tradingview.fetch_live_batch(["NYSE:ORCL"], ["1D"])


def test_the_watchlist_is_split_into_chunks(monkeypatch):
    calls = []

    def _post(url, json, headers, timeout):
        calls.append(len(json["symbols"]["tickers"]))
        return _Response({"data": []})

    monkeypatch.setattr(tradingview.requests, "post", _post)
    monkeypatch.setattr(tradingview, "SCAN_CHUNK", 2)
    tradingview.fetch_live_batch(["A", "B", "C", "D", "E"], ["1D"])
    assert calls == [2, 2, 1]


def test_a_repeated_symbol_is_only_asked_for_once(monkeypatch):
    calls = []

    def _post(url, json, headers, timeout):
        calls.append(json["symbols"]["tickers"])
        return _Response({"data": []})

    monkeypatch.setattr(tradingview.requests, "post", _post)
    tradingview.fetch_live_batch(["NYSE:ORCL", "NYSE:ORCL"], ["1D"])
    assert calls == [["NYSE:ORCL"]]


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

    def _batch(tv_symbols, intervals, period=14, extra_fields=()):
        rows = {}
        for symbol in tv_symbols:
            row = {}
            for interval in intervals:
                rsi = tradingview.rsi_field_name(period, interval)
                close = tradingview._close_field_name(interval)
                young = symbol == "NASDAQ:BSP" and interval == "1W"
                row[rsi] = None if young else 55.0
                row[close] = 43.22 if young else 100.0
            rows[symbol] = row
        return rows

    monkeypatch.setattr(cli_module, "fetch_live_batch", _batch)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(config, args) == 0

    out = capsys.readouterr().out
    assert "no 1 week RSI yet" in out, "the skip should still be visible in the log"


def test_a_symbol_missing_from_the_batch_is_asked_for_on_its_own(tmp_path, monkeypatch, capsys):
    """The scan index is not quite the set the symbol endpoint serves, so a
    listing can resolve there and be absent here. Falling back is what stops a
    ticker quietly dropping off the dashboard."""
    from argparse import Namespace

    from screener import cli as cli_module
    from screener.cli import cmd_run
    from screener.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: ORCL, tradingview: "NYSE:ORCL", morningstar: xnys/orcl, markets: [sp500]}}
  - {{symbol: NOPE, tradingview: "NASDAQ:NOPE", morningstar: xnas/nope, markets: [nasdaq]}}
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

    def _batch(tv_symbols, intervals, period=14, extra_fields=()):
        row = {}
        for interval in intervals:
            row[tradingview.rsi_field_name(period, interval)] = 55.0
            row[tradingview._close_field_name(interval)] = 100.0
        return {"NYSE:ORCL": row}  # NOPE simply absent

    asked = []

    def _single(tv_symbol, period=14, interval="1D"):
        asked.append(tv_symbol)
        return tradingview.LiveQuote(symbol=tv_symbol, close=7.5, rsi=61.0)

    monkeypatch.setattr(cli_module, "fetch_live_batch", _batch)
    monkeypatch.setattr(cli_module, "fetch_live_rsi", _single)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(load_config(path), args) == 0

    out = capsys.readouterr().out
    assert asked == ["NASDAQ:NOPE"] * 4, "only the missing symbol is fetched singly"
    assert "NOPE: RSI  61.00" in out
    assert "ORCL: RSI  55.00" in out


def test_a_symbol_missing_everywhere_fails_only_itself(tmp_path, monkeypatch, capsys):
    """And when the fallback fails too, it is still one ticker's problem."""
    from argparse import Namespace

    from screener import cli as cli_module
    from screener.cli import cmd_run
    from screener.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: ORCL, tradingview: "NYSE:ORCL", morningstar: xnys/orcl, markets: [sp500]}}
  - {{symbol: NOPE, tradingview: "NASDAQ:NOPE", morningstar: xnas/nope, markets: [nasdaq]}}
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

    def _batch(tv_symbols, intervals, period=14, extra_fields=()):
        row = {}
        for interval in intervals:
            row[tradingview.rsi_field_name(period, interval)] = 55.0
            row[tradingview._close_field_name(interval)] = 100.0
        return {"NYSE:ORCL": row}

    def _single(tv_symbol, period=14, interval="1D"):
        raise MarketDataError(f"TradingView returned no RSI/close for {tv_symbol}")

    monkeypatch.setattr(cli_module, "fetch_live_batch", _batch)
    monkeypatch.setattr(cli_module, "fetch_live_rsi", _single)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(load_config(path), args) == 1

    out = capsys.readouterr().out
    assert "NOPE: RSI unavailable" in out
    assert "ORCL: RSI  55.00" in out, "the healthy ticker must still be recorded"


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
        raise MarketDataError("TradingView scan failed for 1 symbols: 500")

    monkeypatch.setattr(cli_module, "fetch_live_batch", _boom)
    args = Namespace(date="2026-08-05", with_morningstar=False, horizon=None)
    assert cmd_run(config := load_config(path), args) == 1
