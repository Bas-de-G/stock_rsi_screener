"""Tests for the up/down/up RSI pattern and the valuation gate."""

from __future__ import annotations

import datetime as dt

import pytest

from screener.config import SignalConfig
from screener.signals import (
    earnings_growth_passes,
    find_cross_pairs,
    find_upward_crosses,
    is_strong,
    signal_fires,
    valuation_passes,
)
from screener.storage import RsiPoint

THRESHOLD = 30.0
START = dt.date(2026, 1, 5)  # a Monday


def series(*rsis: float, symbol: str = "TEST", step_days: int = 1) -> list[RsiPoint]:
    """Build an RSI series with one point per `step_days`, starting at START."""
    points = []
    for i, rsi in enumerate(rsis):
        date = (START + dt.timedelta(days=i * step_days)).isoformat()
        points.append(RsiPoint(symbol, date, 100.0, rsi, "test"))
    return points


def config(**overrides) -> SignalConfig:
    base = dict(window_days=14, window_unit="calendar", valuation_rule="fair_value_below_price")
    base.update(overrides)
    return SignalConfig(**base)


# ------------------------------------------------------------ cross finding


def test_finds_simple_upward_cross():
    assert find_upward_crosses(series(25, 28, 35), THRESHOLD) == [2]


def test_touching_threshold_exactly_counts_as_a_cross():
    # "goes up to 30 (crosses it)" — reaching 30 from below is the cross.
    assert find_upward_crosses(series(25, 30.0), THRESHOLD) == [1]


def test_no_cross_when_staying_below():
    assert find_upward_crosses(series(20, 25, 28, 29.9), THRESHOLD) == []


def test_no_cross_when_staying_above():
    assert find_upward_crosses(series(40, 55, 62), THRESHOLD) == []


def test_downward_cross_is_not_an_upward_cross():
    assert find_upward_crosses(series(45, 20), THRESHOLD) == []


# ------------------------------------------------------------ the pattern


def test_full_pattern_within_window_is_detected():
    # below 30 -> up through 30 -> back below -> up through 30 again
    points = series(25, 34, 27, 36)
    pairs = find_cross_pairs(points, THRESHOLD, config())
    assert len(pairs) == 1
    pair = pairs[0]
    assert (pair.up1_date, pair.down_date, pair.up2_date) == (
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
    )
    assert pair.span_days == 2


def test_single_cross_is_not_a_signal():
    assert find_cross_pairs(series(25, 34, 40, 50), THRESHOLD, config()) == []


def test_second_cross_outside_window_is_rejected():
    # crosses on day 1 and day 21 — 20 calendar days apart, window is 14
    points = series(25, 34, *([27] * 18), 36)
    pairs = find_cross_pairs(points, THRESHOLD, config())
    assert pairs == []


def test_second_cross_exactly_on_window_boundary_is_accepted():
    # cross #1 at index 1, cross #2 at index 15 => 14 calendar days apart
    points = series(25, 34, *([27] * 13), 36)
    pairs = find_cross_pairs(points, THRESHOLD, config())
    assert len(pairs) == 1
    assert pairs[0].span_days == 14


def test_one_day_past_the_boundary_is_rejected():
    points = series(25, 34, *([27] * 14), 36)
    pairs = find_cross_pairs(points, THRESHOLD, config())
    assert pairs == []


def test_only_consecutive_crosses_are_paired():
    """Crosses on days 1, 3 and 5 give pairs (1,3) and (3,5) — never (1,5)."""
    points = series(25, 34, 27, 36, 28, 33)
    pairs = find_cross_pairs(points, THRESHOLD, config())
    assert [(p.up1_date, p.up2_date) for p in pairs] == [
        ("2026-01-06", "2026-01-08"),
        ("2026-01-08", "2026-01-10"),
    ]


def test_calendar_window_accounts_for_weekend_gaps():
    """Trading days skip weekends; 14 calendar days is a shorter run of bars."""
    # 11 trading-day steps that span more than 14 calendar days
    points = series(25, 34, *([27] * 9), 36, step_days=2)
    pairs_calendar = find_cross_pairs(points, THRESHOLD, config())
    pairs_trading = find_cross_pairs(points, THRESHOLD, config(window_unit="trading"))
    assert pairs_calendar == []          # 20 calendar days apart -> too far
    assert len(pairs_trading) == 1       # 10 bars apart -> inside a 14-bar window


def test_dip_date_is_the_last_day_below_threshold():
    points = series(25, 34, 20, 22, 28, 31)
    pair = find_cross_pairs(points, THRESHOLD, config())[0]
    assert pair.down_date == "2026-01-09"  # index 4, the last sub-30 day


def test_empty_and_short_series_are_safe():
    assert find_cross_pairs([], THRESHOLD, config()) == []
    assert find_cross_pairs(series(25), THRESHOLD, config()) == []


# ------------------------------------------------------------ valuation gate


