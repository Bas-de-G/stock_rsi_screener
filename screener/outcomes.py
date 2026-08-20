"""What actually happened after each signal.

Nothing here is captured; it is all computed from price history already on
disk, which means it can be re-run over the whole record at any time and gives
the same answer. That is deliberate: an outcome derived from prices is a fact,
and re-deriving it costs nothing, whereas a stored one would need to be right
first time and forever.

Two decisions shape every number below.

**Forward windows are counted in daily bars, whatever timeframe the signal
fired on.** The natural alternative -- bars of the signal's own horizon -- makes
"+20 bars" mean twenty hours for a 1h signal and twenty weeks for a 1w one, so
the four horizons cannot be compared with each other. Comparing them is the
entire question ("is the 4h entry better than the daily one?"), so the daily
series is used as the common ruler for all of them. It is also the deepest
history every ticker has.

**Returns are signed to the direction of the call.** A sell that is followed by
a fall is a *correct* call, so it scores positive, exactly as a buy followed by
a rise does. Without that, a hit rate over a mixed sample measures nothing --
sells outnumber buys most weeks, and their gains would read as losses.
"""

from __future__ import annotations

from dataclasses import dataclass

# Trading days after the signal at which the position is measured. Roughly a
# day, a week, a month, a quarter and a year -- the spans a human actually
# reasons in, and the ones the strategy discussion asked for.
FORWARD_BARS = (1, 5, 20, 60, 250)


@dataclass(frozen=True)
class Outcome:
    """How one pattern did, measured `bars` trading days later."""

    symbol: str
    horizon: str
    direction: str
    up2_date: str
    bars: int
    entry: float
    exit: float
    return_pct: float      # signed to the call: positive means it was right
    max_gain: float        # best the call ever looked, within the window
    max_drawdown: float    # worst it ever looked, within the window


def _signed(entry: float, price: float, direction: str) -> float:
    """Return on the position the signal implies, as a fraction of entry."""
    move = (price - entry) / entry
    return -move if direction == "sell" else move


def forward_outcomes(
    symbol: str,
    horizon: str,
    direction: str,
    up2_date: str,
    entry: float | None,
    daily_closes,
    bars=FORWARD_BARS,
) -> list[Outcome]:
    """Measure one pattern against the daily bars that followed it.

    `daily_closes` is (date, close) for the symbol, ascending. Bars on or
    before the signal's own day are dropped: a signal that completes intraday
    is entered at that moment, and that day's close is the first thing that can
    be measured against it.

    Returns only the windows the history actually reaches. A pattern from last
    week has a +5 outcome and no +250 one, and saying nothing is the honest
    answer -- a missing row and a zero return must never look alike.
    """
    if not entry:
        return []
    closes = list(daily_closes)
    if not closes:
        return []
    day = up2_date[:10]

    # The daily series has to *reach back to* the signal, not merely contain
    # bars after it. Each horizon is backfilled to its own depth -- five years
    # of weekly bars against two of daily ones -- so a 2023 weekly pattern sits
    # years before the first daily bar, and "the next twenty bars" would
    # silently be twenty bars from 2025. Measured that way PLTR's August 2023
    # sell scored -1052%: entered at 15.41 and exited at 177.57, two years and
    # twenty days later. Every number computed from an uncovered signal is
    # reading the future, so refuse to compute one.
    if closes[0][0][:10] > day:
        return []

    ahead = [(d, c) for d, c in closes if d[:10] > day]
    if not ahead:
        return []

    out: list[Outcome] = []
    for n in bars:
        if len(ahead) < n:
            break
        if not _span_is_plausible(day, ahead[n - 1][0][:10], n):
            # A gap inside the series -- a delisting, a suspension, a stretch
            # CI missed -- would otherwise stretch "twenty trading days" across
            # months without saying so.
            break
        window = [c for _, c in ahead[:n]]
        out.append(Outcome(
            symbol=symbol,
            horizon=horizon,
            direction=direction,
            up2_date=up2_date,
            bars=n,
            entry=entry,
            exit=window[-1],
            return_pct=_signed(entry, window[-1], direction),
            max_gain=max(_signed(entry, c, direction) for c in window),
            max_drawdown=min(_signed(entry, c, direction) for c in window),
        ))
    return out


