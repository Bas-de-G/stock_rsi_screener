"""Tests for deep value: the tier above strong buy.

A strong buy whose fair value also sits at least `deep_value.margin` above the
price, measured the way every horizon margin already is. It earns its own badge
on the page, its own journal verdict, its own leaderboard selection -- and,
under `notify.push_tier: deep`, it is the only thing that rings the phone.

Offline throughout.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from screener.config import DeepValueConfig, NotifyConfig, load_config
from screener.storage import RsiPoint, Signal, Store, Valuation

NOW = dt.datetime.now().replace(second=0, microsecond=0)


def _stamp(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="minutes")


def _write_config(tmp_path, extra: str = "", tickers: str | None = None):
    tickers = tickers or (
        '  - {symbol: PTON, tradingview: "NASDAQ:PTON", morningstar: xnas/pton, '
        "markets: [nasdaq]}"
    )
    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
{tickers}
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
  notifications: "{tmp_path / 'notified.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard:
  output: "{tmp_path / 't.html'}"
  chart_days: 90
  site_url: https://example.test/screener
{extra}
""")
    return load_config(path)


@pytest.fixture()
def config(tmp_path):
    return _write_config(tmp_path, "notify:\n  push_horizons: [1h, 4h, 1d, 1w]\n  push_tier: deep")


def _seed(store, fair_value: float, price: float = 5.45, eg_known=False, eg_pass=False):
    """A fresh 1h buy on PTON at `price`, with the fair value given."""
    for i in range(30):
        store.upsert_rsi_point(
            RsiPoint("PTON", _stamp(30 - i), price, 33.3, "test", horizon="1h")
        )
    # The stored valuation columns are what `_rescore_signals` maintains; the
    # strong verdict reads them at the horizon margin (10% on 1h).
    confirms = price * 1.10 < fair_value
    store.record_signal(Signal(
        "PTON", _stamp(6), _stamp(4), _stamp(2.5), price, fair_value,
        True, confirms, True, "now", horizon="1h", direction="buy",
        earnings_growth=15.0 if eg_known else None,
        earnings_growth_known=eg_known, earnings_growth_pass=eg_pass,
    ))
    store.upsert_valuation(Valuation("PTON", "2026-08-10", price, fair_value,
                                     "2026-08-10", "manual"))


def _row(config, store, horizon="1h"):
    from screener.dashboard import _collect

    return next(r for r in _collect(store, config, config.horizon(horizon))
                if r.symbol == "PTON")


# ------------------------------------------------------------------ config


def test_the_margin_is_measured_like_every_other_margin():
    """`price * (1 + margin) < fair_value` -- so 0.50 is "fair value 50% above
    the price", the same reading as the daily chart's 30%. Not "half price",
    which would be a margin of 1.00 and catches a tenth as many signals."""
    assert DeepValueConfig().margin == 0.50


def test_deep_value_is_never_looser_than_the_strong_buy_it_extends(config):
    """`max(horizon margin, deep margin)`: on the weekly chart the horizon's own
    margin is already 0.50, so there every strong buy is also deep value."""
    deep = config.deep_value
    assert deep.margin_for(config.horizon("1h")) == 0.50
    assert deep.margin_for(config.horizon("1d")) == 0.50
    assert deep.margin_for(config.horizon("1w")) == config.horizon("1w").margin
    loose = DeepValueConfig(margin=0.05)
    assert loose.margin_for(config.horizon("1d")) == config.horizon("1d").margin


def test_an_unknown_deep_value_key_is_refused(tmp_path):
    """A `margin_pct: 50` would leave the default in force while the file
    appeared to say otherwise -- and this number decides what rings a phone."""
    with pytest.raises(ValueError, match="unknown"):
        _write_config(tmp_path, "deep_value:\n  margin_pct: 50")


def test_a_percentage_typed_as_a_whole_number_is_refused(tmp_path):
    with pytest.raises(ValueError, match="fraction"):
        _write_config(tmp_path, "deep_value:\n  margin: 50")


