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


def test_an_ungraded_ticker_can_never_be_strong():
    """`is_strong` requires a gate that was actually applied. Crypto now has
    one — the two-clock drawdown — but an asset with no recorded highs has
    nothing applied to it, and must not slide into the strong cohort on a
    default. Renamed from "unvalued can never be strong", which stopped being
    true the day the drawdown gate shipped."""
    assert not is_strong((False, False))
    assert not is_strong((False, True)), "an ungraded asset is never a pass"


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
    # No highs recorded in this fixture, so the gate cannot be applied and the
    # card says which -- not "does not qualify", which would be a claim.
    assert "No highs recorded yet" in card
    assert "Buffett score" not in card, "a permanent dash is not information"
    assert "No earnings growth data yet" not in card, "'yet' promises what never comes"
    assert "morningstar.com" not in card
    assert "tradingview.com" in card


# ------------------------------------------------------ reaching a phone


def test_the_push_filter_needs_both_the_timeframe_and_the_market():
    from screener.config import NotifyConfig

    notify = NotifyConfig(push_horizons=("4h", "1d"), push_markets=("crypto",))
    assert notify.pushes("1d", ("crypto",))
    assert not notify.pushes("1h", ("crypto",)), "wrong timeframe"
    assert not notify.pushes("1d", ("sp500",)), "wrong market"


def test_a_ticker_in_several_markets_passes_on_any_of_them():
    """BioNTech is tagged europe and nasdaq. Muting europe must not silence it
    while nasdaq is still on."""
    from screener.config import NotifyConfig

    notify = NotifyConfig(push_horizons=("1d",), push_markets=("nasdaq",))
    assert notify.pushes("1d", ("europe", "nasdaq"))


def test_no_market_list_means_every_market():
    """The setting did not exist before, so an unset one must keep the old
    behaviour rather than silently muting everything."""
    from screener.config import NotifyConfig

    notify = NotifyConfig(push_horizons=("1d",))
    assert notify.pushes("1d", ("crypto",))
    assert notify.pushes("1d", ()), "a ticker with no markets is not muted either"


def test_a_mistyped_push_market_is_refused(tmp_path):
    """Same failure mode as a mistyped timeframe: silence that looks like calm.
    `push_markets: [cryptos]` would mute every crypto alert and nothing would
    look broken."""
    from screener.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: BTC, tradingview: "BINANCE:BTCUSDT", markets: [crypto]}}
rsi: {{period: 14, threshold: 30, overbought: 70, interval: "1D"}}
signal: {{window_days: 14, window_unit: calendar, valuation_rule: price_below_fair_value}}
storage:
  database: "{tmp_path / 't.db'}"
  csv_dir: "{tmp_path}"
  fair_values: "{tmp_path / 'fv.yaml'}"
  notifications: "{tmp_path / 'n.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
