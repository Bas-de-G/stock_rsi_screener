"""SQLite persistence for RSI history, Morningstar valuations, and fired signals.

Three tables, one job each:

  rsi_history  — one row per (symbol, date): close + RSI. Filled by backfill
                 (Yahoo closes, computed locally) and by daily runs
                 (TradingView's own live RSI, trusted over any local estimate).
  valuations   — one row per (symbol, date): Morningstar price + fair value.
                 Only ever filled by daily runs — Morningstar's historical
                 fair value isn't available to backfill.
  signals      — one row per completed up/down/up RSI pattern per symbol,
                 keyed so a pattern is recorded (and possibly fired) exactly
                 once even if the tool runs against the same history again.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rsi_history (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,   -- ISO yyyy-mm-dd
    close  REAL NOT NULL,
    rsi    REAL NOT NULL,
    source TEXT NOT NULL,   -- 'backfill:yahoo' | 'live:tradingview'
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS valuations (
    symbol         TEXT NOT NULL,
    date           TEXT NOT NULL,  -- date we captured this, ISO yyyy-mm-dd
    price          REAL NOT NULL,  -- live price shown at top of the MS quote page
    fair_value     REAL NOT NULL,
    fair_value_date TEXT,          -- date MS itself attaches to the fair value estimate
    uncertainty    TEXT,
    moat           TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS signals (
    symbol             TEXT NOT NULL,
    up1_date           TEXT NOT NULL,  -- first upward cross of the threshold
    down_date          TEXT NOT NULL,  -- crosses back below
    up2_date           TEXT NOT NULL,  -- second upward cross -- the signal date
    price              REAL,           -- price on up2_date, if known
    fair_value         REAL,           -- fair value on up2_date, if known
    valuation_known    INTEGER NOT NULL,  -- 1 if we had a valuation for up2_date
    valuation_pass     INTEGER NOT NULL,  -- 1 if the valuation gate was satisfied
    fired              INTEGER NOT NULL,  -- 1 if this counts as an actual buy signal
    recorded_at        TEXT NOT NULL,     -- when this row was written
    PRIMARY KEY (symbol, up2_date)
);
"""


@dataclass(frozen=True)
class RsiPoint:
    symbol: str
    date: str
    close: float
    rsi: float
    source: str


@dataclass(frozen=True)
class Valuation:
    symbol: str
    date: str
    price: float
    fair_value: float
    fair_value_date: str | None
    uncertainty: str | None
    moat: str | None


@dataclass(frozen=True)
class Signal:
    symbol: str
    up1_date: str
    down_date: str
    up2_date: str
    price: float | None
    fair_value: float | None
    valuation_known: bool
    valuation_pass: bool
    fired: bool
    recorded_at: str


