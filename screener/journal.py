"""What the screener recommended, recorded as it was recommended.

The point of this file is to make "is any of this working?" a question with an
answer. That needs a record of what was said *at the time*, and the obvious
candidate for one -- the `signals` table -- cannot be it.

`cli._rescore_signals` rewrites every signal's `price`, `fair_value` and
`valuation_pass` whenever a fair value is recorded, across the whole history.
That is correct for the dashboard, which should show today's verdict on an old
pattern, and fatal for measurement: a signal recorded in March carries August's
valuation, so any hit rate computed from it is reading the future. The table is
a live view, not a ledger.

So this is a separate, append-only ledger. A row is written the first time a
recommendation is published and never touched again -- not corrected, not
re-scored, not deleted -- because a record you would edit is not evidence.

It lives in a committed CSV at the repo root rather than in `data/screener.db`,
for the reason `screener.notified` gives: CI only commits the database on the
last run of the day, so anything an intraday run writes there is discarded
before the next run reads it. It is also the format the analysis actually
wants -- readable in a diff, openable in Excel, one `read_csv` from pandas.

Columns are flat rather than a JSON blob so a spreadsheet is useful without
parsing anything. `extra` carries whatever a later factor adds (a Rule #1
score, a composite weighting) without a migration, and stays empty until then.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Bumped when a column's *meaning* changes, so an analysis can tell rows that
# are not comparable apart. Adding a column does not need it; redefining one
# does.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Recommendation:
    """One verdict on one pattern, as it stood when it was published."""

    decided_at: str
    symbol: str
    horizon: str
    direction: str
    up2_date: str          # the completing cross: this pattern's identity
    verdict: str           # strong | signal | sell_strong | sell | suspended
    fresh: int             # 1 if the pattern had just completed
    price: float | None
    currency: str
    rsi: float | None
    fair_value: float | None
    discount: float | None          # (fair_value - price) / price
    valuation_known: int
    valuation_pass: int
    earnings_growth: float | None
    earnings_growth_known: int
    earnings_growth_pass: int
    earnings_state: str             # clear | before | after
    earnings_sessions: int | None   # trading days to the release, if known
    margin: float                   # the horizon's required headroom
    leverage: int
    schema: int = SCHEMA_VERSION
    extra: str = ""                 # JSON, for factors added later

    @property
    def key(self) -> tuple[str, str, str, str]:
        """What makes this recommendation the same one on the next run."""
        return (self.symbol, self.horizon, self.direction, self.up2_date)


COLUMNS = [f.name for f in fields(Recommendation)]


class Journal:
    """The append-only ledger, loaded once and appended to as rows arrive."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._seen: set[tuple[str, str, str, str]] = set()
        self._rows_written = 0
        if self.path.exists():
            self._seen = self._load_keys()

    def _load_keys(self) -> set[tuple[str, str, str, str]]:
        keys = set()
        try:
            with self.path.open(newline="") as handle:
                for row in csv.DictReader(handle):
                    keys.add((
                        row.get("symbol", ""), row.get("horizon", ""),
                        row.get("direction", ""), row.get("up2_date", ""),
                    ))
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            # A damaged ledger must not stop the screener publishing. Starting
            # empty risks duplicate rows, which analysis can drop; refusing to
            # run would lose the day's data entirely.
            #
            # UnicodeDecodeError is the one that actually happens and the one
            # easiest to miss: it is a ValueError, not an OSError or a
            # csv.Error, so a handler for the two obvious cases lets binary
            # rubbish through and takes the dashboard build down with it. A
            # NUL-filled block, by contrast, the csv module reads quite
            # happily -- garbage keys are harmless here, since the worst they
            # cost is a duplicate row.
            print(f"  ! recommendation journal unreadable ({exc}) — appending anyway")
        return keys

    def has(self, recommendation: Recommendation) -> bool:
        return recommendation.key in self._seen

    def record(self, recommendation: Recommendation) -> bool:
        """Append one recommendation. Returns False if it was already logged.

        Appends immediately rather than batching, so a crash halfway through a
        run leaves the rows it did publish rather than losing all of them.
        """
        if self.has(recommendation):
            return False
        write_header = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(asdict(recommendation))
        self._seen.add(recommendation.key)
        self._rows_written += 1
        return True

    @property
    def added(self) -> int:
        return self._rows_written


def verdict_for(signal, direction: str, strong: bool, suspended: bool) -> str:
    """The label this pattern was published under.

    Per signal rather than per card. A ticker can carry a live buy and a live
    sell at once on different horizons, and "what did we say about this
    pattern" has to survive that.
    """
    if suspended:
        return "suspended"
    if direction == "sell":
        return "sell_strong" if strong else "sell"
    return "strong" if strong else "signal"


def recommendation_from(row, signal, horizon, now: dt.datetime | None = None) -> Recommendation:
    """Build a ledger row from a dashboard row and one of its signals."""
    from .signals import is_strong, signal_is_fresh

    now = now or dt.datetime.now()
    strong = is_strong(
        (bool(signal.valuation_known), bool(signal.valuation_pass)),
        (bool(signal.earnings_growth_known), bool(signal.earnings_growth_pass)),
    )
    discount = (
        (signal.fair_value - signal.price) / signal.price
        if signal.price and signal.fair_value else None
    )
    return Recommendation(
        decided_at=now.isoformat(timespec="seconds"),
        symbol=row.symbol,
        horizon=horizon.key,
        direction=signal.direction,
        up2_date=signal.up2_date,
        verdict=verdict_for(signal, signal.direction, strong, row.suspended),
        fresh=int(signal_is_fresh(signal, row.series, horizon)),
        price=signal.price if signal.price is not None else (
            row.latest.close if row.latest else None
        ),
        currency=row.currency,
        rsi=row.rsi,
        fair_value=signal.fair_value,
        discount=discount,
        valuation_known=int(bool(signal.valuation_known)),
        valuation_pass=int(bool(signal.valuation_pass)),
        earnings_growth=signal.earnings_growth,
        earnings_growth_known=int(bool(signal.earnings_growth_known)),
        earnings_growth_pass=int(bool(signal.earnings_growth_pass)),
        earnings_state=row.earnings.state,
        earnings_sessions=row.earnings.sessions,
        margin=horizon.margin,
        leverage=horizon.leverage,
    )
