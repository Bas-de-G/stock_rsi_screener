"""Hand-checked Morningstar fair values, kept in a plain YAML file.

Why not in the database: the SQLite file is binary, gitignored locally, and
rewritten by the scheduled job. A value typed on a laptop would never reach
the published page, and two machines editing it would produce a binary merge
conflict nobody can resolve.

A committed YAML file solves all of that. It can be edited straight on
github.com with the pencil icon — no clone, no Python, no install — the diff
is readable in the commit history, and the next scheduled run picks it up and
republishes the dashboard.

Two spellings are accepted, so the quick one stays quick:

    IBM: 225.00

    TSLA:
      fair_value: 280.00
      checked: 2026-07-27
      note: post-earnings cut

`source` records where a number came from -- "manual" when someone typed it,
"scraped" when `screener scrape` read it off the page. It is omitted when
manual, so a hand-edited file stays as short as it looks above.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

_HEADER = """\
# Morningstar fair value estimates, checked by hand.
#
# Edit this file directly on GitHub: click the pencil icon, change a number,
# commit. The dashboard rebuilds on the next scheduled run and applies the
# valuation gate using what's here.
#
# Either form works:
#
#   IBM: 225.00
#
#   TSLA:
#     fair_value: 280.00
#     checked: 2026-07-27
#     note: post-earnings cut
#
# Written by `screener fair-value SYM <value>`, which rewrites this file --
# so any comments you add below this header will not survive that command.
"""


@dataclass(frozen=True)
class FairValue:
    symbol: str
    fair_value: float
    checked: str | None = None
    note: str | None = None
    # How the number got here. Defaults to "manual" so files written before
    # the scraper existed keep loading unchanged.
    source: str = "manual"


class FairValueError(ValueError):
    """Raised when the YAML file can't be understood."""


def load_fair_values(path: Path) -> dict[str, FairValue]:
    """Read the file. A missing file is normal — it just means none recorded."""
    if not path.exists():
        return {}

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise FairValueError(f"{path.name} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise FairValueError(f"{path.name} should map tickers to values, got {type(raw).__name__}")

    out: dict[str, FairValue] = {}
    for symbol, entry in raw.items():
        key = str(symbol).upper()
        if isinstance(entry, dict):
            if "fair_value" not in entry:
                raise FairValueError(f"{key} in {path.name} has no `fair_value`")
            value = _as_number(entry["fair_value"], key, path)
            out[key] = FairValue(
                symbol=key,
                fair_value=value,
                checked=_as_text(entry.get("checked")),
                note=_as_text(entry.get("note")),
                source=_as_text(entry.get("source")) or "manual",
            )
        else:
            out[key] = FairValue(symbol=key, fair_value=_as_number(entry, key, path))
    return out


def _as_number(value, symbol: str, path: Path) -> float:
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        raise FairValueError(f"{symbol} in {path.name}: {value!r} is not a number") from None
    if number <= 0:
        raise FairValueError(f"{symbol} in {path.name}: fair value must be positive, got {number}")
    return number


def _as_text(value) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def save_fair_value(
    path: Path,
    symbol: str,
    fair_value: float,
    checked: str | None = None,
    note: str | None = None,
    source: str = "manual",
) -> dict[str, FairValue]:
    """Record one value and rewrite the file, keeping every other entry."""
    values = load_fair_values(path)
    values[symbol.upper()] = FairValue(
        symbol=symbol.upper(),
        fair_value=fair_value,
        checked=checked or dt.date.today().isoformat(),
        note=note,
        source=source,
    )
    write_fair_values(path, values)
    return values


def write_fair_values(path: Path, values: dict[str, FairValue]) -> None:
    body: dict[str, object] = {}
    for symbol in sorted(values):
        entry = values[symbol]
        record: dict[str, object] = {"fair_value": entry.fair_value}
        if entry.checked:
            record["checked"] = entry.checked
        if entry.note:
            record["note"] = entry.note
        # "manual" is the default on load, so writing it back only adds noise
        # to the diff of a hand-edited file.
        if entry.source and entry.source != "manual":
            record["source"] = entry.source
        body[symbol] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(body, sort_keys=False, default_flow_style=False, allow_unicode=True)
    path.write_text(f"{_HEADER}\n{rendered}", encoding="utf-8")
