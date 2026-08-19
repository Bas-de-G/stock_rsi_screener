"""Tests for measuring what happened after a signal.

The arithmetic is easy; the traps are not. Two of them cost real numbers during
development and both are pinned here: measuring a signal the price history does
not reach back to, and comparing a signal's hit rate against nothing.

Offline: every price series below is a fixture.
"""

from __future__ import annotations

import pytest

from screener.outcomes import (
    FORWARD_BARS,
    baseline_outcomes,
    forward_outcomes,
    summarise,
)


def series(start_day: int, closes) -> list[tuple[str, float]]:
    """Daily (date, close) pairs from 2026-01-`start_day`, weekends ignored."""
    return [(f"2026-01-{start_day + i:02d}", c) for i, c in enumerate(closes)]


RISING = series(1, [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110])
FALLING = series(1, [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90])


# ------------------------------------------------------------ the maths


def test_a_buy_followed_by_a_rise_scores_positive():
    [o] = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, RISING, bars=(5,))
    assert o.exit == 105.0
    assert o.return_pct == pytest.approx(0.05)


def test_a_sell_followed_by_a_fall_also_scores_positive():
    """Signed to the call. Without this a hit rate over a mixed sample measures
    nothing — sells outnumber buys most weeks, and their gains would read as
    losses."""
    [o] = forward_outcomes("X", "1d", "sell", "2026-01-01", 100.0, FALLING, bars=(5,))
    assert o.exit == 95.0
    assert o.return_pct == pytest.approx(0.05)


def test_a_sell_followed_by_a_rise_scores_negative():
    [o] = forward_outcomes("X", "1d", "sell", "2026-01-01", 100.0, RISING, bars=(5,))
    assert o.return_pct == pytest.approx(-0.05)


def test_the_entry_bar_itself_is_not_part_of_the_window():
    """The signal completes on that bar, so the first thing measurable against
    it is the next close."""
    [o] = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, RISING, bars=(1,))
    assert o.exit == 101.0


def test_the_excursions_are_the_best_and_worst_inside_the_window():
    wobbly = series(1, [100, 120, 80, 110, 105, 106])
    [o] = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, wobbly, bars=(4,))
    assert o.max_gain == pytest.approx(0.20)
    assert o.max_drawdown == pytest.approx(-0.20)
    assert o.return_pct == pytest.approx(0.05)


def test_a_window_the_history_does_not_reach_is_omitted():
    """A missing row and a zero return must never look alike."""
    out = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, RISING, bars=FORWARD_BARS)
    assert [o.bars for o in out] == [1, 5]


def test_no_entry_price_means_no_measurement():
    assert forward_outcomes("X", "1d", "buy", "2026-01-01", None, RISING) == []


# ---------------------------------------------- the look-ahead this had


def test_a_signal_years_before_the_history_is_not_measured():
    """The bug both guards exist for.

    Each horizon is backfilled to its own depth — five years of weekly bars
    against two of daily ones — so a 2023 weekly pattern sits years before the
    first daily bar. Without a check, "the next twenty bars" silently became
    twenty bars from two years later: PLTR's August 2023 sell was scored at
    -1052%, entering at 15.41 and exiting at 177.57 in September 2025.
    """
    assert forward_outcomes("PLTR", "1w", "sell", "2023-08-07", 15.41, RISING) == []


def test_a_signal_just_before_the_history_is_not_measured_either():
    """Isolates the coverage check from the span one.

    Four days out, so the dates look perfectly plausible and only the coverage
    rule catches it. It still has to: nothing is known about the price between
    the signal and the first bar, so calling that first bar "one day later" is
    a measurement of something else.
    """
    assert forward_outcomes("X", "1d", "buy", "2025-12-28", 100.0, RISING, bars=(1,)) == []


def test_a_signal_on_the_first_bar_of_the_history_is_measured():
    """The boundary: covered means the history reaches the signal's own day."""
    out = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, RISING, bars=(1,))
    assert len(out) == 1


def test_a_gap_in_the_series_stops_the_measurement():
    """A delisting, a suspension, or a stretch CI missed would otherwise
    stretch 'twenty trading days' across months without saying so."""
    gappy = [("2026-01-01", 100.0), ("2026-01-02", 101.0), ("2027-06-01", 300.0)]
    out = forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, gappy, bars=(1, 2))
    assert [o.bars for o in out] == [1], "the second bar is 17 months away"


# ------------------------------------------------------------- baseline


def test_the_baseline_measures_entries_chosen_for_no_reason():
    """Equities drift upward, so any long posts a hit rate above half over a
    rising sample. The coin's score is what makes the signal's readable."""
    base = baseline_outcomes("X", RISING, step=2, bars=(1,))
    assert len(base) == 5
    assert all(o.return_pct > 0 for o in base), "every entry in a rising series wins"


def test_the_baseline_is_not_silently_empty():
    """It was: `forward_outcomes` is handed the whole series and does its own
    filtering, and passing it the slice after the entry failed the coverage
    check on every single bar."""
    assert baseline_outcomes("X", RISING, step=5, bars=(1,)) != []


def test_the_baseline_can_run_short():
    base = baseline_outcomes("X", RISING, direction="sell", step=2, bars=(1,))
    assert all(o.return_pct < 0 for o in base)


# ------------------------------------------------------------ summarise


def test_a_summary_reports_hit_rate_and_spread():
    out = (
        forward_outcomes("X", "1d", "buy", "2026-01-01", 100.0, RISING, bars=(5,))
        + forward_outcomes("Y", "1d", "buy", "2026-01-01", 100.0, FALLING, bars=(5,))
    )
    s = summarise(out)
    assert s["n"] == 2
    assert s["hit_rate"] == 0.5
    assert s["mean"] == pytest.approx(0.0)


def test_an_empty_cohort_summarises_to_nothing_rather_than_crashing():
    assert summarise([]) == {"n": 0}


def test_the_median_is_reported_because_the_mean_lies():
    """One 6x winner drags a mean somewhere the typical trade never went, and
    the typical trade is what someone acting on this actually gets."""
    flat = series(1, [100, 100, 100, 100, 100, 100])
    moon = series(1, [100, 100, 100, 100, 100, 600])
    out = []
    for i in range(9):
        out += forward_outcomes(f"S{i}", "1d", "buy", "2026-01-01", 100.0, flat, bars=(5,))
    out += forward_outcomes("MOON", "1d", "buy", "2026-01-01", 100.0, moon, bars=(5,))
    s = summarise(out)
    assert s["mean"] == pytest.approx(0.5)
    assert s["median"] == pytest.approx(0.0)
