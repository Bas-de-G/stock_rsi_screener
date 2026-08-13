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