@pytest.mark.parametrize(
    "rule,price,fair_value,expected",
    [
        # Configured rule: fair value < price
        ("fair_value_below_price", 217.24, 225.00, False),  # FV above price
        ("fair_value_below_price", 240.00, 225.00, True),   # FV below price
        # The inverse rule
        ("price_below_fair_value", 217.24, 225.00, True),
        ("price_below_fair_value", 240.00, 225.00, False),
    ],
)
def test_valuation_gate_directions(rule, price, fair_value, expected):
    known, passed = valuation_passes(price, fair_value, config(valuation_rule=rule))
    assert known is True
    assert passed is expected


def test_equal_price_and_fair_value_does_not_pass_either_rule():
    for rule in ("fair_value_below_price", "price_below_fair_value"):
        known, passed = valuation_passes(200.0, 200.0, config(valuation_rule=rule))
        assert known is True
        assert passed is False


def test_missing_valuation_is_reported_as_unknown():
    """No figures to compare means the gate has no opinion either way."""
    known, confirms = valuation_passes(None, None, config())
    assert known is False
    assert confirms is False


def test_partial_valuation_counts_as_missing():
    known, _ = valuation_passes(217.0, None, config())
    assert known is False


# ------------------------------------------------ firing vs. confirming


def test_pattern_alone_fires_when_configured_to():
    """The RSI pattern is a buy signal on its own; fair value only grades it."""
    known, confirms = valuation_passes(None, None, config(fire_without_valuation=True))
    assert signal_fires(confirms, config(fire_without_valuation=True)) is True
    assert is_strong((known, confirms)) is False


def test_pattern_alone_does_not_fire_in_strict_mode():
    known, confirms = valuation_passes(None, None, config(fire_without_valuation=False))
    assert signal_fires(confirms, config(fire_without_valuation=False)) is False
    assert is_strong((known, confirms)) is False


def test_confirming_valuation_makes_it_a_strong_buy():
    cfg = config(fire_without_valuation=True, valuation_rule="price_below_fair_value")
    known, confirms = valuation_passes(217.24, 225.00, cfg)   # below fair value
    assert signal_fires(confirms, cfg) is True
    assert is_strong((known, confirms)) is True


def test_contradicting_valuation_still_fires_but_is_not_strong():
    """Trading above fair value doesn't cancel the RSI signal, just downgrades it."""
    cfg = config(fire_without_valuation=True, valuation_rule="price_below_fair_value")
    known, confirms = valuation_passes(240.00, 225.00, cfg)   # above fair value
    assert signal_fires(confirms, cfg) is True
    assert is_strong((known, confirms)) is False


def test_contradicting_valuation_blocks_the_signal_in_strict_mode():
    cfg = config(fire_without_valuation=False, valuation_rule="price_below_fair_value")
    known, confirms = valuation_passes(240.00, 225.00, cfg)
    assert signal_fires(confirms, cfg) is False
    assert is_strong((known, confirms)) is False


def test_a_confirmed_signal_is_strong_under_either_mode():
    for fire in (True, False):
        cfg = config(fire_without_valuation=fire, valuation_rule="price_below_fair_value")
        known, confirms = valuation_passes(217.24, 225.00, cfg)
        assert signal_fires(confirms, cfg) is True
        assert is_strong((known, confirms)) is True


# ------------------------------------------------- earnings growth gate


def test_missing_earnings_growth_is_unknown():
    known, confirms = earnings_growth_passes(None)
    assert (known, confirms) == (False, False)


def test_positive_growth_confirms():
    known, confirms = earnings_growth_passes(32.6)
    assert (known, confirms) == (True, True)


def test_negative_growth_does_not_confirm():
    known, confirms = earnings_growth_passes(-47.1)
    assert (known, confirms) == (True, False)


def test_zero_growth_does_not_confirm():
    """Flat earnings aren't growing earnings -- no threshold to tune, just > 0."""
    known, confirms = earnings_growth_passes(0.0)
    assert (known, confirms) == (True, False)


# --------------------------------------- is_strong across multiple factors


def test_nothing_known_is_never_strong():
    """Not even the coin-flip case: with zero factors checked, no rocket."""
    assert is_strong((False, False), (False, False)) is False


def test_a_single_known_factor_confirming_is_enough():
    """A fresh pattern with no fair value yet can still be strong on
    earnings growth alone -- and vice versa."""
    assert is_strong((False, False), (True, True)) is True
    assert is_strong((True, True), (False, False)) is True


def test_a_single_known_factor_denying_blocks_it():
    assert is_strong((False, False), (True, False)) is False
    assert is_strong((True, False), (False, False)) is False


def test_both_known_and_agreeing_is_strong():
    assert is_strong((True, True), (True, True)) is True


def test_both_known_but_disagreeing_is_not_strong():
    """One dissenting *known* factor is enough to withhold the rocket --
    this is the value-trap case: cheap and oversold, but earnings shrinking."""
    assert is_strong((True, True), (True, False)) is False
    assert is_strong((True, False), (True, True)) is False


def test_is_strong_accepts_any_number_of_factors():
    """Extensible to a third factor later without changing the call shape."""
    assert is_strong((True, True), (True, True), (True, True)) is True
    assert is_strong((True, True), (True, True), (True, False)) is False
    assert is_strong((False, False), (False, False), (True, True)) is True
