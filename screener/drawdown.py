"""The crypto valuation gate: how far below its own highs an asset is trading.

A cryptocurrency has no earnings, no book value and no analyst fair value, so
the Morningstar gate that grades every equity here has nothing to work with.
This is the substitute, and it is deliberately built as *two* measures rather
than one.

**Why two.** A single "how far below the all-time high" test was rejected
earlier for a good reason: it is another way of saying the price has fallen,
which is what the RSI signal already said, so it would confirm nearly every
signal it saw while looking like an independent second opinion. Two measures on
different clocks do not have that problem, because they routinely disagree --
and it is the disagreement that carries the information. Measured across the
watchlist on the day this was written:

    ZEC   -74.3% from its all-time high, but only  -3.8% from its 6-month high
    UNI   -87.3% from its all-time high, and        0.0% from its 6-month high
    BCH   -93.5% from its all-time high, and       -49.0% from its 6-month high

Zcash and Uniswap collapsed years ago and have since recovered; Bitcoin Cash is
falling on both clocks. A gate requiring both is selective in a way neither leg
is alone: eight of eighteen assets passed it that day, against sixteen for the
all-time leg by itself.

**What each leg is for.** They answer different questions and are configured
differently on purpose.

  - *All-time*, a single global floor. This is a fact about the asset, not
    about the trade: something trading near its record high is not cheap by any
    reading, and no holding period changes that.
  - *Recent*, scaled by the horizon. This is a fact about the trade, so it uses
    the same `horizon.margin` the equity gate uses -- 10% on the hourly chart
    up to 50% on the weekly one. A position held for a week should demand more
    of a discount than one held for an hour, and that is already how every
    equity on this watchlist is graded.

**What this is not.** It is not a valuation. Nobody is claiming an asset is
worth its old high. It is a statement about where the price sits in its own
range, and the dashboard says exactly that rather than calling it a fair value.
Whether it predicts anything is an open question, which is what the Historical
Dashboard exists to answer -- the journal records the gate on every crypto
signal from the day it ships, so "did the gated ones do better?" becomes
answerable rather than arguable.

Highs are computed from closes, like everything else here. An intraday spike
that closed lower is not a level anyone could have sold at.
"""

from __future__ import annotations

from dataclasses import dataclass

# How far below its all-time high an asset must trade before the first leg
# passes, as a fraction. 0.5 admitted 16 of the 18 assets on the watchlist the
# day it was chosen, which is the point: this leg is a floor that excludes
# assets near their records, not a filter meant to be selective on its own.
# The recent leg does the discriminating.
DEFAULT_ATH_FLOOR = 0.5

# Trading days in the recent window. 180 calendar days of a market that never
# closes -- crypto has no weekends, so a bar is a day.
RECENT_WINDOW_BARS = 180


@dataclass(frozen=True)
class Highs:
    """What an asset has been worth, on two clocks."""

    symbol: str
    all_time: float | None
    recent: float | None          # highest close in the recent window
    recent_bars: int = 0          # how many bars that high was taken over
    ath_date: str = ""
    updated_at: str = ""

    @property
    def known(self) -> bool:
        return bool(self.all_time) and bool(self.recent)


def drawdown(price: float | None, high: float | None) -> float | None:
    """How far below `high` the price sits, as a positive fraction.

    0.30 means 30% below. None when either number is missing or nonsensical --
    a zero or negative high is not a level, and dividing by it would produce a
    confident answer about nothing.
    """
    if not price or not high or high <= 0 or price <= 0:
        return None
    # Rounded, because the thresholds are read off a config file in whole
    # percents and binary floating point does not put 20% where you left it:
    # 1 - 80/100 is 0.19999999999999996, so an asset sitting exactly on a 20%
    # threshold would be refused by a rule whose own text says it needs 20%.
    # Nine places is far finer than any threshold anyone would write and far
    # coarser than the artifact.
    return round(max(0.0, 1.0 - price / high), 9)


def recent_high(closes, bars: int = RECENT_WINDOW_BARS) -> tuple[float | None, int]:
    """The highest close in the last `bars` bars, and how many there were.

    The count is returned rather than discarded because it is the difference
    between "20% below its six-month high" and "20% below the high of the three
    weeks we have on file". A young listing has the second, and the caller has
    to be able to tell.
    """
    window = [c for c in list(closes)[-bars:] if c]
    if not window:
        return None, 0
    return max(window), len(window)


def gate(
    price: float | None,
    highs: Highs | None,
    margin: float,
    ath_floor: float = DEFAULT_ATH_FLOOR,
    min_bars: int = 0,
) -> tuple[bool, bool]:
    """Apply the two-leg gate, in the shape the rest of the code expects.

    Returns `(known, confirms)`, matching `signals.valuation_passes` exactly, so
    it drops into `is_strong` in the same slot the Morningstar gate occupies --
    required, never a veto. `known` is False when there is nothing to compare,
    which is what keeps an asset with no recorded highs out of the strong
    cohort rather than sliding it in on a default.

    `min_bars` refuses a recent high taken over too short a window. Without it
    a listing three weeks old would be graded against three weeks of history
    while the card said "6-month high", which is the kind of quiet
    misdescription this codebase has been bitten by before.
    """
    if price is None or highs is None or not highs.known:
        return False, False
    if min_bars and highs.recent_bars < min_bars:
        return False, False

    from_ath = drawdown(price, highs.all_time)
    from_recent = drawdown(price, highs.recent)
    if from_ath is None or from_recent is None:
        return False, False

    return True, from_ath >= ath_floor and from_recent >= margin


def explain(price: float | None, highs: Highs | None, margin: float,
            ath_floor: float = DEFAULT_ATH_FLOOR) -> str:
    """One line for the card, naming both legs and which one failed.

    Saying only "does not qualify" would leave the reader unable to tell an
    asset that has barely fallen from one that has fallen a long way but not
    recently, and those are opposite situations.
    """
    if highs is None or not highs.known or not price:
        return "No highs recorded yet"

    from_ath = drawdown(price, highs.all_time) or 0.0
    from_recent = drawdown(price, highs.recent) or 0.0
    return (
        f"{from_ath:.0%} below its all-time high, "
        f"{from_recent:.0%} below its 6-month high "
        f"(needs {ath_floor:.0%} and {margin:.0%})"
    )
