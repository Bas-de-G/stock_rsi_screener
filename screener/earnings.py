"""The event-risk window around an earnings release.

A stock going oversold two days before results is not the same thing as a stock
going oversold in an ordinary correction, even though RSI cannot tell them
apart. The move that produced the low may be positioning rather than
information, and whatever the chart says, the price gaps on the release. Buying
the technical signal into that is taking a coin flip the model never priced.

So a signal inside the window is *suspended* rather than deleted: the card
still shows it, badged with when results land, but it does not earn a rocket,
cannot be the deal of the day, and does not reach anyone's phone. The pattern
stays in the log either way -- this is applied where signals are consumed, like
`signal_is_live`, never in detection.

Symmetrical by design. An overbought stock gapping *down* on results is the
same risk from the other side, and buys and sells are mirrored everywhere else
in this codebase.

The dates come free with the RSI: TradingView's scanner serves
`earnings_release_next_date` and `earnings_release_date` in the same batch
request, for US, European and Hong Kong listings alike.
"""

from __future__ import annotations

import datetime as dt

# Trading days before a release that the window opens. Three rather than two:
# positioning ahead of results starts before the last session, and the cost of
# being early is one deferred signal against the cost of being late, which is
# holding through a gap.
DEFAULT_BLACKOUT_DAYS = 3

BEFORE = "before"   # results are imminent
AFTER = "after"     # results are out, the first session has not closed
CLEAR = "clear"     # no release near enough to matter


class EarningsWindow:
    """Where a symbol sits relative to its next and last release.

    `sessions` is how many trading days until the release when state is BEFORE,
    and None otherwise -- it is what the badge quotes.
    """

    __slots__ = ("state", "sessions", "release")

    def __init__(self, state: str, sessions: int | None = None, release: dt.date | None = None):
        self.state = state
        self.sessions = sessions
        self.release = release

    @property
    def suspended(self) -> bool:
        return self.state != CLEAR

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EarningsWindow({self.state!r}, sessions={self.sessions})"


CLEAR_WINDOW = EarningsWindow(CLEAR)


def sessions_until(target: dt.date, today: dt.date) -> int:
    """Trading sessions between `today` and `target`, counting weekdays.

    Weekdays rather than the symbol's own bar dates, because those only exist
    for the *past* and this question is about the future -- there is no bar for
    next Tuesday to count. Every exchange on the watchlist trades Monday to
    Friday, so weekends are handled exactly; a market holiday inside the window
    is not, and makes the release one session closer than this says.

    That error has a direction, so the default absorbs it: the window opens
    three sessions out rather than two, which covers a single holiday and still
    only costs a deferred signal when there isn't one. Being a day early is
    cheap; being a day late means holding through the gap.
    """
    if target <= today:
        return 0
    day, sessions = today, 0
    while day < target:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            sessions += 1
    return sessions


def earnings_window(
    next_release: dt.date | None,
    last_release: dt.date | None,
    trading_days,
    today: dt.date | None = None,
    blackout_days: int = DEFAULT_BLACKOUT_DAYS,
) -> EarningsWindow:
    """Whether a signal on this symbol is actionable right now.

    The window opens `blackout_days` trading sessions before the release and
    closes once a full session has traded after it -- "the first trading day
    after results", which is when the gap has happened and the new information
    is in the price. That second half *is* answered from the symbol's own bar
    dates: the past is where real trading days exist.

    A symbol with no earnings date is CLEAR. That is deliberate: the feed does
    not always have a date, and refusing to signal on every stock whose
    calendar we cannot see would silently disable the screener.
    """
    today = today or dt.date.today()

    # Coming up. A `next_release` already in the past means the feed has not
    # caught up yet; that is the aftermath below, not an imminent release.
    if next_release is not None and next_release >= today:
        sessions = sessions_until(next_release, today)
        if sessions <= blackout_days:
            return EarningsWindow(BEFORE, sessions, next_release)

    # Just been. Suspended until a session has actually closed after the
    # release date, so a company reporting before the open clears at that day's
    # close and one reporting after the close clears at the next.
    past = [r for r in (last_release, next_release) if r is not None and r <= today]
    if past:
        release = max(past)
        if not any(day > release for day in trading_days):
            return EarningsWindow(AFTER, None, release)

    return CLEAR_WINDOW


def to_date(value) -> dt.date | None:
    """Read a release date from whatever the feed or the database supplies.

    TradingView serves epoch seconds; the database stores an ISO string. Both
    arrive here, and a null in either form means "no date", not an error.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).date()
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
