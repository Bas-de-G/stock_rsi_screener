"""Exit rules, entry filters, and the leaderboard that compares them.

`outcomes.py` asks "where was the price N days later". This asks a different
question: "if I had run *this* set of rules, what would have happened" -- and
then asks it of every combination, so the answer is a ranking rather than an
opinion.

A strategy here is three independent choices:

  1. **Which signals to take** -- every fired buy, or only the strong ones
     (pattern fired, valuation confirms, nothing vetoes).
  2. **Which timeframes to watch** -- the daily chart alone, or the faster
     ones alongside it. More timeframes means more trades, not better ones,
     and which way that cuts is exactly what the leaderboard is for.
  3. **When to get out** -- a take-profit, a stop-loss, a holding period, or
     some combination.

They are stored and computed separately on purpose. Walking a price path is
the expensive part, and it depends only on the exit rule -- so the walk happens
once per exit rule and the entry and timeframe choices are applied afterwards
as filters over the same trades. Sixteen strategies therefore cost seven walks,
not sixteen, and every strategy sharing an exit rule is guaranteed to be
looking at literally the same trades.

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

# Entry filters.
STRONG_ONLY = "strong"
ALL_BUYS = "all"


@dataclass(frozen=True)
class ExitRule:
    """When to get out. Percentages are positive numbers in both directions.

    `take_profit` or `stop_loss` of None means "no such barrier" -- that is how
    a pure holding-period rule is expressed, and it is a real strategy rather
    than a degenerate one: exit after twenty days whatever has happened.
    """

    key: str
    label: str
    take_profit: float | None
    stop_loss: float | None
    max_bars: int
    note: str = ""

    @property
    def ratio(self) -> float | None:
        """Reward for risk. Below 1 needs a hit rate above half to break even.
        None for a rule with no barriers, where there is no ratio to take."""
        if self.take_profit is None or self.stop_loss is None:
            return None
        return self.take_profit / self.stop_loss if self.stop_loss else float("inf")

    @property
    def breakeven_hit_rate(self) -> float | None:
        """The hit rate this rule breaks even at, if exits landed on the barrier.

        The number that makes two rules comparable before any data is involved:
        +3/-5 has to be right 62.5% of the time, while +5/-5 only has to be
        right half the time. A rule leading on hit rate can be the worse one,
        which is the trap this exists to expose.

        It is an idealisation, not a pass mark. Real exits overshoot -- a gap
        opens through the stop, a timeout closes wherever the window ended --
        so a cohort can sit below its breakeven and still return positively.
        None for a rule with no barriers, where the question does not apply.
        """
        if self.take_profit is None or self.stop_loss is None:
            return None
        total = self.take_profit + self.stop_loss
        return self.stop_loss / total if total else 0.0


@dataclass(frozen=True)
class Selection:
    """Which signals a strategy acts on: the entry bar, and the timeframes."""

    key: str
    label: str
    entry: str                      # STRONG_ONLY | ALL_BUYS
    horizons: tuple[str, ...]

    def takes(self, trade) -> bool:
        if trade.horizon not in self.horizons:
            return False
        if self.entry == STRONG_ONLY and not trade.strong:
            return False
        return True


@dataclass(frozen=True)
class Strategy:
    """One exit rule applied to one selection of signals."""

    exit_rule: ExitRule
    selection: Selection

    @property
    def key(self) -> str:
        return f"{self.exit_rule.key}:{self.selection.key}"

    @property
    def name(self) -> str:
        """Composite rather than invented. "Runner · 4h+1d · Strong only" says
        what the strategy does; "Strategy 14" says nothing, and with dozens of
        permutations a memorable name per row would be noise rather than help."""
        return f"{self.exit_rule.label} · {self.selection.label}"


@dataclass(frozen=True)
class Trade:
    """One signal taken to its exit under one exit rule."""

    symbol: str
    horizon: str
    direction: str
    up2_date: str
    strategy: str           # the EXIT RULE's key; selections filter afterwards
    entry: float
    exit: float
    return_pct: float       # signed to the call, as a fraction
    bars_held: int
    outcome: str            # TARGET | STOPPED | TIMEOUT
    # Whether the signal was strong when it fired, recomputed against the price
    # on its own bar rather than read from the stored flag -- see
    # `historical._retrospective_strong`.
    strong: bool = False

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
    rule: ExitRule,
    strong: bool = False,
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

    window = ahead[:rule.max_bars]
    if not window:
        return None

    take = rule.take_profit / 100.0 if rule.take_profit is not None else None
    stop = rule.stop_loss / 100.0 if rule.stop_loss is not None else None
    for held, (_, close) in enumerate(window, start=1):
        ret = _signed(entry, close, direction)
        if stop is not None and ret <= -stop:
            return Trade(symbol, horizon, direction, up2_date, rule.key,
                         entry, close, ret, held, STOPPED, strong)
        if take is not None and ret >= take:
            return Trade(symbol, horizon, direction, up2_date, rule.key,
                         entry, close, ret, held, TARGET, strong)

    close = window[-1][1]
    return Trade(symbol, horizon, direction, up2_date, rule.key,
                 entry, close, _signed(entry, close, direction),
                 len(window), TIMEOUT, strong)


def summarise(trades) -> dict:
    """Aggregate trades into the row a leaderboard shows.

    `hit_rate` counts trades that ended up, which is not the same as trades
    that reached the target -- a timeout can close green. Both are reported,
    because a rule whose profits come from timeouts rather than targets is not
    doing what its author intended.

    `total` is the sum of returns, which is the closest thing here to "what
    would this have made". It is deliberately not compounded: these trades
    overlap in time and a compounded figure would imply a position sizing and
    a capital constraint that nothing here models.
    """
    trades = list(trades)
    n = len(trades)
    if not n:
        return {"n": 0}

    returns = [t.return_pct for t in trades]
    counts = {o: sum(1 for t in trades if t.outcome == o)
              for o in (TARGET, STOPPED, TIMEOUT)}
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    return {
        "n": n,
        "hit_rate": sum(1 for r in returns if r > 0) / n,
        "mean": sum(returns) / n,
        "median": median(returns),
        "total": sum(returns),
        "best": max(returns),
        "worst": min(returns),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "target": counts[TARGET],
        "stopped": counts[STOPPED],
        "timeout": counts[TIMEOUT],
        "target_rate": counts[TARGET] / n,
        "mean_bars": sum(t.bars_held for t in trades) / n,
    }


def leaderboard(trades, strategies) -> list[tuple[Strategy, dict]]:
    """Every strategy's summary, best mean return first.

    Ordered by mean rather than by hit rate on purpose: a rule can be right
    most of the time and still lose money, which is the whole reason a +3/-5
    variant needs comparing rather than assuming.

    Strategies with no trades are kept and sort last. An empty row is a result
    -- "this combination never triggered" -- and dropping it would leave the
    reader thinking it had not been tried.
    """
    by_rule: dict[str, list] = {}
    for trade in trades:
        by_rule.setdefault(trade.strategy, []).append(trade)

    rows = []
    for strategy in strategies:
        pool = by_rule.get(strategy.exit_rule.key, ())
        rows.append((strategy, summarise(t for t in pool if strategy.selection.takes(t))))
    return sorted(
        rows,
        key=lambda kv: (kv[1]["n"] > 0, kv[1].get("mean", 0.0)),
        reverse=True,
    )


def compare(trades_by_strategy: dict) -> list[tuple[str, dict]]:
    """Summaries keyed by exit rule, best mean first. Used by `backtest`."""
    rows = [(key, summarise(trades)) for key, trades in trades_by_strategy.items()]
    return sorted(rows, key=lambda kv: kv[1].get("mean", 0.0), reverse=True)
