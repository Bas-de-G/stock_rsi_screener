"""Detection of the double-oversold-recovery pattern.

The pattern, as specified:

    RSI is below 30, rises back through 30 (cross #1), falls below 30 again,
    then rises back through 30 a second time (cross #2). If cross #2 happens
    within 14 days of cross #1, that's a buy signal — subject to the
    valuation gate.

Two notes on how that translates into code:

* An "upward cross" on day i means rsi[i-1] < threshold <= rsi[i]. Because a
  cross requires the previous day to be *below* the threshold, two crosses
  can't happen without a dip below in between — the "goes below 30 again"
  leg is implied. We still locate and report that dip so the stored signal
  shows the full up/down/up shape.
* Only *consecutive* cross pairs are considered. If RSI crosses up on day 1,
  10 and 25, the candidate pairs are (1, 10) and (10, 25) — never (1, 25),
  which would describe a different shape than the one specified.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import SignalConfig
from .storage import RsiPoint


BUY, SELL = "buy", "sell"


@dataclass(frozen=True)
class CrossPair:
    """A completed cross/retrace/cross pattern, before the valuation gate.

    Field names read from the buy side (`up1`, `down`, `up2`) because that's
    the pattern this started as; on the sell side they mean the mirror --
    first downward cross, the rebound between, second downward cross.
    """

    up1_date: str
    down_date: str
    up2_date: str
    span_days: float   # calendar days (fractional on intraday bars)
    rsi_at_up2: float
    direction: str = BUY


def find_upward_crosses(series: list[RsiPoint], threshold: float) -> list[int]:
    """Indices where RSI crossed from below the threshold to at/above it."""
    crosses = []
    for i in range(1, len(series)):
        if series[i - 1].rsi < threshold <= series[i].rsi:
            crosses.append(i)
    return crosses


def find_downward_crosses(series: list[RsiPoint], threshold: float) -> list[int]:
    """Indices where RSI crossed from above the threshold to at/below it.

    The exact mirror of `find_upward_crosses`, including the boundary: landing
    exactly on 70 from above counts, just as landing exactly on 30 from below
    does.
    """
    crosses = []
    for i in range(1, len(series)):
        if series[i - 1].rsi > threshold >= series[i].rsi:
            crosses.append(i)
    return crosses


def find_cross_pairs(
    series: list[RsiPoint], threshold: float, config: SignalConfig,
    direction: str = BUY,
) -> list[CrossPair]:
    """Find every consecutive pair of crosses that fits the window.

    `direction` picks which way the crosses go: BUY looks for two upward
    crosses of the oversold line with a dip between, SELL for two downward
    crosses of the overbought line with a rebound between.
    """
    finder = find_upward_crosses if direction == BUY else find_downward_crosses
    crosses = finder(series, threshold)
    pairs: list[CrossPair] = []

    for first, second in zip(crosses, crosses[1:]):
        dip = _find_dip(series, first, second, threshold, direction)
        if dip is None:
            # Can't happen given the cross definition, but if the series ever
            # gains a gap we'd rather skip than record a malformed pattern.
            continue

        span = _span(series, first, second, config)
        if span > config.window_days:
            continue

        pairs.append(
            CrossPair(
                up1_date=series[first].date,
                down_date=series[dip].date,
                up2_date=series[second].date,
                span_days=span,
                rsi_at_up2=series[second].rsi,
                direction=direction,
            )
        )
    return pairs


def _find_dip(
    series: list[RsiPoint], first: int, second: int, threshold: float,
    direction: str = BUY,
) -> int | None:
    """Index of the last bar between the crosses on the far side of the line.

    For a buy that's the dip back under 30; for a sell, the rebound back over
    70. Either way it's what makes the shape a genuine double cross rather
    than one cross recorded twice.
    """
    for i in range(second - 1, first - 1, -1):
        beyond = series[i].rsi < threshold if direction == BUY else series[i].rsi > threshold
        if beyond:
            return i
    return None


def _moment(label: str) -> dt.datetime:
    """Parse a bar's label, which is a date on daily/weekly bars and a full
    timestamp on intraday ones.

    `date.fromisoformat` rejects '2026-08-04T18:49' outright on every Python
    version this supports, so parsing as a datetime is what makes the intraday
    horizons work at all. A bare 'yyyy-mm-dd' parses as midnight, which is
    exactly the old behaviour.
    """
    return dt.datetime.fromisoformat(label)


def _span(series: list[RsiPoint], first: int, second: int, config: SignalConfig) -> float:
    """Distance between the two crosses, in whichever unit is configured.

    Returned as a float so intraday windows keep sub-day resolution: with a
    2-day window on hourly bars, truncating 1.9 days to 1 would quietly widen
    the window by most of a day.
    """
    if config.window_unit == "trading":
        return float(second - first)
    delta = _moment(series[second].date) - _moment(series[first].date)
    return delta.total_seconds() / 86400.0


def valuation_passes(
    price: float | None,
    fair_value: float | None,
    config: SignalConfig,
    margin: float = 0.0,
) -> tuple[bool, bool]:
    """Apply the configured valuation gate.

    Returns (known, confirms). `known` is False when there are no Morningstar
    figures to compare; `confirms` is only meaningful when `known` is True.

    `margin` is the headroom the gate demands, as a fraction: 0.30 means fair
    value must sit at least 30% above the price before the valuation confirms.
    It comes from the horizon (see `config.Horizon`), because a trade held for
    an hour and one held for a week don't deserve the same bar. At the default
    0.0 this is exactly the old "is it below fair value" test, which is what
    keeps the daily behaviour unchanged for anyone who zeroes the margins out.

    Note this answers "does the valuation agree?", not "is this a signal?" —
    see `signal_fires`. Keeping them apart is what lets an RSI pattern stand
    on its own while a matching fair value upgrades it to a strong buy.
    """
    if price is None or fair_value is None:
        return False, False

    # Strict, so an exactly-equal pair still fails — at margin 0 this is
    # character-for-character the original test.
    if config.valuation_rule == "fair_value_below_price":
        # Inverted rule: fair value must sit that far *below* the price.
        return True, fair_value * (1 + margin) < price
    return True, price * (1 + margin) < fair_value


def earnings_growth_passes(growth: float | None) -> tuple[bool, bool]:
    """Apply the earnings-growth grading factor.

    Returns (known, confirms), the same shape as `valuation_passes`, so both
    factors grade a signal identically. `known` is False when TradingView has
    neither a TTM nor an FY figure yet (e.g. a stock too recently listed to
    have a trailing year). `confirms` is True for any positive YoY EPS growth
    — no threshold to tune, mirroring the valuation gate's plain boolean.

    Like the valuation gate, this only ever grades strength (see `is_strong`)
    — it never affects whether a pattern fires at all.
    """
    if growth is None:
        return False, False
    return True, growth > 0


def signal_fires(confirms: bool, config: SignalConfig) -> bool:
    """Whether a completed pattern counts as a buy signal.

    With fire_without_valuation set, the RSI pattern is enough on its own and
    the valuation only decides how strong it is. Without it, the screener runs
    strict and nothing fires until a fair value confirms it.

    Deliberately takes only the valuation's `confirms` — earnings growth never
    gates firing, only grading (see `is_strong`). A soft quarter shouldn't be
    able to silently suppress an otherwise-good signal.
    """
    return confirms or config.fire_without_valuation


def is_strong(valuation: tuple[bool, bool], *vetoes: tuple[bool, bool]) -> bool:
    """A strong buy: the valuation confirms, and nothing else known contradicts it.

    The factors are deliberately *not* peers. Fair value is the thesis — the
    reason to think the stock is worth more than it costs — so it is required:
    a signal nobody has checked a fair value for is never strong, however well
    the company is doing.

    Everything after it is a veto. A veto that's unknown costs nothing; a veto
    that's known and disagrees withholds the rocket. That's what makes earnings
    growth a filter on the valuation rather than a substitute for it — the
    value-trap case (cheap by fair value, but earnings shrinking) is exactly
    what it exists to catch.
    """
    known, confirms = valuation
    if not (known and confirms):
        return False
    return all(agrees for is_known, agrees in vetoes if is_known)


def signal_is_live(
    signal, series: list[RsiPoint], config: SignalConfig, threshold: float
) -> bool:
    """Whether a recorded pattern is still a tradeable setup *right now*.

    Two conditions, both from the strategy as specified:

    * Both crosses fall inside the lookback window measured back from the most
      recent bar — not merely inside the window measured between themselves.
      A pattern that completed in March is a matter of record, not a signal
      you can act on in August.
    * Current RSI is still on the signalling side of the threshold. A buy
      setup whose RSI has fallen back under 30 has not resolved; it's a stock
      still falling.

    Kept separate from detection on purpose: `signals` remains a complete
    historical log of every pattern ever found, and liveness is applied when
    deciding what to *show* and what to scrape.
    """
    if not series:
        return False
    latest = series[-1]
    horizon_start = _moment(latest.date) - dt.timedelta(days=config.window_days)
    if _moment(signal.up1_date) < horizon_start:
        return False
    if signal.direction == "sell":
        return latest.rsi < threshold
    return latest.rsi > threshold
