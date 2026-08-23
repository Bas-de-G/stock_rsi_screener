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
parsing anything -- and a new factor gets real columns rather than being hidden
in `extra`, because a spreadsheet is where these numbers get argued with. Rule
#1 travels as seven of them, the growth rate included, since a sticker price
without the assumption behind it cannot be argued with at all. `extra` remains
for anything not worth a column of its own.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Bumped when a column's *meaning* changes, so an analysis can tell rows that
# are not comparable apart. Adding a column does not need it; redefining one
# does.
# 2 added the conviction score, its band, its coverage and the weights in force.
SCHEMA_VERSION = 2


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
    # Phil Town's Rule #1, as it stood. Promoted to real columns rather than
    # left in `extra`, because a spreadsheet is where these get argued with --
    # and the growth rate has to travel with the sticker price it produced.
    r1_score: int | None = None
    r1_band: str = ""
    r1_growth: float | None = None          # base case used for the sticker
    r1_conservative_growth: float | None = None
    r1_implied_growth: float | None = None  # what the price demanded
    r1_headroom: float | None = None        # base minus implied, in points
    r1_sticker: float | None = None
    r1_mos: float | None = None
    r1_to_sticker: float | None = None      # (sticker - price) / price
    r1_big_four: int | None = None
    # The weighted conviction score, and the weights that produced it. Both,
    # because the score is meaningless later without them: re-weighting is a
    # config edit, and a column of scores computed under three different
    # weightings that does not say which is which cannot be measured at all.
    conviction: int | None = None
    conviction_band: str = ""
    conviction_coverage: float | None = None   # share of weight actually known
    conviction_weights: str = ""               # JSON, as it stood at the time
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
            self._widen()
            self._seen = self._load_keys()

    def _widen(self) -> None:
        """Rewrite the file under the current header when columns have been added.

        The rows are appended one at a time, which is what makes a crash
        mid-run lose nothing -- but it also means a `DictWriter` built from
        today's `COLUMNS` will happily write 37 values under a header that
        lists 23, and every column from that point on is silently misaligned.

        That is not hypothetical. It had already happened: when the Rule #1
        columns were added, 54 of the 488 rows on file were written 33 wide
        under a 23-wide header, so every Rule #1 value in the journal -- in the
        one file that exists to answer "did any of this work?" -- was
        unreadable by any CSV reader. Nothing warned, because a ragged CSV is
        perfectly valid text.

        The recovery works because every column ever added went in *before*
        `schema` and `extra`, which have always been last. So a row of width W
        is the first W-2 columns plus those two, whatever generation wrote it.
        """
        try:
            with self.path.open(newline="") as handle:
                rows = list(csv.reader(handle))
        except (OSError, csv.Error, UnicodeDecodeError):
            return          # _load_keys reports it; never block the run
        if not rows or rows[0] == COLUMNS:
            return

        tail = COLUMNS[-2:]          # schema, extra
        migrated = []
        for row in rows[1:]:
            if not row:
                continue
            head = COLUMNS[:max(0, len(row) - len(tail))]
            names = head + tail
            migrated.append(dict(zip(names, row)))

        with self.path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, restval="")
            writer.writeheader()
            for row in migrated:
                writer.writerow({k: v for k, v in row.items() if k in COLUMNS})

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
        **_rule_one_fields(getattr(row, "rule_one", None)),
        **_conviction_fields(getattr(row, "conviction", None)),
    )


def _conviction_fields(comp) -> dict:
    """The composite score and the weights it was computed under.

    The weights travel with the score for the same reason the growth rate
    travels with the sticker price: without them the column is a number nobody
    can reproduce, and re-weighting is meant to be a config edit rather than an
    event. Sorted so the JSON is stable and a genuine change shows up in the
    diff instead of key ordering.
    """
    if comp is None:
        return {}
    weights = {c.key: c.weight for c in comp.contributions if c.weight}
    return {
        "conviction": comp.score,
        "conviction_band": comp.band,
        "conviction_coverage": round(comp.coverage, 4),
        "conviction_weights": json.dumps(weights, sort_keys=True, separators=(",", ":")),
    }


def _rule_one_fields(reading) -> dict:
    """The Rule #1 columns, or empties where there is no reading.

    A company with no usable earnings gets blanks rather than zeros: zero is a
    score, and "we could not value this" is not one.
    """
    if reading is None or not reading.applicable:
        return {"r1_band": reading.band if reading is not None else ""}
    return {
        "r1_score": reading.score,
        "r1_band": reading.band,
        "r1_growth": reading.growth,
        "r1_conservative_growth": reading.conservative_growth,
        "r1_implied_growth": reading.implied_growth,
        "r1_headroom": reading.headroom,
        "r1_sticker": reading.sticker,
        "r1_mos": reading.mos,
        "r1_to_sticker": reading.to_sticker,
        "r1_big_four": reading.big_four,
    }