notify: {{push_markets: [cryptos]}}
""")
    with pytest.raises(ValueError, match="push_markets names no market"):
        load_config(path)


def test_a_pattern_alert_does_not_read_like_a_strong_buy():
    """The distinction the dashboard is careful about matters most on a phone,
    where a push is read in three seconds and acted on without opening it."""
    from screener.config import DEFAULT_HORIZONS
    from screener.notify import format_pattern_buy, pattern_issue_title

    horizon = next(h for h in DEFAULT_HORIZONS if h.key == "1d")
    message = format_pattern_buy("BTC", 77_411.0, "USD", horizon, 30.0, "u")
    assert "PATTERN" in message
    assert "STRONG" not in message and "🚀" not in message
    assert "no fair value exists" in message
    assert "strong" not in pattern_issue_title("BTC", horizon).lower()


def test_a_crypto_pattern_actually_reaches_the_phone(tmp_path, monkeypatch):
    """The bug a market filter alone would have left in place.

    `_notify_new_strong_buys` requires `is_strong`, and an unvalued ticker can
    never be strong -- so listing crypto in `push_markets` would have changed
    nothing at all: the strong gate blocks it upstream and the phone stays
    silent while the config looks correct.
    """
    import datetime as dt

    from screener import cli
    from screener.storage import RsiPoint, Signal, Store

    config = crypto_config(tmp_path)
    pushed = []
    monkeypatch.setattr(cli, "send_push", lambda t, m, u="": pushed.append((t, m)) or True)
    monkeypatch.setattr(cli, "send_webhook", lambda m: False)
    monkeypatch.setattr(cli, "send_github_issue", lambda t, m, k: False)

    # Relative to today: freshness is measured against now, so a pattern
    # seeded at a fixed date ages out and the test would pass for the wrong
    # reason as soon as the calendar moved past it.
    base = dt.date.today() - dt.timedelta(days=39)
    dates = [(base + dt.timedelta(days=i)).isoformat() for i in range(40)]
    with Store(config.storage.database) as store:
        for i, date in enumerate(dates):
            # Ends above the threshold, as a completed double cross must.
            store.upsert_rsi_point(RsiPoint("BTC", date, 50_000.0 + i, 35.0,
                                            "test", horizon="1d"))
        store.record_signal(Signal(
            "BTC", dates[-4], dates[-3], dates[-2], 50_000.0, None,
            False, False, True, "now", horizon="1d", direction="buy",
        ))
        sent = cli._notify_new_strong_buys(store, config)

    assert sent == 1, "a crypto pattern with no valuation must still be announced"
    title, message = pushed[0]
    assert "PATTERN" in message and "STRONG" not in message
    assert "BTC" in title


# --------------------------------------- the drawdown gate, end to end


def _seed_crypto(store, config, price, all_time, recent, bars=180):
    """A crypto row with a fired, fresh buy pattern and known highs."""
    import datetime as dt

    from screener.drawdown import Highs
    from screener.storage import RsiPoint, Signal

    base = dt.date.today() - dt.timedelta(days=39)
    dates = [(base + dt.timedelta(days=i)).isoformat() for i in range(40)]
    for i, date in enumerate(dates):
        store.upsert_rsi_point(RsiPoint("BTC", date, price, 35.0, "test", horizon="1d"))
    store.record_signal(Signal(
        "BTC", dates[-4], dates[-3], dates[-2], price, None,
        False, False, True, "now", horizon="1d", direction="buy",
    ))
    store.upsert_crypto_highs(Highs(
        symbol="BTC", all_time=all_time, recent=recent, recent_bars=bars,
    ))
    return dates


def test_a_crypto_signal_clearing_both_legs_is_a_strong_buy(tmp_path):
    """What was asked for: a rocket on crypto, gated on distance from the
    6-month high AND the all-time high, with the RSI pattern already fired."""
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    config = crypto_config(tmp_path)
    horizon = config.horizon("1d")   # wants 30% off the 6-month high
    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=245.0, all_time=3785.0, recent=480.0)
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    assert row.drawdown_gate == (True, True)
    assert row.strong, "93% below its record and 49% below its 6-month high"
    assert row.state == "strong"
    card = _card(row, config, horizon)
    assert "Below all-time" in card and "Below 6-month" in card
    assert "drawdown confirms" in card
    assert "Not a valuation" in card, "the card must not call this a fair value"


def test_a_crypto_signal_that_has_recovered_is_not_strong(tmp_path):
    """Zcash: 74% below its record, but 4% off its 6-month high. A single
    all-time-high gate would have called this a strong buy."""
    from screener.dashboard import _collect
    from screener.storage import Store

    config = crypto_config(tmp_path)
    horizon = config.horizon("1d")
    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=819.61, all_time=3191.93, recent=852.19)
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    assert row.drawdown_gate == (True, False)
    assert not row.strong
    assert row.fired, "still a signal — the pattern is real, it just isn't confirmed"


def test_the_journal_records_the_gate_the_page_judged_by(tmp_path):
    """The signal's stored valuation columns are empty for crypto, so reading
    them here would journal a published strong buy as a plain signal and the
    record would disagree with the page."""
    import json

    from screener.dashboard import _collect
    from screener.journal import recommendation_from
    from screener.storage import Store

    config = crypto_config(tmp_path)
    horizon = config.horizon("1d")
    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=245.0, all_time=3785.0, recent=480.0)
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    rec = recommendation_from(row, row.buys[-1], horizon)
    assert rec.verdict == "strong"
    extra = json.loads(rec.extra)
    assert extra["dd_pass"] == 1
    assert extra["dd_ath"] == pytest.approx(0.935, abs=0.01)
    assert extra["dd_recent"] == pytest.approx(0.49, abs=0.01)


def test_a_failing_crypto_row_is_journalled_too(tmp_path):
    """Both sides of the comparison, or 'did the gated ones do better?' cannot
    be answered."""
    import json

    from screener.dashboard import _collect
    from screener.journal import recommendation_from
    from screener.storage import Store

    config = crypto_config(tmp_path)
    horizon = config.horizon("1d")
    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=819.61, all_time=3191.93, recent=852.19)
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    rec = recommendation_from(row, row.buys[-1], horizon)
    assert rec.verdict == "signal"
    assert json.loads(rec.extra)["dd_pass"] == 0


def test_turning_the_gate_off_returns_crypto_to_pattern_only(tmp_path):
    from dataclasses import replace

    from screener.dashboard import _collect
    from screener.storage import Store

    config = crypto_config(tmp_path)
    config = replace(config, crypto=replace(config.crypto, enabled=False))
    horizon = config.horizon("1d")
    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=245.0, all_time=3785.0, recent=480.0)
        row = next(r for r in _collect(store, config, horizon) if r.symbol == "BTC")

    assert row.drawdown_gate == (False, False)
    assert not row.strong and row.fired


def test_a_crypto_strong_buy_reaches_the_phone_as_a_strong_buy(tmp_path, monkeypatch):
    """Not as PATTERN. The distinction has to survive into the push, in both
    directions: a gated crypto buy is the strong claim and must read as one."""
    from screener import cli
    from screener.storage import Store

    config = crypto_config(tmp_path)
    pushed = []
    monkeypatch.setattr(cli, "send_push", lambda t, m, u="": pushed.append(m) or True)
    monkeypatch.setattr(cli, "send_webhook", lambda m: False)
    monkeypatch.setattr(cli, "send_github_issue", lambda t, m, k: False)

    with Store(config.storage.database) as store:
        _seed_crypto(store, config, price=245.0, all_time=3785.0, recent=480.0)
        sent = cli._notify_new_strong_buys(store, config)

    assert sent == 1
    assert "STRONG BUY" in pushed[0]
    assert "PATTERN" not in pushed[0]