def test_the_push_tier_defaults_to_the_old_behaviour(tmp_path):
    """An unset key changes nothing for anyone who has not opted in."""
    assert NotifyConfig().push_tier == "strong"
    assert _write_config(tmp_path).notify.push_tier == "strong"


def test_a_misspelt_push_tier_is_refused(tmp_path):
    """Falling back to "strong" on a typo would ring for exactly the strong
    buys the setting was written to silence."""
    with pytest.raises(ValueError, match="push_tier"):
        _write_config(tmp_path, "notify:\n  push_tier: deeep")


# ---------------------------------------------------------------- dashboard


def test_fifty_percent_headroom_is_deep_value(config):
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.52)
        row = _row(config, store)
    assert row.strong and row.deep
    assert row.state == "deep"


def test_just_under_fifty_percent_is_a_strong_buy_and_no_more(config):
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.48)
        row = _row(config, store)
    assert row.strong and not row.deep
    assert row.state == "strong"


def test_shrinking_earnings_veto_deep_value_too(config):
    """Deep value is a strong buy first: the value-trap veto still applies."""
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 3, eg_known=True, eg_pass=False)
        row = _row(config, store)
    assert not row.strong and not row.deep


def test_crypto_can_never_be_deep_value(tmp_path):
    """The tier is defined by a fair value, and an unvalued ticker has none."""
    from screener.config import Ticker
    from screener.dashboard import _deep_dates

    config = _write_config(tmp_path)
    coin = Ticker(symbol="BTC", tradingview="BINANCE:BTCUSDT", morningstar="",
                  markets=("crypto",))
    sig = Signal("BTC", _stamp(6), _stamp(4), _stamp(2.5), 1.0, 100.0,
                 True, True, True, "now", horizon="1h", direction="buy")
    assert not coin.valued
    assert _deep_dates([sig], coin, config, config.horizon("1h")) == frozenset()


def test_a_sell_is_never_deep_value(config):
    from screener.dashboard import _deep_dates

    ticker = config.ticker("PTON")
    sig = Signal("PTON", _stamp(6), _stamp(4), _stamp(2.5), 1.0, 100.0,
                 True, True, True, "now", horizon="1h", direction="sell")
    assert _deep_dates([sig], ticker, config, config.horizon("1h")) == frozenset()


def test_switching_the_tier_off_removes_it_everywhere(config):
    off = replace(config, deep_value=DeepValueConfig(enabled=False))
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 3)
        row = _row(off, store)
    assert row.strong and not row.deep


def test_deep_value_leads_the_page(config):
    """It is the rarest and strongest verdict, so it sorts above the rockets."""
    from screener.dashboard import Row

    def row(symbol, deep):
        sig = Signal(symbol, "2026-07-16", "2026-07-22", "2026-07-23", 100.0, 200.0,
                     True, True, True, "now")
        return Row(symbol=symbol, morningstar_url="", tradingview_url="",
                   series=[RsiPoint(symbol, "2026-07-27", 100.0, 45.0, "t")],
                   crosses=[], valuation=None, signals=[sig],
                   deep_dates=frozenset({"2026-07-23"}) if deep else frozenset())

    assert row("A", True).state == "deep"
    assert row("B", False).state == "strong"


def test_the_card_wears_its_own_badge(config):
    from screener.dashboard import render

    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 2)
        row = _row(config, store)
    html = render([row], config, config.horizon("1h"))
    assert "Deep value 💎" in html
    assert "state-deep" in html
    assert "clears the 50% deep-value bar" in html


def test_only_the_stocks_book_counts_deep_value(config):
    """On the crypto book the tile could only ever read 0."""
    from screener.dashboard import _aggregates

    stocks = _aggregates([], config.horizon("1d"), 30, conviction=True)
    crypto = _aggregates([], config.horizon("1d"), 30, conviction=False)
    assert "Deep 💎" in stocks and "Deep 💎" not in crypto
    assert stocks.count("<div") == 9 and crypto.count("<div") == 6


