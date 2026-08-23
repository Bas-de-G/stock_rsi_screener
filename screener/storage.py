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

# Split per table rather than one blob: the multi-horizon migration has to
# re-create a single table in place (SQLite cannot ALTER a primary key), and
# needs that table's DDL on its own to do it.
_RSI_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS rsi_history (
    symbol  TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT '1d',  -- '1h' | '4h' | '1d' | '1w'
    -- 'yyyy-mm-dd' for the daily and weekly bars, full ISO 'yyyy-mm-ddThh:mm'
    -- for intraday. Sorts correctly as a string either way, which is what the
    -- ORDER BY in rsi_series relies on.
    date    TEXT NOT NULL,
    close   REAL NOT NULL,
    rsi     REAL NOT NULL,
    source  TEXT NOT NULL,   -- 'backfill:yahoo' | 'live:tradingview'
    -- YoY EPS growth (%), from TradingView's own scanner field -- only ever
    -- populated on a live row; backfill has no historical source for it, so
    -- these are NULL on backfilled dates.
    earnings_growth        REAL,
    earnings_growth_period TEXT,   -- 'ttm' | 'fy' | NULL
    PRIMARY KEY (symbol, horizon, date)
);
"""

_VALUATIONS_DDL = """
CREATE TABLE IF NOT EXISTS valuations (
    symbol         TEXT NOT NULL,
    date           TEXT NOT NULL,  -- date we captured this, ISO yyyy-mm-dd
    price          REAL NOT NULL,  -- live price shown at top of the MS quote page
    fair_value     REAL NOT NULL,
    fair_value_date TEXT,          -- date MS itself attaches to the fair value estimate
    uncertainty    TEXT,
    moat           TEXT,
    source         TEXT NOT NULL DEFAULT 'morningstar',  -- 'morningstar' | 'manual'
    PRIMARY KEY (symbol, date)
);
"""

_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    symbol             TEXT NOT NULL,
    up1_date           TEXT NOT NULL,  -- first upward cross of the threshold
    down_date          TEXT NOT NULL,  -- crosses back below
    up2_date           TEXT NOT NULL,  -- second upward cross -- the signal date
    price              REAL,           -- price on up2_date, if known
    fair_value         REAL,           -- fair value on up2_date, if known
    valuation_known    INTEGER NOT NULL,  -- 1 if we had a valuation for up2_date
    valuation_pass     INTEGER NOT NULL,  -- 1 if the valuation gate was satisfied
    -- Second grading factor, independent of the valuation gate above. Both
    -- factors only ever grade strength (the rocket) -- neither is required
    -- for `fired`, which stays governed by the valuation gate alone.
    earnings_growth       REAL,           -- YoY EPS growth (%) on up2_date, if known
    earnings_growth_known INTEGER NOT NULL DEFAULT 0,
    earnings_growth_pass  INTEGER NOT NULL DEFAULT 0,  -- 1 if growth was positive
    fired              INTEGER NOT NULL,  -- 1 if this counts as an actual buy signal
    recorded_at        TEXT NOT NULL,     -- when this row was written
    horizon            TEXT NOT NULL DEFAULT '1d',
    direction          TEXT NOT NULL DEFAULT 'buy',  -- 'buy' | 'sell'
    PRIMARY KEY (symbol, horizon, direction, up2_date)
);
"""

# NOTE: what has already been announced deliberately does NOT live here. It
# started as a `notifications` table and could not stay: this database is a
# 50 MB binary that CI only commits on the last run of the day, so every
# intraday run read a copy from last night and re-announced the morning's
# strong buys. It now lives in `screener.notified`, in a small file committed
# on every run. Older copies of the database still carry the dead table.

# When each company next reports, and when it last did. One row per symbol,
# overwritten every run from the same TradingView batch request that fetches
# RSI -- the dates are columns on that response, so this costs nothing extra.
#
# Not part of rsi_history despite arriving with it: a release date is a fact
# about the company on a calendar, not a reading taken at a bar, and keeping it
# per-bar would store the same date thousands of times over.
_EARNINGS_DDL = """
CREATE TABLE IF NOT EXISTS earnings_calendar (
    symbol       TEXT PRIMARY KEY,
    next_date    TEXT,   -- ISO date of the next release, if the feed has one
    next_at      TEXT,   -- full timestamp, which says before-open vs after-close
    last_date    TEXT,   -- ISO date of the most recent release
    updated_at   TEXT NOT NULL
);
"""

