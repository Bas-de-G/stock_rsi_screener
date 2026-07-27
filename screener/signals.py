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


@dataclass(frozen=True)
class CrossPair:
    """A completed up/down/up pattern, before the valuation gate is applied."""

    up1_date: str
    down_date: str
    up2_date: str
    span_days: int
    rsi_at_up2: float


def find_upward_crosses(series: list[RsiPoint], threshold: float) -> list[int]:
    """Indices where RSI crossed from below the threshold to at/above it."""
    crosses = []
    for i in range(1, len(series)):
        if series[i - 1].rsi < threshold <= series[i].rsi:
            crosses.append(i)
    return crosses


def find_cross_pairs(
    series: list[RsiPoint], threshold: float, config: SignalConfig
) -> list[CrossPair]:
    """Find every consecutive pair of upward crosses that fits the window."""
    crosses = find_upward_crosses(series, threshold)
    pairs: list[CrossPair] = []

    for first, second in zip(crosses, crosses[1:]):
        dip = _find_dip(series, first, second, threshold)
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
            )
        )
    return pairs


def _find_dip(series: list[RsiPoint], first: int, second: int, threshold: float) -> int | None:
    """Index of the last day between the two crosses where RSI sat below the threshold."""
    for i in range(second - 1, first - 1, -1):
        if series[i].rsi < threshold:
            return i
    return None


def _span(series: list[RsiPoint], first: int, second: int, config: SignalConfig) -> int:
    """Distance between the two crosses, in whichever unit is configured."""
    if config.window_unit == "trading":
        return second - first
    d1 = dt.date.fromisoformat(series[first].date)
    d2 = dt.date.fromisoformat(series[second].date)
    return (d2 - d1).days


def valuation_passes(
    price: float | None, fair_value: float | None, config: SignalConfig
) -> tuple[bool, bool]:
    """Apply the configured valuation gate.

    Returns (known, confirms). `known` is False when there are no Morningstar
    figures to compare; `confirms` is only meaningful when `known` is True.

    Note this answers "does the valuation agree?", not "is this a signal?" —
    see `signal_fires`. Keeping them apart is what lets an RSI pattern stand
    on its own while a matching fair value upgrades it to a strong buy.
    """
    if price is None or fair_value is None:
        return False, False

    if config.valuation_rule == "fair_value_below_price":
        return True, fair_value < price
    return True, price < fair_value


def signal_fires(confirms: bool, config: SignalConfig) -> bool:
    """Whether a completed pattern counts as a buy signal.

    With fire_without_valuation set, the RSI pattern is enough on its own and
    the valuation only decides how strong it is. Without it, the screener runs
    strict and nothing fires until a fair value confirms it.
    """
    return confirms or config.fire_without_valuation


def is_strong(known: bool, confirms: bool) -> bool:
    """A strong buy: the pattern fired AND a real fair value backs it up."""
    return known and confirms
