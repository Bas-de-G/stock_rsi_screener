"""Tests for the crypto universe and the unvalued-ticker path.

The theme running through these: a ticker with no fundamentals must be shown as
having none, not given a proxy for them. Every test below is a place where the
code was tempted to invent a number and must not.

Offline throughout -- no CoinGecko, no TradingView.
"""

from __future__ import annotations

import pytest

from screener.coingecko import Asset, config_line
from screener.config import Ticker, load_config
from screener.signals import is_strong


def asset(symbol="BTC", rank=1, **over):
    fields = dict(
        rank=rank, coingecko_id=symbol.lower(), symbol=symbol, name=symbol,
        market_cap=1e12, price=50_000.0, ath=100_000.0, ath_change_pct=-50.0,
    )
    fields.update(over)
    return Asset(**fields)


def crypto_config(tmp_path, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: AAPL, tradingview: "NASDAQ:AAPL", morningstar: xnas/aapl, markets: [nasdaq]}}
  - {{symbol: BTC, tradingview: "BINANCE:BTCUSDT", yahoo: BTC-USD, markets: [crypto]}}
{extra}
rsi: {{period: 14, threshold: 30, overbought: 70, interval: "1D"}}
signal: {{window_days: 14, window_unit: calendar, valuation_rule: price_below_fair_value}}
storage:
  database: "{tmp_path / 't.db'}"
  csv_dir: "{tmp_path}"
  fair_values: "{tmp_path / 'fv.yaml'}"
  notifications: "{tmp_path / 'n.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
""")
    return load_config(path)


# ------------------------------------------------- a ticker without a slug


def test_a_crypto_ticker_needs_no_morningstar_slug(tmp_path):
    config = crypto_config(tmp_path)
    assert config.ticker("BTC").valued is False
    assert config.ticker("AAPL").valued is True


def test_an_equity_without_a_slug_is_refused(tmp_path):
    """The consequence is invisible and permanent -- `is_strong` requires a
    valuation, so a typo here would silently bar the stock from ever earning a
    rocket. Only crypto may omit it."""
    with pytest.raises(ValueError, match="no morningstar slug"):
        crypto_config(tmp_path, extra=(
            '  - {symbol: MSFT, tradingview: "NASDAQ:MSFT", markets: [nasdaq]}'
        ))


def test_an_unvalued_ticker_can_never_be_strong():
    """The whole of option 1 in one assertion. A crypto pattern is real and
    reportable; it is never confirmed, because there is nothing to confirm it
    with."""
    assert not is_strong((False, False))
    assert not is_strong((False, True)), "unknown valuation is never a pass"


def test_an_unvalued_ticker_links_to_the_chart_instead():
    ticker = Ticker(symbol="BTC", tradingview="BINANCE:BTCUSDT", markets=("crypto",))
    assert "tradingview.com" in ticker.tradingview_url
    assert "BINANCE-BTCUSDT" in ticker.tradingview_url


# ------------------------------------------------------ the universe rules


def test_the_config_line_carries_no_valuation_field():
    line = config_line(asset("ETH", rank=2))
    assert "morningstar" not in line, "there is no fair value for a cryptocurrency"
    assert "markets: [crypto]" in line
    assert 'tradingview: "BINANCE:ETHUSDT"' in line


def test_the_yahoo_symbol_is_a_guess_that_needs_checking():
    """`{SYMBOL}-USD` is right for most and wrong for some: Yahoo disambiguates
    a colliding ticker with a numeric suffix, and plain UNI-USD returns a
    malformed chart rather than an error. Pinned so the convention is not
    mistaken for a guarantee."""
    assert asset("BTC").yahoo == "BTC-USD"
    assert asset("UNI").yahoo == "UNI-USD"  # and this one does NOT work live


def test_exclusion_is_by_id_because_symbols_collide(monkeypatch):
    """The bug this prevents: a token called "Mezo Wrapped BTC" carries the
    symbol BTC, so excluding by symbol removed Bitcoin itself -- rank 1 -- and
    left a crypto watchlist that looked perfectly reasonable without it."""
    from screener import coingecko

    pages = {
        ("markets", "wrapped-tokens"): [
            {"id": "mezo-wrapped-btc", "symbol": "btc", "name": "Mezo Wrapped BTC"},
        ],
        ("markets", None): [
            {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
             "market_cap_rank": 1, "current_price": 50_000.0,
             "market_cap": 1e12, "ath": 1e5, "ath_change_percentage": -50.0},
        ],
    }

    def fake_get(path, params):
        return pages[("markets", params.get("category"))]

    monkeypatch.setattr(coingecko, "_get", fake_get)
    monkeypatch.setattr(coingecko.time, "sleep", lambda _: None)

    excluded = coingecko.excluded_ids(categories=("wrapped-tokens",))
    assert excluded == {"mezo-wrapped-btc"}, "ids, not symbols"

    kept = coingecko.top_assets(limit=1, exclude=excluded)
    assert [a.symbol for a in kept] == ["BTC"], "real Bitcoin survives the filter"


def test_a_rate_limit_is_reported_as_such(monkeypatch):
    """429 is the normal answer from the free tier under load, and "CoinGecko
    request failed" would send someone hunting for a network fault."""
    import urllib.error

    from screener import coingecko
    from screener.tradingview import MarketDataError

    def boom(request, timeout=None):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(coingecko.urllib.request, "urlopen", boom)
    with pytest.raises(MarketDataError, match="rate-limited"):
        coingecko._get("coins/markets", {"vs_currency": "usd"})


# --------------------------------------------------- what the card shows


def test_the_card_says_pattern_only_and_scores_nothing(tmp_path):
    """Bitcoin scored 6/10 on 24% coverage in the first version, every point
    from "Earnings timing: no release near" -- an asset credited for staying
    clear of results it can never have. A confident number built from nothing,
    sitting beside equities scored on five real factors, is worse than none."""
    import datetime as dt

    from screener.dashboard import _card, _collect
    from screener.storage import RsiPoint, Store

    config = crypto_config(tmp_path)
    horizon = config.horizon("1d")
    with Store(config.storage.database) as store:
        base = dt.date(2026, 1, 1)
        for i in range(30):
            store.upsert_rsi_point(RsiPoint(
                "BTC", (base + dt.timedelta(days=i)).isoformat(),
                50_000.0 + i, 35.0, "test", horizon="1d",
            ))
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    assert row.valued is False
    assert row.conviction is None, "no composite where four of five factors cannot exist"

    card = _card(row, config, horizon)
    assert "No valuation — pattern only" in card
    assert "Buffett score" not in card, "a permanent dash is not information"
    assert "No earnings growth data yet" not in card, "'yet' promises what never comes"
    assert "morningstar.com" not in card
    assert "tradingview.com" in card
