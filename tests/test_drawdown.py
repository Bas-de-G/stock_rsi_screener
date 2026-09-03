"""Tests for the crypto drawdown gate.

The load-bearing one is `test_the_two_legs_disagree_and_that_is_the_point`. A
single all-time-high test was rejected earlier as a restatement of the RSI
signal; two clocks are defensible precisely because they disagree, so that
property is pinned rather than assumed.

Offline throughout.
"""

from __future__ import annotations

import pytest

from screener.drawdown import (
    DEFAULT_ATH_FLOOR,
    Highs,
    drawdown,
    explain,
    gate,
    recent_high,
)


def highs(all_time=100.0, recent=50.0, bars=180, symbol="X") -> Highs:
    return Highs(symbol=symbol, all_time=all_time, recent=recent, recent_bars=bars)


# ----------------------------------------------------------- the arithmetic


def test_a_drawdown_is_a_positive_fraction():
    assert drawdown(50.0, 100.0) == pytest.approx(0.5)
    assert drawdown(90.0, 100.0) == pytest.approx(0.1)


def test_a_price_above_its_high_is_zero_not_negative():
    """A new high is 'not down at all', which is what the gate needs to hear.
    A negative drawdown would compare as less than every threshold and read as
    a very deep discount."""
    assert drawdown(120.0, 100.0) == 0.0


def test_a_missing_or_nonsensical_high_is_unknown():
    assert drawdown(50.0, None) is None
    assert drawdown(None, 100.0) is None
    assert drawdown(50.0, 0.0) is None, "dividing by zero would be a confident nothing"
    assert drawdown(50.0, -10.0) is None


# ---------------------------------------------------------- the recent high


def test_the_recent_high_is_the_highest_close_in_the_window():
    high, bars = recent_high([10, 20, 15, 12], bars=180)
    assert (high, bars) == (20, 4)


def test_the_window_only_looks_back_that_far():
    """The high from two years ago is the all-time leg's business."""
    high, bars = recent_high([999, 10, 20, 15], bars=3)
    assert (high, bars) == (20, 3), "999 is outside the window"


def test_the_bar_count_comes_back_so_a_short_window_can_be_refused():
    _, bars = recent_high([10, 20], bars=180)
    assert bars == 2, "two bars is not a six-month high, and the caller must know"


# ------------------------------------------------------------- the gate


def test_both_legs_must_pass():
    # 60% below the all-time high, 50% below the recent one.
    price = 40.0
    assert gate(price, highs(all_time=100.0, recent=80.0), margin=0.30)[1]

    # Deep against the all-time high, but barely off the recent one.
    assert not gate(price, highs(all_time=100.0, recent=41.0), margin=0.30)[1]

    # Well off the recent high, but nowhere near a record drawdown.
    assert not gate(price, highs(all_time=45.0, recent=80.0), margin=0.30)[1]


def test_the_recent_leg_scales_with_the_horizon():
    """Same asset, different holding periods. 20% below its 6-month high
    qualifies for an hour's trade and not for a week's, exactly as the equity
    gate demands more headroom on the slower charts."""
    price, h = 80.0, highs(all_time=1000.0, recent=100.0)
    assert gate(price, h, margin=0.10)[1], "1h wants 10%"
    assert gate(price, h, margin=0.20)[1], "4h wants 20%"
    assert not gate(price, h, margin=0.30)[1], "1d wants 30%"
    assert not gate(price, h, margin=0.50)[1], "1w wants 50%"


def test_the_two_legs_disagree_and_that_is_the_point():
    """Real assets from the watchlist. A single all-time-high test would pass
    all three; requiring both separates them, which is the whole justification
    for the second leg."""
    zec = gate(819.61, highs(all_time=3191.93, recent=852.19), margin=0.30)
    uni = gate(5.693, highs(all_time=44.92, recent=5.693), margin=0.30)
    bch = gate(245.0, highs(all_time=3785.82, recent=480.67), margin=0.30)

    assert all(g[0] for g in (zec, uni, bch)), "all three are gradeable"
    assert not zec[1], "collapsed years ago, but 4% off its 6-month high"
    assert not uni[1], "87% below its record and sitting AT its 6-month high"
    assert bch[1], "falling on both clocks"


def test_an_asset_with_no_highs_is_unknown_not_failing():
    """Unknown must not read as 'does not qualify' -- `is_strong` treats a
    missing gate as no rocket either way, but the card says different things."""
    assert gate(50.0, None, margin=0.30) == (False, False)
    assert gate(50.0, highs(all_time=None), margin=0.30) == (False, False)
    assert gate(None, highs(), margin=0.30) == (False, False)


def test_too_short_a_history_is_refused_rather_than_graded():
    """Three weeks of bars is not a six-month high, and grading against one
    while the card says '6-month' is the quiet misdescription this guards."""
    young = highs(all_time=1000.0, recent=100.0, bars=20)
    assert gate(10.0, young, margin=0.30, min_bars=120) == (False, False)
    assert gate(10.0, young, margin=0.30, min_bars=0)[1], "the same asset passes uncapped"


def test_the_floor_is_configurable_and_defaults_to_half():
    assert DEFAULT_ATH_FLOOR == 0.5
    price, h = 40.0, highs(all_time=100.0, recent=80.0)
    assert not gate(price, h, margin=0.30, ath_floor=0.8)[1], "60% is not 80%"
    assert gate(price, h, margin=0.30, ath_floor=0.5)[1]


# --------------------------------------------------------------- the words


def test_the_explanation_names_both_legs_and_both_thresholds():
    """'Does not qualify' cannot distinguish an asset that has barely fallen
    from one that fell long ago and recovered — opposite situations."""
    text = explain(40.0, highs(all_time=100.0, recent=80.0), margin=0.30)
    assert "60% below its all-time high" in text
    assert "50% below its 6-month high" in text
    assert "needs 50% and 30%" in text


def test_the_explanation_says_when_there_is_nothing_to_say():
    assert explain(40.0, None, margin=0.30) == "No highs recorded yet"
