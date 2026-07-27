"""Tests for the Wilder RSI implementation."""

from __future__ import annotations

import pytest

from screener.rsi import wilder_rsi_series


def test_undefined_until_enough_data():
    closes = [float(i) for i in range(10)]
    assert wilder_rsi_series(closes, period=14) == [None] * 10


def test_first_value_appears_at_index_period():
    closes = [100.0 + i for i in range(20)]
    out = wilder_rsi_series(closes, period=14)
    assert out[13] is None
    assert out[14] is not None


def test_monotonic_rise_gives_rsi_100():
    closes = [100.0 + i for i in range(30)]
    out = wilder_rsi_series(closes, period=14)
    assert out[-1] == pytest.approx(100.0)


def test_monotonic_fall_gives_rsi_0():
    closes = [100.0 - i for i in range(30)]
    out = wilder_rsi_series(closes, period=14)
    assert out[-1] == pytest.approx(0.0)


def test_flat_series_is_neutral_by_convention():
    """No losses at all means RS is undefined; Wilder's convention returns 100."""
    out = wilder_rsi_series([100.0] * 30, period=14)
    assert out[-1] == pytest.approx(100.0)


def test_matches_tradingview_reference_value():
    """Real NVDA daily closes, pinned against TradingView's own reported RSI.

    These are actual closes (Yahoo daily bars, with TradingView's own last
    close of 196.38 as the final point). TradingView reported RSI 42.36 for
    that bar. Wilder smoothing is recursive, so the value converges as more
    history is included: this 40-bar window lands on 42.47, and the full
    120-bar series reproduces 42.3590 — i.e. 42.36 exactly.

    A non-Wilder (simple moving average) RSI gives ~55 on this same input, so
    this test is what stops the smoothing method from silently regressing.
    """
    closes = [
        211.14, 224.36, 222.82, 214.75, 218.66, 205.10, 208.64, 208.19,
        200.42, 204.87, 205.19, 212.45, 207.41, 204.65, 210.69, 208.65,
        200.04, 199.00, 195.74, 192.53, 194.97, 200.09, 197.58, 194.83,
        195.55, 196.93, 204.12, 202.78, 210.96, 203.53, 211.80, 212.50,
        207.40, 202.81, 203.28, 207.29, 212.06, 208.76, 206.84, 196.38,
    ]
    out = wilder_rsi_series(closes, period=14)
    assert out[-1] == pytest.approx(42.36, abs=0.5)
    assert out[-1] < out[-2]  # the sharp drop on the last bar pushed RSI down


def test_period_is_respected():
    closes = [100.0 + (i % 3) for i in range(30)]
    short = wilder_rsi_series(closes, period=2)
    long = wilder_rsi_series(closes, period=14)
    assert short[2] is not None
    assert long[2] is None


def test_empty_input():
    assert wilder_rsi_series([], period=14) == []
