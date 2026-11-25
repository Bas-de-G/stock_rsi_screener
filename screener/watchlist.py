"""Fair values for an outside ranked list, kept off the public repo.

Fred's Sunrise V2 exports a ranked watchlist as a CSV -- one US ticker a row,
most wanted first. This module answers one question per row: what does
Morningstar say it is worth? It does that in two passes, in the order Fred
asked for:

1. **Fill** every row the screener already holds a fair value for, straight
   from `fair_values.yaml`. Free, instant, no Morningstar visit.
2. **Scrape** the next N rows in rank order, through the same logged-in
   browser and the same pacing as `scrape`.

The output is a copy of the input CSV with fair-value columns appended. It
never touches `fair_values.yaml` and refuses to land anywhere git would
commit: the agreement with Fred is that these values are not published, and
this repo is public. `private/` is gitignored for exactly this.

Every row ends in exactly one status, so "Morningstar has no fair value for
this" never reads like "we failed to look":

* ``covered``          -- a fair value, with its date and source
* ``no fair value``    -- the page loaded, signed in, and shows no estimate;
                          almost always a stock Morningstar's analysts skip
* ``failed``           -- the visit went wrong; the note says how. Retried
                          on the next run.
* ``not listed in US`` -- no NASDAQ/NYSE/AMEX listing, so no page to open
* ``not checked yet``  -- below this run's cut-off; a later run gets there
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

RANK_COLUMN = "Watchlist Rank"
TICKER_COLUMN = "Ticker"
PRICE_COLUMN = "Stock Price (USD)"

COVERED = "covered"
NO_FAIR_VALUE = "no fair value"
FAILED = "failed"
UNLISTED = "not listed in US"
PENDING = "not checked yet"

# Settled answers are not asked again until they go stale; a failure always is.
SETTLED = (COVERED, NO_FAIR_VALUE, UNLISTED)

FV_COLUMNS = (
    "Morningstar Fair Value",
    "Fair Value Currency",
    "Upside to Fair Value",
    "Fair Value Status",
    "Fair Value Checked",
    "Fair Value Source",
    "Morningstar Listing",
    "Fair Value Note",
)

# Primary US venues only, in the order a duplicate is resolved. Morningstar's
# exchange codes are the ISO MICs the rest of config.yaml already uses.
EXCHANGES = (("NASDAQ", "xnas"), ("NYSE", "xnys"), ("AMEX", "xase"))


class WatchlistError(RuntimeError):
    pass


@dataclass(frozen=True)
class Listing:
    tradingview: str   # "NASDAQ:IMDX"
    morningstar: str   # "xnas/imdx"
    close: float | None


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    """Read the CSV, ordered by rank. Rows without a rank keep file order,
    after the ranked ones."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    if TICKER_COLUMN not in columns:
        raise WatchlistError(f"{path.name} has no '{TICKER_COLUMN}' column")

    def rank(indexed):
        index, row = indexed
        try:
            return (0, float(row.get(RANK_COLUMN) or ""), index)
        except ValueError:
            return (1, 0.0, index)

    ordered = [row for _, row in sorted(enumerate(rows), key=rank)]
    return [c for c in columns if c not in FV_COLUMNS], ordered


def symbol_of(row: dict) -> str:
    return (row.get(TICKER_COLUMN) or "").strip().upper()


def merge_previous(rows: list[dict], previous: list[dict]) -> None:
    """Carry the fair-value columns over from an earlier output, by ticker.

    This is what makes a run resumable: Fred's file can be re-exported with a
    new ranking and the answers already collected still apply.
    """
    by_symbol = {symbol_of(r): r for r in previous}
    for row in rows:
        old = by_symbol.get(symbol_of(row))
        if old:
            for column in FV_COLUMNS:
                if old.get(column):
                    row[column] = old[column]


def resolve_listings(symbols: list[str], scan) -> dict[str, Listing]:
    """Find each symbol's primary US listing.

    `scan` takes a list of TradingView symbols and returns {symbol: close} for
    the ones that exist -- injected so tests never touch the network.
    """
    candidates = [f"{ex}:{s}" for s in symbols for ex, _ in EXCHANGES]
    found = scan(candidates)
    out: dict[str, Listing] = {}
    for symbol in symbols:
        for exchange, mic in EXCHANGES:
            tv = f"{exchange}:{symbol}"
            if tv in found:
                # Morningstar's URLs use the share-class dot as-is, lowercased.
                out[symbol] = Listing(tv, f"{mic}/{symbol.lower()}", found[tv])
                break
    return out


def _price(row: dict, listing: Listing | None) -> float | None:
    try:
        price = float(row.get(PRICE_COLUMN) or "")
        if price > 0:
            return price
    except ValueError:
        pass
    return listing.close if listing else None


