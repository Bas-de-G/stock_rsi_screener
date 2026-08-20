"""What has already been announced, in a file that survives the run.

This exists because the obvious home for it -- a table in `data/screener.db` --
turned out to be the one place it could not live.

The workflow defers the database snapshot to the last run of the day: the file
is a 50 MB binary blob and SQLite deltas badly in git, so committing it on every
half-hourly run would add roughly a gigabyte a month. Every intraday run
therefore starts from the database as it stood at last night's close. A record
written at 14:00 was simply gone by 14:30, so the 14:30 run found no record,
announced the same strong buy again, and GitHub sent a second email. Four or
five for the same pick over an afternoon.

So the ledger moved out of the database into this file: a few hundred bytes of
JSON, committed on *every* run, diffable, and readable by anyone wondering why
a notification did or didn't go out.

The key is the pattern -- kind, horizon, symbol, and the completing cross --
not a timestamp. A strong buy sits on the dashboard for as long as it stays
fresh, and the same pattern re-derived after a rebuild is still the same news.

That covers the same pattern twice. It does not cover the same *name* twice,
which turned out to be the louder problem -- see `COOLDOWN` below.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

# Long enough to outlive the freshest pattern several times over (a daily
# signal stays fresh for two days, a weekly one for two weeks), short enough
# that the file stays a readable few hundred bytes rather than a year of
# history nobody reads.
KEEP_FOR = dt.timedelta(days=60)

# How long a symbol stays quiet on one timeframe after it has been announced.
#
# Keying on the pattern alone was correct and not sufficient. Intraday bars are
# labelled with the minute the run happened, so a fresh pattern can complete on
# almost every half-hourly run -- each one genuinely new, each one passing the
# per-pattern check. ANET announced itself eleven times on the 1h chart in six
# hours on 2026-08-19: 15:29, 15:59, 16:41, 17:26, 17:51, 18:34, 19:24, 19:50,
# 20:26, 20:55, 21:31. Thirty of that day's alerts were really about a dozen
# opportunities.
#
# Rolling rather than per calendar day, so a signal at 23:50 cannot be followed
# by another at 00:10.
#
# Twelve hours, not twenty-four, and the difference matters. Replaying the 58
# alerts on file: anything from 8 to 18 hours cuts them to 34 and loses nothing.
# At 24 hours five real next-session signals disappear -- DECK, GRAB, HEIA and
# JOBY each came back the following morning 18 to 21 hours later, and a full day
# swallows them, because the market opens at roughly the same hour every day.
# Twelve sits in the middle of that plateau: longer than a trading session,
# shorter than the gap to the next one.
COOLDOWN = dt.timedelta(hours=12)


def key_for(kind: str, horizon: str, symbol: str, up2_date: str) -> str:
    """The identity of one piece of news, as a single flat string.

    Flat rather than nested so the JSON stays greppable and a diff of the file
    reads as one line added per notification.
    """
    return f"{kind}/{horizon}/{symbol}/{up2_date}"


class Ledger:
    """The set of announcements already made. Loads on construction, saves on
    every record, so a crash mid-run cannot lose an announcement that was
    already sent."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._sent: dict[str, str] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self._sent = dict(raw.get("sent", {}))
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                # A corrupt ledger must not stop the screener from publishing.
                # Starting empty risks one duplicate notification; raising here
                # would take the whole dashboard build down with it.
                print(f"  ! notification ledger unreadable ({exc}) — starting fresh")

    def seen(self, key: str) -> bool:
        return key in self._sent

    def last_sent(self, kind: str, horizon: str, symbol: str) -> dt.datetime | None:
        """When this symbol was last announced on this timeframe, any pattern.

        `seen` answers "is this exact news old?"; this answers "have we bothered
        them about this name recently?" -- the question the cooldown needs.
        """
        prefix = f"{kind}/{horizon}/{symbol}/"
        stamps = []
        for key, stamp in self._sent.items():
            if not key.startswith(prefix):
                continue
            try:
                stamps.append(dt.datetime.fromisoformat(stamp))
            except ValueError:
                # An unparseable stamp can't place the record in time. Skipping
                # it risks one extra alert; guessing risks silencing a real one.
                continue
        return max(stamps, default=None)

    def record(self, key: str, now: dt.datetime | None = None) -> None:
        """Mark one announcement as made. Idempotent."""
        now = now or dt.datetime.now()
        self._sent.setdefault(key, now.isoformat(timespec="seconds"))
        self._prune(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sent": dict(sorted(self._sent.items()))}, indent=2) + "\n"
        )

    def _prune(self, now: dt.datetime) -> None:
        cutoff = now - KEEP_FOR
        for key, stamp in list(self._sent.items()):
            try:
                if dt.datetime.fromisoformat(stamp) < cutoff:
                    del self._sent[key]
            except ValueError:
                # An unparseable stamp is not worth dropping a record over --
                # keeping it costs a line and preserves the dedupe.
                continue
