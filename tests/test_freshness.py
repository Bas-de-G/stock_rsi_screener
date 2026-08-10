"""Tests for "just fired" — the freshness badge and the deal of the day.

Freshness is a different question from liveness. `signal_is_live` asks whether
a setup is still valid, and its window is generous by design: a 1d signal
stays live for a fortnight. That leaves a pattern which closed yesterday
indistinguishable from one which closed thirteen days ago, even though only
the first is an entry near the bounce. Offline throughout.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screener.config import DEFAULT_HORIZONS, FRESH_BARS, load_config
from screener.dashboard import Row, _deal_of_the_day, render
from screener.signals import signal_age, signal_is_fresh
from screener.storage import RsiPoint, Signal, Valuation

# Anchored to the real clock: `signal_is_fresh` refuses to call anything fresh
# when the newest bar is more than a day old, so a fixture pinned to a literal
# date would stop being fresh the day after it was written.
NOW = dt.datetime.now()


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("""
tickers:
  - {symbol: NVDA, tradingview: "NASDAQ:NVDA", morningstar: xnas/nvda, markets: [sp500]}
rsi: {period: 14, threshold: 30, overbought: 70, interval: "1D"}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage: {database: data/t.db, csv_dir: data}
dashboard: {output: data/t.html, chart_days: 90}
""")
    return load_config(path)


def _stamp(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="minutes")


def bars(symbol="NVDA", n=40, step_h=4, rsi=45.0):
    return [
        RsiPoint(symbol, _stamp(step_h * (n - 1 - i)), 100.0, rsi, "test")
        for i in range(n)
    ]


def signal(up2_hours_ago, direction="buy", known=True, confirms=True,
           price=100.0, fair=150.0, fired=True, eg_known=True, eg_pass=True):
    return Signal(
        "NVDA", _stamp(up2_hours_ago + 8), _stamp(up2_hours_ago + 4),
        _stamp(up2_hours_ago), price, fair, known, confirms, fired, "now",
        earnings_growth=12.0 if eg_known else None,
        earnings_growth_known=eg_known, earnings_growth_pass=eg_pass,
        horizon="4h", direction=direction,
    )


def row(horizon, signals, symbol="NVDA", price=100.0, fair=150.0, currency="USD"):
    return Row(
        symbol=symbol, morningstar_url="#", tradingview_url="#",
        series=bars(symbol), crosses=[], signals=signals,
        valuation=Valuation(symbol, "2026-08-10", price, fair, "2026-08-10", "manual"),
        currency=currency, horizon=horizon,
    )


# ------------------------------------------------ the horizon's own bar


def test_each_horizon_knows_how_long_one_bar_lasts():
    assert {h.key: h.bar_hours for h in DEFAULT_HORIZONS} == {
        "1h": 1, "4h": 4, "1d": 24, "1w": 168,
    }


@pytest.mark.parametrize(
    "key,expected", [("1h", "2 hours"), ("4h", "8 hours"), ("1d", "2 days"), ("1w", "2 weeks")]
)
def test_freshness_window_scales_with_the_timeframe(key, expected):
    horizon = next(h for h in DEFAULT_HORIZONS if h.key == key)
    assert horizon.fresh_label == expected
    assert horizon.fresh_within == dt.timedelta(hours=horizon.bar_hours * FRESH_BARS)


def test_a_horizons_override_does_not_reset_the_bar_length(tmp_path):
    """config.yaml tunes window/margin/leverage. bar_hours describes what the
    timeframe *is*, so it must survive an override untouched."""
    path = tmp_path / "config.yaml"
    path.write_text("""
tickers:
  - {symbol: NVDA, tradingview: "NASDAQ:NVDA", morningstar: xnas/nvda}
rsi: {period: 14, threshold: 30, interval: "1D"}
signal: {window_days: 14, window_unit: calendar, valuation_rule: price_below_fair_value}
storage: {database: data/t.db, csv_dir: data}
dashboard: {output: data/t.html, chart_days: 90}
horizons:
  "1h": {window_days: 3, margin: 0.15, leverage: 8}
