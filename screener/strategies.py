"""Exit rules, and how the same signals do under each of them.

`outcomes.py` asks "where was the price N days later". This asks a different
question: "if I had taken profit at +5% and cut at -5%, what would have
happened". Same signals, same price history, different exit rule -- which is
what makes two strategies comparable at all.

Three decisions shape every number here, and each one exists because the
obvious shortcut is wrong.

**The path is walked, never inferred from the extremes.** `outcomes` already
stores `max_gain` and `max_drawdown`, so a +5/-5 strategy looks like a query
away: won if max_gain >= 5, lost if max_drawdown <= -5. It is not. Those two
columns do not say which happened *first*, and a volatile name routinely hits
both -- so that query answers "did it ever touch +5%", which is a different and
much easier question than "did it touch +5% before it touched -5%". Every
strategy would score better than it deserves, tight stops most of all.

**The exit is the close that breached the barrier, not the barrier.** We hold
daily closes, so a stop touched at 11am and recovered by the bell is invisible,
and a gap-down opens far below the level. Recording -5.0% when the close was
-6.3% would flatter every strategy at exactly the point where the flattery
matters. What is recorded is the fill we can actually prove.

**A timeout is its own outcome, not a loss.** A position that reaches neither
barrier within `max_bars` is closed at the last close and labelled `TIMEOUT`.
Folding those into the losers hides the difference between "this rule is wrong"
and "this rule never fired", which is usually the interesting distinction
between two variants.

The intraday blindness is a real limit, not a rounding error: the tighter the
barriers, the more of the true path happens between the closes we can see.
`history.html` says so on the page rather than leaving it in this docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .outcomes import _signed, forward_series

TARGET = "target"      # take-profit hit first
STOPPED = "stopped"    # stop-loss hit first
TIMEOUT = "timeout"    # neither, closed at the end of the window


@dataclass(frozen=True)
class Strategy:
    """One exit rule. Percentages are positive numbers in both directions."""

    key: str
    label: str
    take_profit: float          # e.g. 5.0 for +5%
    stop_loss: float            # e.g. 5.0 for -5%
    max_bars: int               # trading days before the position is closed
    note: str = ""

    @property
    def ratio(self) -> float:
        """Reward for risk. Below 1 needs a hit rate above half to break even."""
        return self.take_profit / self.stop_loss if self.stop_loss else float("inf")

    @property
    def breakeven_hit_rate(self) -> float:
        """The hit rate this rule breaks even at, if exits landed on the barrier.

        The number that makes two variants comparable before any data is
        involved: +3/-5 has to be right 62.5% of the time, while +5/-5 only has
        to be right half the time. A rule leading on hit rate can be the worse
        one, which is the trap this exists to expose.

        It is an idealisation, not a pass mark. Real exits overshoot -- a gap
        opens through the stop, a timeout closes wherever the window ended --
        so a cohort can sit below its breakeven and still return positively.
        Measured over the 7,291 recorded signals, +3/-5 buys hit 60.1% against
        a 62.5% breakeven and still averaged +0.51%.
        """
        total = self.take_profit + self.stop_loss
        return self.stop_loss / total if total else 0.0


@dataclass(frozen=True)
class Trade:
    """One signal taken to its exit under one strategy."""

    symbol: str
    horizon: str
    direction: str
    up2_date: str
    strategy: str
    entry: float
    exit: float
    return_pct: float       # signed to the call, as a fraction
    bars_held: int
    outcome: str            # TARGET | STOPPED | TIMEOUT

    @property
    def won(self) -> bool:
        return self.return_pct > 0


def walk(
    symbol: str,
    horizon: str,
    direction: str,
    up2_date: str,
    entry: float | None,
    daily_closes,
    strategy: Strategy,
) -> Trade | None:
    """Take one signal to its exit, bar by bar, in date order.

    Returns None when the signal cannot be measured -- no entry price, or a
    price series that does not reach back to the signal day. None and a
    zero-return trade must never look alike.

    The stop is tested before the target, which with close-only data never
    actually decides anything: one close sits on one side of the entry, so it
    cannot breach both barriers. It matters only if this is ever given intraday
    highs and lows, where a single bar genuinely can touch both and the honest
    reading is the pessimistic one. Ordering it that way now means that change
    is a data change rather than a silent shift in what the numbers mean.
    """
    if not entry:
        return None
    ahead = forward_series(up2_date, daily_closes)
    if not ahead:
        return None

    window = ahead[:strategy.max_bars]
    if not window:
        return None

    take, stop = strategy.take_profit / 100.0, strategy.stop_loss / 100.0
    for held, (_, close) in enumerate(window, start=1):
        ret = _signed(entry, close, direction)
        if ret <= -stop:
            return Trade(symbol, horizon, direction, up2_date, strategy.key,
                         entry, close, ret, held, STOPPED)
        if ret >= take:
            return Trade(symbol, horizon, direction, up2_date, strategy.key,
                         entry, close, ret, held, TARGET)

    close = window[-1][1]
    return Trade(symbol, horizon, direction, up2_date, strategy.key,
                 entry, close, _signed(entry, close, direction),
                 len(window), TIMEOUT)


def summarise(trades) -> dict:
    """Aggregate one strategy's trades into the row the comparison shows.

    `hit_rate` counts trades that ended up, which is not the same as trades
    that reached the target -- a timeout can close green. Both are reported,
    because a rule whose profits come from timeouts rather than targets is not
    doing what its author intended.
    """
    trades = list(trades)
    n = len(trades)
    if not n:
        return {"n": 0}

    returns = [t.return_pct for t in trades]
    counts = {o: sum(1 for t in trades if t.outcome == o)
              for o in (TARGET, STOPPED, TIMEOUT)}
    return {
        "n": n,
        "hit_rate": sum(1 for t in trades if t.won) / n,
        "mean": sum(returns) / n,
        "median": median(returns),
        "total": sum(returns),
        "target": counts[TARGET],
        "stopped": counts[STOPPED],
        "timeout": counts[TIMEOUT],
        "target_rate": counts[TARGET] / n,
        "mean_bars": sum(t.bars_held for t in trades) / n,
    }


def compare(trades_by_strategy: dict) -> list[tuple[str, dict]]:
    """Every strategy's summary, best mean return first.

    Ordering by mean rather than by hit rate on purpose: a rule can be right
    most of the time and still lose money, which is the whole reason a +3/-5
    variant needs comparing rather than assuming.
    """
    rows = [(key, summarise(trades)) for key, trades in trades_by_strategy.items()]
    return sorted(rows, key=lambda kv: kv[1].get("mean", 0.0), reverse=True)