def _upside(fair_value: float, price: float | None) -> str:
    # Same convention as the screener: fair value 50% above the price is +50%.
    if not price:
        return ""
    return f"{fair_value / price - 1:+.1%}"


def record(row: dict, status: str, *, listing: Listing | None = None,
           fair_value: float | None = None, checked: str = "", source: str = "",
           note: str = "") -> None:
    row["Fair Value Status"] = status
    row["Morningstar Listing"] = listing.morningstar if listing else row.get("Morningstar Listing", "")
    row["Morningstar Fair Value"] = f"{fair_value:g}" if fair_value is not None else ""
    row["Fair Value Currency"] = "USD" if fair_value is not None else ""
    row["Upside to Fair Value"] = (
        _upside(fair_value, _price(row, listing)) if fair_value is not None else ""
    )
    row["Fair Value Checked"] = checked
    row["Fair Value Source"] = source
    row["Fair Value Note"] = note


def fill_from_screener(rows: list[dict], known: dict[str, tuple[float, str]],
                       listings: dict[str, Listing]) -> int:
    """Copy in the fair values the screener already holds. Returns how many.

    `known` maps a US ticker to (fair value, checked date). It is built only
    from US-listed, dollar-quoted screener tickers: Fred's file is all US, and
    a bare "BARC" there is not the London line the screener holds.
    """
    filled = 0
    for row in rows:
        symbol = symbol_of(row)
        if symbol not in known:
            continue
        # Never overwrite a fresher scrape of the same stock with an older file value.
        if row.get("Fair Value Status") == COVERED and row.get("Fair Value Source") == "scraped" \
                and (row.get("Fair Value Checked") or "") > (known[symbol][1] or ""):
            continue
        value, checked = known[symbol]
        record(row, COVERED, listing=listings.get(symbol), fair_value=value,
               checked=checked or "", source="screener")
        filled += 1
    return filled


def mark_unchecked(rows: list[dict]) -> None:
    for row in rows:
        if not row.get("Fair Value Status"):
            record(row, PENDING)


def due(rows: list[dict], today: dt.date, max_age_days: int) -> list[dict]:
    """Rows to visit, in rank order: never asked, failed, or gone stale."""
    out = []
    for row in rows:
        # The screener's own `scrape` keeps those fresh; a visit here would
        # spend Fred's budget re-reading a stock that is already looked after.
        if row.get("Fair Value Source") == "screener":
            continue
        status = row.get("Fair Value Status") or PENDING
        if status in SETTLED:
            try:
                checked = dt.date.fromisoformat(row.get("Fair Value Checked") or "")
            except ValueError:
                checked = None
            if checked and (today - checked).days <= max_age_days:
                continue
            if status == UNLISTED and not checked:
                continue
        out.append(row)
    return out


def write_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Write atomically, so an interrupted run never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*columns, *FV_COLUMNS], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in [*columns, *FV_COLUMNS]})
    tmp.replace(path)


_DATED = re.compile(r"^(?P<base>.+)_\d{4}-\d{2}-\d{2}$")


def has_fair_values(path: Path) -> bool:
    """Whether this CSV is one of our outputs rather than Fred's raw export."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh), [])
    return "Fair Value Status" in header


def output_base(source: Path) -> str:
    """The name outputs are dated from: Fred's file name, as he sent it.

    Feeding an earlier output back in (say, one downloaded again from the
    Drive folder) strips the date this tool added, so the next run is
    `<his name>_<today>` and not a growing chain of dates. A raw export keeps
    its own date -- `..._Oversold_2026-11-20` is Fred's, not ours.
    """
    if has_fair_values(source):
        match = _DATED.match(source.stem)
        if match:
            return match.group("base")
    return source.stem


def default_output(source: Path, today: dt.date) -> Path:
    """`<Fred's file name>_<today>.csv`, beside the input."""
    return source.with_name(f"{output_base(source)}_{today.isoformat()}.csv")


def previous_outputs(source: Path) -> list[Path]:
    """Earlier dated outputs for this list, oldest first -- what a rerun on a
    later day resumes from, now that each day writes a new file."""
    base = output_base(source)
    found = [
        p for p in source.parent.glob(f"{glob.escape(base)}_????-??-??.csv")
        if _DATED.match(p.stem) and _DATED.match(p.stem).group("base") == base
    ]
    return sorted(found)


def assert_private(path: Path, repo_root: Path) -> None:
    """Refuse to write somewhere git would publish.

    Outside the repo is fine. Inside it, only a gitignored path is -- the
    output holds Morningstar values that were agreed not to be shared, and the
    repo is public.
    """
    target = path.resolve()
    root = repo_root.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", str(target)],
        capture_output=True,
    ).returncode == 0
    if not ignored:
        raise WatchlistError(
            f"{path} is inside the repo and not gitignored, so it could be committed "
            "and published. Write it outside the repo, or under private/."
        )