def test_the_palette_defines_the_tier_colour_everywhere(config):
    """A token missing from one block renders unstyled in that theme."""
    from screener.dashboard import render

    assert render([], config).count("--deep:") == 4


# ------------------------------------------------------------------ journal


def test_the_journal_records_what_the_page_said(config):
    """The record must never disagree with the page, and the page says deep."""
    from screener.journal import recommendation_from

    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 2)
        row = _row(config, store)
    rec = recommendation_from(row, row.buys[0], config.horizon("1h"))
    assert rec.verdict == "deep"


def test_a_plain_strong_buy_is_still_journalled_as_strong(config):
    from screener.journal import recommendation_from

    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.3)
        row = _row(config, store)
    rec = recommendation_from(row, row.buys[0], config.horizon("1h"))
    assert rec.verdict == "strong"


def test_suspension_still_outranks_every_verdict():
    from screener.journal import verdict_for

    assert verdict_for(None, "buy", True, True, deep=True) == "suspended"
    assert verdict_for(None, "buy", False, False, deep=True) == "signal", (
        "deep value is a strong buy first"
    )


# ------------------------------------------------------------- notifications


def _capture(monkeypatch):
    sent = {"push": [], "issue": [], "hook": []}
    monkeypatch.setattr("screener.cli.send_push",
                        lambda t, m, u: sent["push"].append((t, m)) or True)
    monkeypatch.setattr("screener.cli.send_github_issue",
                        lambda t, b, k: sent["issue"].append((t, k)) or True)
    monkeypatch.setattr("screener.cli.send_webhook",
                        lambda m: sent["hook"].append(m) or True)
    return sent


def test_deep_value_rings_the_phone(config, monkeypatch):
    from screener.cli import _notify_new_strong_buys

    sent = _capture(monkeypatch)
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 2)
        assert _notify_new_strong_buys(store, config) == 1
    assert len(sent["push"]) == 1
    title, message = sent["push"][0]
    assert title.startswith("💎 PTON deep value")
    assert "DEEP VALUE 💎 — PTON" in message


def test_a_strong_buy_that_is_not_deep_stays_off_the_phone(config, monkeypatch):
    """The point of the setting. Everything else still carries it."""
    from screener.cli import _notify_new_strong_buys

    sent = _capture(monkeypatch)
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.3)
        assert _notify_new_strong_buys(store, config) == 1
    assert sent["push"] == []
    assert len(sent["issue"]) == 1 and len(sent["hook"]) == 1
    assert sent["issue"][0][0].startswith("🚀 PTON strong buy")


def test_the_run_says_why_the_phone_stayed_quiet(config, monkeypatch, capsys):
    from screener.cli import _notify_new_strong_buys

    _capture(monkeypatch)
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.3)
        _notify_new_strong_buys(store, config)
    assert "only rings for deep value" in capsys.readouterr().out


def test_a_strong_buy_that_becomes_deep_value_rings_then(config, monkeypatch):
    """A fair value re-scraped higher turns yesterday's strong buy into deep
    value. That escalation is news, and under `deep` it is the only version of
    the news that rings -- so it must not dedupe against the earlier one."""
    from screener.cli import _notify_new_strong_buys

    sent = _capture(monkeypatch)
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.3)
        assert _notify_new_strong_buys(store, config) == 1
        store.update_signal_valuation(
            "PTON", _stamp(2.5), 5.45, 5.45 * 2, True, True, True, "1h", "buy",
        )
        assert _notify_new_strong_buys(store, config) == 1
    assert len(sent["push"]) == 1, "only the deep-value announcement rang"


def test_the_old_tier_still_rings_for_every_strong_buy(config, monkeypatch):
    """Switching back is one line, and restores exactly what was there."""
    from screener.cli import _notify_new_strong_buys

    sent = _capture(monkeypatch)
    loud = replace(config, notify=replace(config.notify, push_tier="strong"))
    with Store(config.storage.database) as store:
        _seed(store, fair_value=5.45 * 1.3)
        assert _notify_new_strong_buys(store, loud) == 1
    assert len(sent["push"]) == 1