# What happened after each pattern, measured in daily bars from the signal.
#
# Safe to keep in the database, unlike the recommendation journal: every row
# here is *derived* from price history that is itself reconstructable from
# Yahoo, so losing the table costs one `evaluate` run and nothing else. The
# journal records a judgement made at a moment and can never be recomputed;
# this records arithmetic.
_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS outcomes (
    symbol        TEXT NOT NULL,
    horizon       TEXT NOT NULL,
    direction     TEXT NOT NULL,
    up2_date      TEXT NOT NULL,
    bars          INTEGER NOT NULL,  -- trading days after the signal
    entry         REAL NOT NULL,
    exit          REAL NOT NULL,
    -- Signed to the call: positive means the signal was right, so a sell
    -- followed by a fall scores positive just as a buy followed by a rise does.
    return_pct    REAL NOT NULL,
    max_gain      REAL NOT NULL,
    max_drawdown  REAL NOT NULL,
    evaluated_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, horizon, direction, up2_date, bars)
);
"""

# The Rule #1 reading for each company, recomputed every run from the same
# batch request that fetches RSI. One row per symbol, overwritten.
#
# The inputs are stored alongside the answer on purpose. A sticker price is a
# projection dominated by one assumption, so a number without the growth rate
# and the multiple it was built from cannot be argued with -- and arguing with
# it is the only safe way to use it.
_RULE_ONE_DDL = """
CREATE TABLE IF NOT EXISTS rule_one (
    symbol      TEXT PRIMARY KEY,
    price       REAL,
    growth      REAL,   -- base-case annual rate, in percent
    conservative_growth REAL,   -- the pessimistic case
    implied_growth      REAL,   -- what today's price already demands
    future_pe   REAL,
    sticker     REAL,   -- intrinsic value at a 15% required return (base case)
    sticker_low REAL,   -- the same at the conservative rate: the band's floor
    mos         REAL,   -- sticker halved: the margin-of-safety price
    eps         REAL,   -- the earnings base the whole projection rests on
    big_four    INTEGER,
    score       INTEGER,  -- 1-10, 0 when not applicable
    band        TEXT NOT NULL,  -- 'green' | 'amber' | 'red' | 'n/a'
    caution     TEXT,   -- set when the earnings base looks like a one-off
    reason      TEXT,   -- why it could not be read, when band is 'n/a'
    updated_at  TEXT NOT NULL
);
"""

SCHEMA = (_RSI_HISTORY_DDL + _VALUATIONS_DDL + _SIGNALS_DDL + _EARNINGS_DDL
          + _OUTCOMES_DDL + _RULE_ONE_DDL)



@dataclass(frozen=True)
class RsiPoint:
    symbol: str
    date: str
    close: float
    rsi: float
    source: str
    earnings_growth: float | None = None
    earnings_growth_period: str | None = None
    # Appended last with a default so every existing positional RsiPoint(...)
    # call keeps working; '1d' is what all pre-multi-horizon data was.
    horizon: str = "1d"


@dataclass(frozen=True)
class Valuation:
    symbol: str
    date: str
    price: float
    fair_value: float
    fair_value_date: str | None = None
    uncertainty: str | None = None
    moat: str | None = None
    source: str = "morningstar"


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
    # A second, independent grading factor alongside the valuation gate above.
    # Appended at the end with defaults so every existing positional Signal(...)
    # call keeps working unchanged. Never affects `fired` -- see signals.py.
    earnings_growth: float | None = None
    earnings_growth_known: bool = False
    earnings_growth_pass: bool = False
    horizon: str = "1d"
    direction: str = "buy"


class Store:
    """Thin wrapper around one SQLite file. Not thread-safe by design —
    this tool runs as a single daily batch job, not a server."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older database up to the current schema.

        Databases created before manual fair-value entry existed have no
        `source` column; adding it in place preserves already-collected
        history rather than forcing a rebuild. Same story for the earnings-
        growth columns added alongside the fair-value grading factor.
        """
        with closing(self._conn.cursor()) as cur:
            columns = {r["name"] for r in cur.execute("PRAGMA table_info(valuations)")}
            if columns and "source" not in columns:
                cur.execute(
                    "ALTER TABLE valuations ADD COLUMN source TEXT NOT NULL DEFAULT 'morningstar'"
                )

            rsi_columns = {r["name"] for r in cur.execute("PRAGMA table_info(rsi_history)")}
            if rsi_columns and "earnings_growth" not in rsi_columns:
                cur.execute("ALTER TABLE rsi_history ADD COLUMN earnings_growth REAL")
                cur.execute("ALTER TABLE rsi_history ADD COLUMN earnings_growth_period TEXT")

            signal_columns = {r["name"] for r in cur.execute("PRAGMA table_info(signals)")}
            if signal_columns and "earnings_growth" not in signal_columns:
                cur.execute("ALTER TABLE signals ADD COLUMN earnings_growth REAL")
                cur.execute(
                    "ALTER TABLE signals ADD COLUMN earnings_growth_known "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                cur.execute(
                    "ALTER TABLE signals ADD COLUMN earnings_growth_pass "
                    "INTEGER NOT NULL DEFAULT 0"
                )

            # Quoting one sticker price implied a precision the method hasn't
            # got, so the reading gained a second, more pessimistic one and the
            # earnings base it all rests on.
            rule_columns = {r["name"] for r in cur.execute("PRAGMA table_info(rule_one)")}
            if rule_columns and "sticker_low" not in rule_columns:
                cur.execute("ALTER TABLE rule_one ADD COLUMN sticker_low REAL")
                cur.execute("ALTER TABLE rule_one ADD COLUMN eps REAL")

            # Multi-horizon support widened both primary keys, and SQLite can't
            # ALTER a primary key -- the table has to be rebuilt. Everything
            # collected before this existed was the daily bar, so it migrates
            # to horizon='1d' and the screener carries on with its history
            # intact rather than starting over.
            self._add_horizon_column(cur, "rsi_history", _RSI_HISTORY_DDL)
            self._add_horizon_column(cur, "signals", _SIGNALS_DDL)
            # Sell signals widened the key again. Everything recorded before
            # they existed was a buy.
            self._widen_key(cur, "signals", "direction", "buy", _SIGNALS_DDL)

    @staticmethod
    def _add_horizon_column(cur, table: str, create_ddl: str) -> None:
        Store._widen_key(cur, table, "horizon", "1d", create_ddl)

    @staticmethod
    def _widen_key(cur, table: str, column: str, default: str, create_ddl: str) -> None:
        """Add a column that belongs in the primary key.

        SQLite cannot ALTER a primary key, so the table is rebuilt: rename the
        old one aside, create the current shape, copy every existing row across
        with `default` filled in for the new column, drop the original. Used
        twice now -- once for `horizon`, once for `direction` -- and it keeps
        collected history intact rather than forcing a rebuild from scratch.
        """
        columns = [r["name"] for r in cur.execute(f"PRAGMA table_info({table})")]
        if not columns or column in columns:
            return
        carried = ", ".join(columns)
        aside = f"{table}_pre_{column}"
        cur.execute(f"ALTER TABLE {table} RENAME TO {aside}")
        cur.executescript(create_ddl)
        cur.execute(
            f"INSERT INTO {table} ({carried}, {column}) "
            f"SELECT {carried}, '{default}' FROM {aside}"
        )
        cur.execute(f"DROP TABLE {aside}")

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
                "SELECT source FROM rsi_history WHERE symbol=? AND horizon=? AND date=?",
                (point.symbol, point.horizon, point.date),
            )
            existing = cur.fetchone()
            if existing and existing["source"] == "live:tradingview" and point.source != "live:tradingview":
                return
            cur.execute(
                """INSERT INTO rsi_history
                     (symbol, horizon, date, close, rsi, source,
                      earnings_growth, earnings_growth_period)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, horizon, date) DO UPDATE SET
                     close=excluded.close, rsi=excluded.rsi, source=excluded.source,
                     earnings_growth=excluded.earnings_growth,
                     earnings_growth_period=excluded.earnings_growth_period""",
                (
                    point.symbol, point.horizon, point.date, point.close, point.rsi,
                    point.source, point.earnings_growth, point.earnings_growth_period,
                ),
            )
        self._conn.commit()

    def rsi_series(self, symbol: str, horizon: str = "1d") -> list[RsiPoint]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM rsi_history WHERE symbol=? AND horizon=? ORDER BY date ASC",
                (symbol, horizon),
            )
            return [
                RsiPoint(
                    row["symbol"], row["date"], row["close"], row["rsi"], row["source"],
                    row["earnings_growth"], row["earnings_growth_period"], row["horizon"],
                )
                for row in cur.fetchall()
            ]

    def prune_unmeasurable_intraday(self, keep_bars: int, horizons=("1h", "4h")) -> int:
        """Drop intraday bars that nothing can read, and return how many went.

        The database is committed to git, and it only ever grows: bars are
        upserted and never removed, so three years of hourly history had piled
        up behind a dashboard that draws ninety bars. 78 MB, past the 50 MB at
        which GitHub starts warning.

        Two things read an intraday bar, and both have a horizon:

        * The chart draws the newest `keep_bars` of a series, and liveness is
          judged within a couple of days of the latest bar. Nothing older than
          the chart window is ever plotted.
        * `outcomes.forward_outcomes` reads the close on the bar a pattern
          completed on, then walks *daily* closes forward -- and it already
          refuses to measure a signal the daily series doesn't reach back to.
          An intraday bar older than the symbol's first daily bar therefore
          prices a pattern whose outcome is unknowable.

        So a bar that is both older than the chart window and older than the
        first daily bar is one no reader can reach. Deleting 283,005 of them
        left every dashboard page byte-identical and every one of the 17,828
        measured outcomes unchanged, and took the file to 46 MB.

        The floor is not redundant with the daily test. SPCX listed recently
        enough that its 4h history predates its daily history, so the daily
        test alone shortened its chart from 90 bars to 76.
        """
        placeholders = ",".join("?" for _ in horizons)
        # The floor is the date of the `keep_bars`-th newest bar; anything
        # strictly older than it goes, so exactly `keep_bars` survive. Offset
        # counts from zero, hence the minus one. A `keep_bars` of nothing means
        # no floor at all rather than an offset of -1.
        floor, params = "", tuple(horizons)
        if keep_bars > 0:
            floor = """AND r.date < COALESCE((
                         SELECT k.date FROM rsi_history k
                         WHERE k.symbol = r.symbol AND k.horizon = r.horizon
                         ORDER BY k.date DESC LIMIT 1 OFFSET ?
                       ), '')"""
            params = (*horizons, keep_bars - 1)
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                f"""DELETE FROM rsi_history WHERE rowid IN (
                      SELECT r.rowid FROM rsi_history r
                      WHERE r.horizon IN ({placeholders})
                        AND r.date < COALESCE((
                              SELECT MIN(substr(d.date, 1, 10)) FROM rsi_history d
                              WHERE d.symbol = r.symbol AND d.horizon = '1d'
                            ), '9999-99-99')
                        {floor}
                    )""",
                params,
            )
            removed = cur.rowcount
        self._conn.commit()
        if removed:
            # Deleting rows leaves the pages allocated, and the point of the
            # exercise is the size of the committed file.
            self._conn.execute("VACUUM")
        return removed

    # -- valuations ------------------------------------------------------

    def upsert_valuation(self, val: Valuation) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO valuations
                     (symbol, date, price, fair_value, fair_value_date, uncertainty, moat, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, date) DO UPDATE SET
                     price=excluded.price, fair_value=excluded.fair_value,
                     fair_value_date=excluded.fair_value_date,
                     uncertainty=excluded.uncertainty, moat=excluded.moat,
                     source=excluded.source""",
                (
                    val.symbol,
                    val.date,
                    val.price,
                    val.fair_value,
                    val.fair_value_date,
                    val.uncertainty,
                    val.moat,
                    val.source,
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
            row["source"],
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
            Valuation(
                r["symbol"], r["date"], r["price"], r["fair_value"],
                r["fair_value_date"], r["uncertainty"], r["moat"], r["source"],
            )
            for r in rows
        ]

    # -- signals -----------------------------------------------------------

    def signal_exists(
        self, symbol: str, up2_date: str, horizon: str = "1d", direction: str = "buy"
    ) -> bool:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND horizon=? AND direction=? "
                "AND up2_date=?",
                (symbol, horizon, direction, up2_date),
            )
            return cur.fetchone() is not None

    def record_signal(self, sig: Signal) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT OR IGNORE INTO signals
                     (symbol, up1_date, down_date, up2_date, price, fair_value,
                      valuation_known, valuation_pass, earnings_growth,
                      earnings_growth_known, earnings_growth_pass, fired,
                      recorded_at, horizon, direction)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sig.symbol,
                    sig.up1_date,
                    sig.down_date,
                    sig.up2_date,
                    sig.price,
                    sig.fair_value,
                    int(sig.valuation_known),
                    int(sig.valuation_pass),
                    sig.earnings_growth,
                    int(sig.earnings_growth_known),
                    int(sig.earnings_growth_pass),
                    int(sig.fired),
                    sig.recorded_at,
                    sig.horizon,
                    sig.direction,
                ),
            )
        self._conn.commit()

    def update_signal_valuation(
        self,
        symbol: str,
        up2_date: str,
        price: float,
        fair_value: float,
        known: bool,
        confirms: bool,
        fired: bool,
        horizon: str = "1d",
        direction: str = "buy",
    ) -> None:
        """Re-score an existing pattern once a fair value becomes available.

        A pattern recorded before anyone checked the valuation can legitimately
        become a fired signal later, so this updates in place rather than
        writing a second row for the same pattern.

        Scoped to one horizon because the gate is horizon-dependent: the same
        Morningstar fair value clears the 10% margin a 1h signal needs while
        failing the 50% a 1w signal needs, so `confirms` genuinely differs per
        horizon for identical inputs.
        """
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """UPDATE signals
                      SET price=?, fair_value=?, valuation_known=?, valuation_pass=?, fired=?
                    WHERE symbol=? AND horizon=? AND direction=? AND up2_date=?""",
                (price, fair_value, int(known), int(confirms), int(fired),
                 symbol, horizon, direction, up2_date),
            )
        self._conn.commit()

    def update_signal_earnings_growth(
        self, symbol: str, horizon: str, growth: float | None, known: bool,
        passes: bool, direction: str = "buy",
    ) -> None:
        """Re-score every recorded pattern for one symbol against the *current*
        earnings growth.

        Deliberately not pinned to the pattern's own date. Earnings growth is a
        fundamental that describes the company now and moves quarterly, not a
        price fact belonging to a particular bar — and the bar at a pattern's
        second cross is almost always backfilled, which carries no growth
        figure at all. Pinning it there left the factor permanently unknown on
        every signal, so it never graded anything. This mirrors how the
        valuation factor already works: `sync_fair_values` scores against the
        latest close and today's fair value, not the values on the signal date.
        """
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """UPDATE signals
                      SET earnings_growth=?, earnings_growth_known=?, earnings_growth_pass=?
                    WHERE symbol=? AND horizon=? AND direction=?""",
                (growth, int(known), int(passes), symbol, horizon, direction),
            )
        self._conn.commit()

    def manual_valuation_symbols(self) -> list[str]:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT DISTINCT symbol FROM valuations WHERE source='manual'")
            return [r["symbol"] for r in cur.fetchall()]

    def upsert_earnings(
        self, symbol: str, next_date: str | None, next_at: str | None, last_date: str | None
    ) -> None:
        """Record when a company next reports. One row per symbol, overwritten."""
        import datetime as _dt

        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO earnings_calendar (symbol, next_date, next_at, last_date, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       next_date=excluded.next_date,
                       next_at=excluded.next_at,
                       last_date=excluded.last_date,
                       updated_at=excluded.updated_at""",
                (symbol, next_date, next_at, last_date,
                 _dt.datetime.now().isoformat(timespec="seconds")),
            )
        self._conn.commit()

    def replace_outcomes(self, outcomes) -> int:
        """Write measured outcomes, overwriting any earlier measurement.

        Overwrite rather than skip: a window measured last week against a
        history that has since been corrected should take the new answer. These
        are derived numbers, so the latest computation is by definition the
        best one.
        """
        import datetime as _dt

        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        rows = [
            (o.symbol, o.horizon, o.direction, o.up2_date, o.bars, o.entry,
             o.exit, o.return_pct, o.max_gain, o.max_drawdown, stamp)
            for o in outcomes
        ]
        if not rows:
            return 0
        with closing(self._conn.cursor()) as cur:
            cur.executemany(
                """INSERT INTO outcomes (symbol, horizon, direction, up2_date, bars,
                                         entry, exit, return_pct, max_gain,
                                         max_drawdown, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, horizon, direction, up2_date, bars) DO UPDATE SET
                       entry=excluded.entry, exit=excluded.exit,
                       return_pct=excluded.return_pct, max_gain=excluded.max_gain,
                       max_drawdown=excluded.max_drawdown,
                       evaluated_at=excluded.evaluated_at""",
                rows,
            )
        self._conn.commit()
        return len(rows)

    def all_outcomes(self, bars: int | None = None, horizon: str | None = None):
        """Measured outcomes, optionally for one window or one timeframe."""
        from .outcomes import Outcome

        sql = ("SELECT symbol, horizon, direction, up2_date, bars, entry, exit,"
               " return_pct, max_gain, max_drawdown FROM outcomes")
        clauses, params = [], []
        if bars is not None:
            clauses.append("bars=?")
            params.append(bars)
        if horizon is not None:
            clauses.append("horizon=?")
            params.append(horizon)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with closing(self._conn.cursor()) as cur:
            cur.execute(sql, params)
            return [Outcome(*row) for row in cur.fetchall()]

    def upsert_rule_one(self, symbol: str, reading) -> None:
        """Record a Rule #1 reading. One row per symbol, overwritten each run."""
        import datetime as _dt

        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO rule_one (symbol, price, growth, conservative_growth,
                                         implied_growth, future_pe, sticker,
                                         sticker_low, mos, eps,
                                         big_four, score, band, caution, reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       price=excluded.price, growth=excluded.growth,
                       conservative_growth=excluded.conservative_growth,
                       implied_growth=excluded.implied_growth,
                       future_pe=excluded.future_pe, sticker=excluded.sticker,
                       sticker_low=excluded.sticker_low,
                       mos=excluded.mos, eps=excluded.eps,
                       big_four=excluded.big_four,
                       score=excluded.score, band=excluded.band,
                       caution=excluded.caution, reason=excluded.reason,
                       updated_at=excluded.updated_at""",
                (symbol, reading.price, reading.growth, reading.conservative_growth,
                 reading.implied_growth, reading.future_pe,
                 reading.sticker, reading.sticker_low, reading.mos, reading.eps,
                 reading.big_four, reading.score,
                 reading.band, reading.caution, reading.reason,
                 _dt.datetime.now().isoformat(timespec="seconds")),
            )
        self._conn.commit()

    def rule_one_readings(self) -> dict:
        """Every symbol's Rule #1 reading, keyed by symbol."""
        from .ruleone import RuleOne

        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT symbol, price, growth, conservative_growth, implied_growth,"
                " future_pe, sticker, sticker_low, mos, eps, big_four, score,"
                " band, caution, reason FROM rule_one"
            )
            return {
                r["symbol"]: RuleOne(
                    applicable=r["band"] != "n/a", reason=r["reason"] or "",
                    growth=r["growth"],
                    conservative_growth=r["conservative_growth"],
                    implied_growth=r["implied_growth"],
                    future_pe=r["future_pe"], sticker=r["sticker"],
                    sticker_low=r["sticker_low"], eps=r["eps"],
                    mos=r["mos"], price=r["price"], big_four=r["big_four"] or 0,
                    score=r["score"] or 0, band=r["band"], caution=r["caution"] or "",
                )
                for r in cur.fetchall()
            }

    def earnings_dates(self) -> dict[str, tuple[str | None, str | None]]:
        """Every symbol's (next, last) release date, as ISO strings."""
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT symbol, next_date, last_date FROM earnings_calendar")
            return {r["symbol"]: (r["next_date"], r["last_date"]) for r in cur.fetchall()}

    def delete_manual_valuations(self, symbol: str) -> None:
        """Drop hand-entered valuations for a symbol.

        Used when an entry disappears from the YAML file, which is the source
        of truth for manual values — leaving a stale row would show a fair
        value on the dashboard that isn't in the file any more. Scraped
        (source='morningstar') rows are historical observations and are left
        alone.
        """
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "DELETE FROM valuations WHERE symbol=? AND source='manual'", (symbol,)
            )
        self._conn.commit()

    def clear_signal_valuation(self, symbol: str, fire_without_valuation: bool) -> None:
        """Return a symbol's signals to the un-checked state."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """UPDATE signals
                      SET price=NULL, fair_value=NULL, valuation_known=0,
                          valuation_pass=?, fired=?
                    WHERE symbol=?""",
                (int(fire_without_valuation), int(fire_without_valuation), symbol),
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

    def all_signals(
        self, symbol: str | None = None, horizon: str | None = None,
        direction: str | None = None,
    ) -> list[Signal]:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol=?"); params.append(symbol)
        if horizon:
            clauses.append("horizon=?"); params.append(horizon)
        if direction:
            clauses.append("direction=?"); params.append(direction)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._conn.cursor()) as cur:
            cur.execute(f"SELECT * FROM signals {where} ORDER BY up2_date ASC", params)
            rows = cur.fetchall()
        return [
            Signal(
                r["symbol"], r["up1_date"], r["down_date"], r["up2_date"],
                r["price"], r["fair_value"], bool(r["valuation_known"]),
                bool(r["valuation_pass"]), bool(r["fired"]), r["recorded_at"],
                r["earnings_growth"], bool(r["earnings_growth_known"]),
                bool(r["earnings_growth_pass"]), r["horizon"], r["direction"],
            )
            for r in rows
        ]


def export_csv_snapshot(store: Store, csv_dir: Path, horizon: str = "1d") -> Path:
    """Write a friend-readable snapshot: latest valuation + latest RSI per symbol."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    out_path = csv_dir / "latest.csv"
    valuations = {v.symbol: v for v in store.latest_valuations()}
    rows = []
    for symbol in store.symbols():
        series = store.rsi_series(symbol, horizon)
        last = series[-1] if series else None
        val = valuations.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "date": last.date if last else "",
                "price": val.price if val else "",
                "fair_value": val.fair_value if val else "",
                "rsi": round(last.rsi, 2) if last else "",
                "earnings_growth": (
                    round(last.earnings_growth, 2)
                    if last and last.earnings_growth is not None
                    else ""
                ),
                "fair_value_below_price": (
                    "" if not val else str(val.fair_value < val.price)
                ),
            }
        )
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol", "date", "price", "fair_value", "rsi",
                "earnings_growth", "fair_value_below_price",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return out_path


