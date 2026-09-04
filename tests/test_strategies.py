"""Tests for the exit rules.

The one that matters is `test_which_barrier_came_first_decides_the_trade`. Every
other test here guards arithmetic; that one guards the reason this module exists
instead of a SQL query over `outcomes.max_gain` and `max_drawdown`.

Offline throughout.
"""

from __future__ import annotations

import pytest

from screener.config import _load_strategies
from screener.strategies import (
    ALL_BUYS,
    STOPPED,
    STRONG_ONLY,
    TARGET,
    TIMEOUT,
    ExitRule,
    Selection,
    Strategy,
    compare,
    leaderboard,
    summarise,
    walk,
)


def rule(take=5.0, stop=5.0, bars=60, key="t") -> ExitRule:
    return ExitRule(key=key, label=key, take_profit=take, stop_loss=stop, max_bars=bars)


def series(*closes, start="2026-01-02"):
    """Daily bars from the signal day onward. Index 0 IS the signal's own day,
    which `forward_series` drops -- the same shape the store hands over."""
    import datetime as dt

    day = dt.date.fromisoformat(start)
    out = []
    for close in closes:
        out.append((day.isoformat(), float(close)))
        day += dt.timedelta(days=1)
    return out


def take(closes, entry=100.0, direction="buy", strategy=None, up2="2026-01-02"):
    return walk("X", "1d", direction, up2, entry,
                series(*closes), strategy or rule())


# ------------------------------------------------------------- the walk


def test_a_target_is_taken_the_day_it_is_reached():
    trade = take([100, 102, 106], strategy=rule(take=5.0, stop=5.0))
    assert trade.outcome == TARGET
    assert trade.bars_held == 2, "the signal's own bar is not a holding day"
    assert trade.return_pct == pytest.approx(0.06)


def test_a_stop_is_taken_the_day_it_is_breached():
    trade = take([100, 98, 93], strategy=rule(take=5.0, stop=5.0))
    assert trade.outcome == STOPPED
    assert trade.return_pct == pytest.approx(-0.07)


def test_neither_barrier_is_a_timeout_not_a_loss():
    trade = take([100, 101, 102, 101], strategy=rule(take=5.0, stop=5.0))
    assert trade.outcome == TIMEOUT
    assert trade.return_pct == pytest.approx(0.01)
    assert trade.won, "a timeout can close green"


def test_the_window_closes_the_position():
    trade = take([100, 101, 102, 103, 104], strategy=rule(take=99.0, stop=99.0, bars=2))
    assert trade.outcome == TIMEOUT
    assert trade.bars_held == 2
    assert trade.exit == 102.0, "closed at the end of its own window, not the series"


# --------------------------------------------- the reason this module exists


def test_which_barrier_came_first_decides_the_trade():
    """A path touching both barriers is a win or a loss depending purely on
    order. `max_gain` and `max_drawdown` cannot tell these two apart -- both
    report +8% and -8% -- which is why the extremes cannot drive a strategy."""
    up_first = take([100, 108, 92], strategy=rule(take=5.0, stop=5.0))
    down_first = take([100, 92, 108], strategy=rule(take=5.0, stop=5.0))

    assert up_first.outcome == TARGET
    assert down_first.outcome == STOPPED
    assert up_first.return_pct > 0 > down_first.return_pct

    highs = [max(p) for p in ([108, 92], [92, 108])]
    lows = [min(p) for p in ([108, 92], [92, 108])]
    assert highs[0] == highs[1] and lows[0] == lows[1], (
        "identical extremes, opposite outcomes — the point of walking the path"
    )


def test_one_close_can_only_breach_one_barrier():
    """Worth pinning because it is easy to assume otherwise and write a
    tie-break for a case that cannot arise: a close sits on one side of the
    entry, so it breaches the target or the stop, never both. The stop being
    tested first therefore changes nothing today — it only matters if this is
    ever given intraday highs and lows."""
    assert take([100, 106], strategy=rule(take=1.0, stop=1.0)).outcome == TARGET
    assert take([100, 94], strategy=rule(take=1.0, stop=1.0)).outcome == STOPPED


# ------------------------------------------------------------- direction


def test_a_sell_is_right_when_the_price_falls():
    trade = take([100, 94], direction="sell", strategy=rule(take=5.0, stop=5.0))
    assert trade.outcome == TARGET
    assert trade.return_pct == pytest.approx(0.06), "signed to the call"


def test_a_sell_is_stopped_when_the_price_rises():
    trade = take([100, 107], direction="sell", strategy=rule(take=5.0, stop=5.0))
    assert trade.outcome == STOPPED


# -------------------------------------------------- refusing to measure


def test_no_entry_price_is_not_a_trade():
    assert take([100, 105], entry=None) is None


def test_a_signal_the_history_does_not_reach_back_to_is_refused():
    """The look-ahead guard `forward_outcomes` already carries: bars that begin
    after the signal would measure a different fortnight entirely."""
    late = series(100, 105, start="2026-06-01")
    assert walk("X", "1d", "buy", "2026-01-02", 100.0, late, rule()) is None