""")
    horizon = load_config(path).horizon("1h")
    assert horizon.window_days == 3 and horizon.leverage == 8   # override applied
    assert horizon.bar_hours == 1                               # and not clobbered


# ------------------------------------------------------------ the predicate


def test_a_pattern_from_one_bar_ago_is_fresh(config):
    h = config.horizon("4h")
    assert signal_is_fresh(signal(up2_hours_ago=4), bars(), h)


def test_a_pattern_exactly_on_the_boundary_is_fresh(config):
    h = config.horizon("4h")   # two bars = 8 hours
    assert signal_is_fresh(signal(up2_hours_ago=8), bars(), h)


def test_a_pattern_past_the_boundary_is_not_fresh(config):
    h = config.horizon("4h")
    assert not signal_is_fresh(signal(up2_hours_ago=9), bars(), h)


def test_nothing_is_fresh_once_the_feed_has_stalled(config):
    """The screener stops publishing whenever CI does. A pattern at the end of
    a three-week-old series is fresh *relative to that series*, and calling it
    a deal would be badge-shaped nonsense."""
    h = config.horizon("4h")
    stale = [RsiPoint("NVDA", _stamp(500 + 4 * i), 100.0, 45.0, "test") for i in range(10)][::-1]
    # up2 sits four hours before the newest bar -- fresh by the series, stale by the clock
    old = Signal("NVDA", _stamp(512), _stamp(508), _stamp(504), 100.0, 150.0,
                 True, True, True, "now", horizon="4h", direction="buy")
    assert signal_age(old, stale) == dt.timedelta(hours=4)
    assert not signal_is_fresh(old, stale, h)


def test_a_pattern_dated_after_its_own_series_is_not_fresh(config):
    """A negative age is nonsense, not extreme freshness."""
    h = config.horizon("4h")
    behind = [RsiPoint("NVDA", _stamp(40 + 4 * i), 100.0, 45.0, "test") for i in range(10)][::-1]
    assert not signal_is_fresh(signal(up2_hours_ago=1), behind, h)


def test_age_on_an_empty_series_is_unknown(config):
    assert signal_age(signal(up2_hours_ago=1), []) is None
    assert not signal_is_fresh(signal(up2_hours_ago=1), [], config.horizon("4h"))


# ------------------------------------------------------------- the row


def test_a_row_with_a_fresh_signal_is_fresh(config):
    assert row(config.horizon("4h"), [signal(up2_hours_ago=4)]).fresh


def test_a_row_whose_signals_are_all_stale_is_not_fresh(config):
    assert not row(config.horizon("4h"), [signal(up2_hours_ago=40)]).fresh


def test_a_row_without_a_horizon_never_claims_freshness():
    """Rows built by older callers have no horizon and must degrade quietly."""
    r = Row(symbol="NVDA", morningstar_url="#", tradingview_url="#",
            series=bars(), crosses=[], valuation=None, signals=[signal(2)])
    assert r.fresh is False
    assert r.deal_discount is None


# ------------------------------------------------ deal-of-the-day eligibility


def test_a_fresh_confirmed_buy_is_a_candidate(config):
    r = row(config.horizon("4h"), [signal(up2_hours_ago=4)])
    assert r.deal_discount == pytest.approx(0.5)   # 100 -> 150


def test_a_sell_is_never_a_candidate(config):
    """You do not get a bargain by selling, so the word would be wrong."""
    r = row(config.horizon("4h"), [signal(up2_hours_ago=4, direction="sell")])
    assert r.deal_discount is None


def test_a_stale_buy_is_not_a_candidate(config):
    r = row(config.horizon("4h"), [signal(up2_hours_ago=40)])
    assert r.deal_discount is None


def test_an_unvalued_buy_is_not_a_candidate(config):
    """Fresh but unpriced is timely, not a known discount."""
    r = row(config.horizon("4h"), [signal(up2_hours_ago=4, known=False, confirms=False)])
    assert r.deal_discount is None


def test_a_buy_the_valuation_contradicts_is_not_a_candidate(config):
    r = row(config.horizon("4h"), [signal(up2_hours_ago=4, confirms=False)])
    assert r.deal_discount is None


def test_declining_earnings_veto_a_candidate(config):
    r = row(config.horizon("4h"), [signal(up2_hours_ago=4, eg_known=True, eg_pass=False)])
    assert r.deal_discount is None


# --------------------------------------------------------- the rendered pick


def test_the_biggest_fresh_discount_wins(config):
    h = config.horizon("4h")
    rows = [
        row(h, [signal(up2_hours_ago=4, price=100.0, fair=190.0)], symbol="CHEAP"),
        row(h, [signal(up2_hours_ago=2, price=100.0, fair=140.0)], symbol="OKAY"),
    ]
    out = _deal_of_the_day(rows, h, 30.0)
    assert "CHEAP" in out and "OKAY" not in out
    assert "90%" in out


def test_a_stale_bargain_loses_to_a_fresh_smaller_one(config):
    """Freshness gates entry; the discount only ranks what already qualifies."""
    h = config.horizon("4h")
    rows = [
        row(h, [signal(up2_hours_ago=99, price=100.0, fair=400.0)], symbol="STALE"),
        row(h, [signal(up2_hours_ago=4, price=100.0, fair=140.0)], symbol="FRESH"),
    ]
    out = _deal_of_the_day(rows, h, 30.0)
    assert "FRESH" in out and "STALE" not in out


def test_nothing_qualifying_renders_nothing(config):
    h = config.horizon("4h")
    rows = [row(h, [signal(up2_hours_ago=4, direction="sell")])]
    assert _deal_of_the_day(rows, h, 30.0) == ""


def test_the_page_carries_the_deal_and_the_badge(config):
    h = config.horizon("4h")
    out = render([row(h, [signal(up2_hours_ago=4)])], config, h)
    assert '<section class="deal"' in out
    assert "Deal of the day" in out
    assert 'class="fresh"' in out


def test_the_page_omits_the_deal_when_none_qualifies(config):
    h = config.horizon("4h")
    out = render([row(h, [signal(up2_hours_ago=40)])], config, h)
    assert '<section class="deal"' not in out
    assert 'class="fresh"' not in out


def test_every_fresh_badge_has_a_style_rule(config):
    """A badge with no CSS behind it renders as unstyled text mid-heading."""
    h = config.horizon("4h")
    out = render([row(h, [signal(up2_hours_ago=4)])], config, h)
    assert ".fresh {" in out
    assert ".deal {" in out