_SIGNAL_CSV_FIELDS = [
    "symbol", "up1_date", "down_date", "up2_date",
    "price", "fair_value", "valuation_known", "valuation_pass",
    "earnings_growth", "earnings_growth_known", "earnings_growth_pass",
    "fired", "recorded_at", "horizon", "direction",
]


def _signal_csv_row(sig: Signal) -> dict:
    return {
        "symbol": sig.symbol,
        "up1_date": sig.up1_date,
        "down_date": sig.down_date,
        "up2_date": sig.up2_date,
        "price": sig.price,
        "fair_value": sig.fair_value,
        "valuation_known": sig.valuation_known,
        "valuation_pass": sig.valuation_pass,
        "earnings_growth": sig.earnings_growth,
        "earnings_growth_known": sig.earnings_growth_known,
        "earnings_growth_pass": sig.earnings_growth_pass,
        "fired": sig.fired,
        "recorded_at": sig.recorded_at,
        "horizon": sig.horizon,
        "direction": sig.direction,
    }


def append_signal_csv(csv_dir: Path, sig: Signal) -> Path:
    """Append one fired/considered signal to a running log CSV a friend can open in Excel.

    Rewrites the file rather than a true append: this CSV is force-committed
    to `main` by CI, so a copy written under an older header (e.g. before the
    earnings-growth columns existed) is sitting in git history right now.
    Blindly appending new-format rows below an old header would misalign
    every column from that point on. Reading the existing rows back through
    `DictReader` and rewriting under the current header costs nothing at this
    file's size (a signal fires rarely) and can't drift out of alignment.
    """
    csv_dir.mkdir(parents=True, exist_ok=True)
    out_path = csv_dir / "signals.csv"

    existing_rows: list[dict] = []
    if out_path.exists():
        with out_path.open(newline="") as f:
            existing_rows = list(csv.DictReader(f))

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_SIGNAL_CSV_FIELDS, restval="")
        writer.writeheader()
        writer.writerows(existing_rows)
        writer.writerow(_signal_csv_row(sig))
    return out_path
