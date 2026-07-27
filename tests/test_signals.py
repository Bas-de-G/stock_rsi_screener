"""Tests for the up/down/up RSI pattern and the valuation gate."""

from __future__ import annotations

import datetime as dt

import pytest

from screener.config import SignalConfig
from screener.signals import find_cross_pairs, find_upward_crosses, valuation_passes
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


def test_missing_valuation_blocks_the_signal_by_default():
    known, passed = valuation_passes(None, None, config())
    assert known is False
    assert passed is False


def test_missing_valuation_can_be_configured_to_fire():
    known, passed = valuation_passes(None, None, config(fire_without_valuation=True))
    assert known is False
    assert passed is True


def test_partial_valuation_counts_as_missing():
    known, _ = valuation_passes(217.0, None, config())
    assert known is False