class Store:
    """Thin wrapper around one SQLite file. Not thread-safe by design —
    this tool runs as a single daily batch job, not a server."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- rsi_history ---------------------------------------------------

    def upsert_rsi_point(self, point: RsiPoint) -> None:
        """Insert, but never let a backfilled estimate clobber a live reading."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT source FROM rsi_history WHERE symbol=? AND date=?",
                (point.symbol, point.date),
            )
            existing = cur.fetchone()
            if existing and existing["source"] == "live:tradingview" and point.source != "live:tradingview":
                return
            cur.execute(
                """INSERT INTO rsi_history (symbol, date, close, rsi, source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, date) DO UPDATE SET
                     close=excluded.close, rsi=excluded.rsi, source=excluded.source""",
                (point.symbol, point.date, point.close, point.rsi, point.source),
            )
        self._conn.commit()

    def rsi_series(self, symbol: str) -> list[RsiPoint]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM rsi_history WHERE symbol=? ORDER BY date ASC",
                (symbol,),
            )
            return [
                RsiPoint(row["symbol"], row["date"], row["close"], row["rsi"], row["source"])
                for row in cur.fetchall()
            ]

    # -- valuations ------------------------------------------------------

    def upsert_valuation(self, val: Valuation) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO valuations
                     (symbol, date, price, fair_value, fair_value_date, uncertainty, moat)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, date) DO UPDATE SET
                     price=excluded.price, fair_value=excluded.fair_value,
                     fair_value_date=excluded.fair_value_date,
                     uncertainty=excluded.uncertainty, moat=excluded.moat""",
                (
                    val.symbol,
                    val.date,
                    val.price,
                    val.fair_value,
                    val.fair_value_date,
                    val.uncertainty,
                    val.moat,
                ),
            )
        self._conn.commit()

    def valuation(self, symbol: str, date: str) -> Valuation | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM valuations WHERE symbol=? AND date=?", (symbol, date)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Valuation(
            row["symbol"],
            row["date"],
            row["price"],
            row["fair_value"],
            row["fair_value_date"],
            row["uncertainty"],
            row["moat"],
        )

    def latest_valuations(self) -> list[Valuation]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT v.* FROM valuations v
                   INNER JOIN (
                     SELECT symbol, MAX(date) AS max_date FROM valuations GROUP BY symbol
                   ) latest ON v.symbol = latest.symbol AND v.date = latest.max_date"""
            )
            rows = cur.fetchall()
        return [
            Valuation(r["symbol"], r["date"], r["price"], r["fair_value"], r["fair_value_date"], r["uncertainty"], r["moat"])
            for r in rows
        ]

    # -- signals -----------------------------------------------------------

    def signal_exists(self, symbol: str, up2_date: str) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND up2_date=?", (symbol, up2_date)
            )
            return cur.fetchone() is not None

    def record_signal(self, sig: Signal) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT OR IGNORE INTO signals
                     (symbol, up1_date, down_date, up2_date, price, fair_value,
                      valuation_known, valuation_pass, fired, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sig.symbol,
                    sig.up1_date,
                    sig.down_date,
                    sig.up2_date,
                    sig.price,
                    sig.fair_value,
                    int(sig.valuation_known),
                    int(sig.valuation_pass),
                    int(sig.fired),
                    sig.recorded_at,
                ),
            )
        self._conn.commit()

    def symbols(self) -> list[str]:
        """Every symbol that has any stored history."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """SELECT symbol FROM rsi_history
                   UNION SELECT symbol FROM valuations
                   ORDER BY symbol"""
            )
            return [r["symbol"] for r in cur.fetchall()]

    def all_signals(self, symbol: str | None = None) -> list[Signal]:
        with closing(self._conn.cursor()) as cur:
            if symbol:
                cur.execute("SELECT * FROM signals WHERE symbol=? ORDER BY up2_date ASC", (symbol,))
            else:
                cur.execute("SELECT * FROM signals ORDER BY up2_date ASC")
            rows = cur.fetchall()
        return [
            Signal(
                r["symbol"], r["up1_date"], r["down_date"], r["up2_date"],
                r["price"], r["fair_value"], bool(r["valuation_known"]),
                bool(r["valuation_pass"]), bool(r["fired"]), r["recorded_at"],
            )
            for r in rows
        ]


def export_csv_snapshot(store: Store, csv_dir: Path) -> Path:
    """Write a friend-readable snapshot: latest valuation + latest RSI per symbol."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    out_path = csv_dir / "latest.csv"
    valuations = {v.symbol: v for v in store.latest_valuations()}
    rows = []
    for symbol in store.symbols():
        series = store.rsi_series(symbol)
        last = series[-1] if series else None
        val = valuations.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "date": last.date if last else "",
                "price": val.price if val else "",
                "fair_value": val.fair_value if val else "",
                "rsi": round(last.rsi, 2) if last else "",
                "fair_value_below_price": (
                    "" if not val else str(val.fair_value < val.price)
                ),
            }
        )
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["symbol", "date", "price", "fair_value", "rsi", "fair_value_below_price"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def append_signal_csv(csv_dir: Path, sig: Signal) -> Path:
    """Append one fired/considered signal to a running log CSV a friend can open in Excel."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    out_path = csv_dir / "signals.csv"
    is_new = not out_path.exists()
    with out_path.open("a", newline="") as f:
        fieldnames = [
            "symbol", "up1_date", "down_date", "up2_date",
            "price", "fair_value", "valuation_known", "valuation_pass", "fired", "recorded_at",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "symbol": sig.symbol,
                "up1_date": sig.up1_date,
                "down_date": sig.down_date,
                "up2_date": sig.up2_date,
                "price": sig.price,
                "fair_value": sig.fair_value,
                "valuation_known": sig.valuation_known,
                "valuation_pass": sig.valuation_pass,
                "fired": sig.fired,
                "recorded_at": sig.recorded_at,
            }
        )
    return out_path