def trajectory(entry: float | None, daily_closes, up2_date: str, days: int) -> list[float]:
    """The price path after a signal, rebased so the signal day is 100.

    What an event-study chart draws: every recommendation starting from the
    same point, so paths of a $5 stock and a $500 one can be read on one axis.
    Index 0 is the signal itself and always 100.

    Shorter than `days + 1` when the history runs out, which is the honest
    shape -- a line that stops is a signal too recent to have finished, not one
    that went flat.
    """
    if not entry:
        return []
    closes = list(daily_closes)
    if not closes or closes[0][0][:10] > up2_date[:10]:
        # Same coverage rule as `forward_outcomes`, and for the same reason: a
        # path drawn from bars that begin after the signal is a picture of a
        # different fortnight.
        return []
    ahead = [c for d, c in closes if d[:10] > up2_date[:10]][:days]
    return [100.0] + [100.0 * c / entry for c in ahead]


def mean_path(paths, min_paths: int = 3) -> list[float]:
    """The average of many trajectories, day by day.

    Truncated where fewer than `min_paths` signals still have data, so the tail
    of the line is not one lucky ticker drawn with the authority of a cohort
    average. That is the failure mode of every event-study chart: the mean
    keeps going long after the sample has thinned to nothing.
    """
    if not paths:
        return []
    out = []
    for day in range(max(len(p) for p in paths)):
        values = [p[day] for p in paths if day < len(p)]
        if len(values) < min_paths:
            break
        out.append(sum(values) / len(values))
    return out


def _span_is_plausible(start: str, end: str, bars: int) -> bool:
    """Whether `bars` trading days could really span these two dates.

    Trading days run about 1.45 to the calendar day. Three times that plus a
    week is loose enough for Christmas and Easter and tight enough to catch a
    series that simply stops for a month.
    """
    import datetime as dt

    try:
        gap = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    except ValueError:
        return True  # unparseable dates are someone else's problem
    return gap <= bars * 3 + 7


def baseline_outcomes(symbol: str, daily_closes, direction: str = "buy",
                      step: int = 5, bars=FORWARD_BARS) -> list[Outcome]:
    """The same measurement, from entries chosen for no reason at all.

    Without this the headline numbers cannot be read. Equities drift upward, so
    *any* long strategy posts a hit rate above half and a positive mean return
    over a rising sample -- including buying on days picked by a coin. The
    question a screener has to answer is not "did signals make money" but "did
    they do better than not screening", and that needs the coin's score next to
    them.

    Entries every `step` trading days rather than every day, which is enough
    for a stable average and keeps the comparison cheap.
    """
    closes = list(daily_closes)
    out: list[Outcome] = []
    for i in range(0, len(closes), step):
        date, entry = closes[i]
        # The whole series, not the slice after the entry: `forward_outcomes`
        # does its own filtering, and it checks that the history reaches back
        # to the entry date before measuring anything. Handing it a slice that
        # starts *after* the entry fails that check on every bar and silently
        # produces no baseline at all.
        out.extend(forward_outcomes(
            symbol, "baseline", direction, date, entry, closes, bars
        ))
    return out


def summarise(outcomes) -> dict:
    """Hit rate and the return distribution for one cohort.

    Median as well as mean, because a handful of large winners drags a mean
    somewhere the typical trade never went -- and the typical trade is what
    someone acting on this actually gets.
    """
    returns = sorted(o.return_pct for o in outcomes)
    n = len(returns)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "hit_rate": sum(1 for r in returns if r > 0) / n,
        "mean": sum(returns) / n,
        "median": returns[n // 2] if n % 2 else (returns[n // 2 - 1] + returns[n // 2]) / 2,
        "best": returns[-1],
        "worst": returns[0],
        "mean_drawdown": sum(o.max_drawdown for o in outcomes) / n,
        "worst_drawdown": min(o.max_drawdown for o in outcomes),
    }