def test_a_crypto_pattern_does_not_ring_under_deep(tmp_path):
    """Nothing unvalued can be deep value, so under `deep` crypto falls silent
    on the phone -- and stays on the issue tracker."""
    assert not NotifyConfig(push_tier="deep").rings_for("pattern")
    assert not NotifyConfig(push_tier="deep").rings_for("strong")
    assert NotifyConfig(push_tier="deep").rings_for("deep")
    assert NotifyConfig(push_tier="strong").rings_for("pattern")


# ------------------------------------------------------- strategy leaderboard


def test_the_deep_selection_takes_only_deep_trades():
    from screener.strategies import DEEP_ONLY, Selection, Trade

    sel = Selection("x", "x", DEEP_ONLY, ("1d",))
    base = dict(symbol="A", horizon="1d", direction="buy", up2_date="2026-01-01",
                strategy="even", entry=1.0, exit=1.1, return_pct=0.1,
                bars_held=5, outcome="target")
    assert sel.takes(Trade(**base, strong=True, deep=True))
    assert not sel.takes(Trade(**base, strong=True, deep=False))
    assert not sel.takes(Trade(**{**base, "horizon": "1h"}, strong=True, deep=True))


def test_the_three_entry_bars_nest(config):
    """Deep within strong within every buy -- because deep is the strong test
    at a margin that is never narrower."""
    from screener.historical import _retrospective_deep, _retrospective_strong

    h = config.horizon("1d")
    for fair in (100.0, 125.0, 140.0, 149.0, 151.0, 300.0):
        sig = Signal("PTON", "2026-01-01", "2026-01-02", "2026-01-03", 100.0, fair,
                     True, True, True, "now", horizon="1d", direction="buy")
        strong = _retrospective_strong(sig, 100.0, fair, config, h.margin)
        deep = _retrospective_deep(sig, 100.0, fair, config, h)
        assert not deep or strong, f"deep without strong at fair value {fair}"
        assert deep == (fair > 150.0)


def test_the_flag_survives_the_database(tmp_path):
    from screener.strategies import Trade

    with Store(tmp_path / "t.db") as store:
        store.record_trades([
            Trade("A", "1d", "buy", "2026-01-01", "even", 1.0, 1.1, 0.1, 5,
                  "target", True, True),
            Trade("B", "1d", "buy", "2026-01-01", "even", 1.0, 1.1, 0.1, 5,
                  "target", True, False),
        ])
        got = {t.symbol: t.deep for t in store.all_trades()}
    assert got == {"A": True, "B": False}


def test_an_older_database_gains_the_column(tmp_path):
    """Added rather than rebuilt: every row is re-derived by the next evaluate."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE strategy_trades (
        symbol TEXT NOT NULL, horizon TEXT NOT NULL, direction TEXT NOT NULL,
        up2_date TEXT NOT NULL, strategy TEXT NOT NULL, entry REAL NOT NULL,
        exit REAL NOT NULL, return_pct REAL NOT NULL, bars_held INTEGER NOT NULL,
        outcome TEXT NOT NULL, strong INTEGER NOT NULL DEFAULT 0,
        evaluated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, horizon, direction, up2_date, strategy))""")
    db.commit()
    db.close()
    with Store(path) as store:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(strategy_trades)")}
    assert "deep" in cols


def test_the_legend_explains_the_deep_selection():
    from screener.historical import _selection_sentence
    from screener.strategies import DEEP_ONLY, Selection

    text = _selection_sentence(Selection("x", "x", DEEP_ONLY, ("4h", "1d")), "50%")
    assert "deep value" in text and "50%" in text
    assert "today's fair value" in text, "the hindsight caveat must sit where the row is read"


def test_the_shipped_config_has_a_deep_value_strategy():
    config = load_config()
    deep = [s for s in config.strategies.selections if s.entry == "deep"]
    assert deep, "the leaderboard must offer a deep-value-only strategy"
    assert set(deep[0].horizons) == set(config.notify.push_horizons), (
        "it should measure exactly what rings the phone"
    )