def test_a_signal_with_no_bars_after_it_is_refused():
    assert walk("X", "1d", "buy", "2026-01-02", 100.0,
                series(100, start="2026-01-02"), rule()) is None


# ------------------------------------------------------------ the maths


def test_the_breakeven_hit_rate_is_the_risk_share():
    assert rule(take=3.0, stop=5.0).breakeven_hit_rate == pytest.approx(0.625)
    assert rule(take=5.0, stop=5.0).breakeven_hit_rate == pytest.approx(0.5)


def test_the_reward_ratio():
    assert rule(take=3.0, stop=5.0).ratio == pytest.approx(0.6)


def test_a_summary_counts_outcomes_separately_from_wins():
    """A timeout closing green is a win but not a target, and a rule earning
    its return that way is not doing what it was designed to do."""
    trades = [
        take([100, 106], strategy=rule(take=5.0, stop=5.0)),      # target
        take([100, 93], strategy=rule(take=5.0, stop=5.0)),       # stopped
        take([100, 101, 102], strategy=rule(take=5.0, stop=5.0, bars=2)),  # timeout, green
    ]
    s = summarise(trades)
    assert s["n"] == 3
    assert (s["target"], s["stopped"], s["timeout"]) == (1, 1, 1)
    assert s["hit_rate"] == pytest.approx(2 / 3), "the green timeout counts as a win"
    assert s["target_rate"] == pytest.approx(1 / 3), "but not as a target"


def test_an_empty_summary_reports_nothing_rather_than_zero():
    assert summarise([])["n"] == 0
    assert "mean" not in summarise([]), "no trades is not a mean of zero"


def test_compare_orders_by_mean_not_hit_rate():
    """The trap the ordering exists to avoid: a rule can be right more often
    and still return less."""
    # a: right four times out of five, but for 1.5% a time against a 10% loss.
    a = rule(take=1.0, stop=9.0, key="a")
    often_right = [take([100, 101.5], strategy=a)] * 4 + [take([100, 90], strategy=a)]
    # b: right once in three, but the win is worth 30%.
    b = rule(take=25.0, stop=9.0, key="b")
    rarely_right = [take([100, 130], strategy=b)] + [take([100, 90], strategy=b)] * 2

    assert summarise(often_right)["hit_rate"] > summarise(rarely_right)["hit_rate"]
    assert summarise(often_right)["mean"] < summarise(rarely_right)["mean"]

    ranked = compare({"a": often_right, "b": rarely_right})
    assert ranked[0][0] == "b", "higher mean leads, regardless of hit rate"


# ------------------------------------------------------------- the config


def test_the_defaults_cross_every_exit_with_every_selection():
    config = _load_strategies(None)
    assert len(config.variants) == len(config.exits) * len(config.selections)
    assert len(config.variants) == 42, "7 exit rules x 6 selections"


def test_an_empty_block_turns_the_leaderboard_off():
    assert _load_strategies({}).variants == ()


def test_the_old_flat_list_shape_says_what_to_do():
    """This block used to be a list of variants. A bare TypeError three frames
    down would not tell anyone what to edit."""
    with pytest.raises(ValueError, match="two sections"):
        _load_strategies([dict(key="x", take_profit=5.0, stop_loss=5.0)])


def test_a_negative_stop_is_refused_rather_than_silently_inverted():
    """`stop_loss: -5` reads naturally and would stop out every winner."""
    with pytest.raises(ValueError, match="positive percentage"):
        _load_strategies({"exits": [
            dict(key="x", take_profit=5.0, stop_loss=-5.0, max_bars=20)
        ], "selections": []})


def test_a_null_barrier_is_allowed_because_a_holding_period_is_a_strategy():
    config = _load_strategies({"exits": [
        dict(key="hold", take_profit=None, stop_loss=None, max_bars=20)
    ], "selections": []})
    rule = config.exits[0]
    assert rule.take_profit is None and rule.stop_loss is None
    assert rule.breakeven_hit_rate is None, "no barriers, no breakeven to quote"


def test_a_duplicate_key_is_refused():
    with pytest.raises(ValueError, match="duplicate exit rule"):
        _load_strategies({"exits": [
            dict(key="x", take_profit=5.0, stop_loss=5.0, max_bars=20),
            dict(key="x", take_profit=3.0, stop_loss=5.0, max_bars=20),
        ], "selections": []})


def test_a_missing_field_names_what_is_missing():
    with pytest.raises(ValueError, match="max_bars"):
        _load_strategies({"exits": [dict(key="x", take_profit=5.0)],
                          "selections": []})


def test_an_unknown_entry_bar_is_refused():
    with pytest.raises(ValueError, match="entry is"):
        _load_strategies({"exits": [], "selections": [
            dict(key="s", entry="strongish", horizons=["1d"])
        ]})


def test_a_selection_with_no_timeframes_is_refused():
    with pytest.raises(ValueError, match="names no timeframes"):
        _load_strategies({"exits": [], "selections": [
            dict(key="s", entry="all", horizons=[])
        ]})
