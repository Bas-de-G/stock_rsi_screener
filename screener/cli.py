"""Command-line entry point.

    python -m screener.cli login       # once, to save a Morningstar session
    python -m screener.cli backfill    # once, to build RSI history
    python -m screener.cli run         # every day
    python -m screener.cli report      # what things look like right now
    python -m screener.cli signals     # every pattern found so far
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path

from .config import DEFAULT_HORIZON, DEFAULT_HORIZONS, Config, load_config
from .fairvalues import FairValueError, load_fair_values, save_fair_value
from .morningstar import (
    AuthenticationError,
    BotChallengeError,
    MorningstarError,
    save_login_session,
    scrape_ticker,
)
from .notify import format_signal, send_webhook
from .rsi import wilder_rsi_series
from .signals import (
    earnings_growth_passes,
    find_cross_pairs,
    is_strong,
    signal_fires,
    valuation_passes,
)
from .storage import (
    RsiPoint,
    Signal,
    Store,
    Valuation,
    append_signal_csv,
    export_csv_snapshot,
)
from .tradingview import MarketDataError, fetch_daily_closes, fetch_live_rsi


def _load_dotenv() -> None:
    """Read .env without adding a dependency. Never overrides real env vars."""
    import os

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ---------------------------------------------------------------- commands


def cmd_login(config: Config, args) -> int:
    try:
        path = save_login_session(config, timeout_minutes=args.timeout)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}")
        return 1
    print(f"Session saved to {path}")
    print("This file is gitignored. It grants access to the account — don't commit or share it.")
    return 0


def cmd_backfill(config: Config, args) -> int:
    """Seed RSI history from daily closes so signals work immediately.

    Without this, the tool would need to observe 14+ days of live readings
    before it could ever recognise the pattern.

    Skips a ticker that already has a full chart's worth of history (unless
    --force), which is what makes this cheap and safe to call unconditionally
    on every scheduled run. That matters because CI used to only ever backfill
    once, guarded by "does the database file exist at all" — so a ticker added
    to config.yaml after that first run (SanDisk, say) would never get
    backfilled by CI and would trickle in one live row a day instead. Making
    this idempotent and calling it every run lets a new ticker backfill itself.
    """
    horizons = _selected_horizons(config, args)
    with Store(config.storage.database) as store:
        for horizon in horizons:
            print(f"\n[{horizon.key}] {horizon.label} bars")
            for ticker in config.tickers:
                existing = store.rsi_series(ticker.symbol, horizon.key)
                # Intraday bars go stale between runs in a way daily ones don't:
                # a full 1h history collected yesterday is missing every bar
                # since. Always refetch those; the upsert dedupes.
                seeded = len(existing) >= config.dashboard.chart_days
                if seeded and not horizon.intraday and not args.force:
                    print(f"  {ticker.symbol}: {len(existing)} bars already — skip (--force to refetch)")
                    continue

                # Each horizon has its own sensible depth (5y of weekly bars,
                # but only 730d of hourly -- Yahoo's intraday ceiling).
                rng = args.range or horizon.yahoo_range
                try:
                    closes = fetch_daily_closes(
                        ticker.yahoo, range_=rng, interval=horizon.yahoo_interval
                    )
                except MarketDataError as exc:
                    print(f"  {ticker.symbol}: {exc}")
                    continue

                if len(closes) < config.rsi.period + 1:
                    print(f"  {ticker.symbol}: only {len(closes)} bars, need {config.rsi.period + 1}")
                    continue

                dates = [d for d, _ in closes]
                prices = [c for _, c in closes]
                rsis = wilder_rsi_series(prices, config.rsi.period)

                written = 0
                for date, close, rsi in zip(dates, prices, rsis):
                    if rsi is None:
                        continue
                    store.upsert_rsi_point(
                        RsiPoint(ticker.symbol, date, close, rsi, "backfill:yahoo",
                                 horizon=horizon.key)
                    )
                    written += 1
                print(f"  {ticker.symbol}: {written} bars ({dates[0]} → {dates[-1]})")

        _detect_and_record(store, config, announce=False, horizons=horizons)
    return 0


def _selected_horizons(config: Config, args) -> list:
    """Which horizons a command should act on: --horizon, else all of them."""
    key = getattr(args, "horizon", None)
    return [config.horizon(key)] if key else list(config.horizons)


def cmd_run(config: Config, args) -> int:
    """The daily job: fetch, store, detect, notify."""
    today = args.date or dt.date.today().isoformat()
    print(f"Run for {today}")
    print(f"Valuation gate: {config.signal.describe_rule()}")
    print(f"Window: two upward RSI crosses of {config.rsi.threshold:g} "
          f"within {config.signal.window_days} {config.signal.window_unit} days\n")

    exit_code = 0
    horizons = _selected_horizons(config, args)
    with Store(config.storage.database) as store:
        recorded = sync_fair_values(store, config)
        sync_earnings_growth(store, config)
        if recorded:
            print(f"  {recorded} hand-checked fair value(s) loaded "
                  f"from {config.storage.fair_values.name}\n")
        # --- RSI, from TradingView -------------------------------------
        # One live reading per (ticker, horizon). The daily bar is keyed by
        # date; intraday bars are keyed by the minute the run happened, so a
        # run every hour lays down a genuine intraday series rather than
        # overwriting a single row all day.
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
        for horizon in horizons:
            print(f"\n[{horizon.key}] {horizon.label} bars")
            for ticker in config.tickers:
                try:
                    quote = fetch_live_rsi(
                        ticker.tradingview,
                        period=config.rsi.period,
                        interval=horizon.tv_interval,
                    )
                except MarketDataError as exc:
                    print(f"  {ticker.symbol}: RSI unavailable — {exc}")
                    exit_code = 1
                    continue
                store.upsert_rsi_point(
                    RsiPoint(
                        ticker.symbol,
                        stamp if horizon.intraday else today,
                        quote.close, quote.rsi, "live:tradingview",
                        quote.earnings_growth, quote.earnings_growth_period,
                        horizon=horizon.key,
                    )
                )
                growth_note = (
                    f"   EPS growth ({quote.earnings_growth_period}) {quote.earnings_growth:+.1f}%"
                    if quote.earnings_growth is not None
                    else ""
                )
                print(f"  {ticker.symbol}: RSI {quote.rsi:6.2f}   close {quote.close:,.2f}{growth_note}")

        # --- Price + fair value, from Morningstar (opt-in) ---------------
        if not args.with_morningstar:
            print("\n  (RSI only — record fair values with: python -m screener.cli scrape)")
        else:
            # Routed through the same YAML file the `scrape` command writes, so
            # there is exactly one way a fair value enters the system. Writing
            # straight to the database would strand these values: it's
            # gitignored locally and rebuilt from scratch by CI.
            print()
            from .morningstar import scrape_many

            scraped = 0
            try:
                for ticker, result, error in scrape_many(
                    list(config.tickers), config.morningstar,
                    reference_prices=_reference_prices(store, list(config.tickers)),
                ):
                    if error is not None:
                        print(f"  {ticker.symbol}: {error}")
                        exit_code = 1
                        continue
                    # Same guard as `cmd_scrape` -- see the comment there.
                    if not result.complete:
                        print(
                            f"  {ticker.symbol}: page loaded but nothing usable came out "
                            f"(price={result.price}, fair_value={result.fair_value})"
                        )
                        exit_code = 1
                        continue
                    save_fair_value(
                        config.storage.fair_values,
                        ticker.symbol,
                        result.fair_value,
                        checked=today,
                        source="scraped",
                    )
                    scraped += 1
                    gap = (result.price / result.fair_value - 1) * 100
                    print(
                        f"  {ticker.symbol}: price {result.price:,.2f}  "
                        f"fair value {result.fair_value:,.2f}  ({gap:+.1f}% vs FV)"
                    )
            except MorningstarError as exc:
                print(f"  {exc}")
                exit_code = 1
            if scraped:
                sync_fair_values(store, config, quiet=True)
                print(f"\n  {scraped} written to {config.storage.fair_values.name} — commit it to share.")

        # --- Detect -----------------------------------------------------
        print()
        _detect_and_record(store, config, announce=True, horizons=horizons)

        snapshot = export_csv_snapshot(store, config.storage.csv_dir)
        print(f"\nSnapshot written to {snapshot}")
    return exit_code


def _latest_earnings_growth(series) -> float | None:
    """Most recent non-null earnings growth in a series.

    Scans backwards rather than reading `series[-1]`: only live-fetched bars
    carry the figure, so the newest bar is frequently a backfilled one with
    nothing on it.
    """
    for point in reversed(series):
        if point.earnings_growth is not None:
            return point.earnings_growth
    return None


def sync_earnings_growth(store: Store, config: Config) -> int:
    """Re-score every recorded signal against the current earnings growth.

    The counterpart to `sync_fair_values`, and for the same reason: both
    grading factors describe the company as it is now, so both have to be
    refreshed on every run rather than frozen at the moment a pattern
    completed.
    """
    applied = 0
    for ticker in config.tickers:
        for horizon in config.horizons:
            series = store.rsi_series(ticker.symbol, horizon.key)
            if not series:
                continue
            growth = _latest_earnings_growth(series)
            known, confirms = earnings_growth_passes(growth)
            store.update_signal_earnings_growth(
                ticker.symbol, horizon.key, growth, known, confirms
            )
            if known:
                applied += 1
    return applied


def _detect_and_record(
    store: Store, config: Config, announce: bool, horizons=None
) -> list[Signal]:
    """Scan stored history for the pattern and record anything new.

    Runs per horizon: each has its own bar series, its own cross-spacing
    window, and its own valuation margin, so the same symbol can legitimately
    have a fired 1h signal and no 1w signal at the same moment.
    """
    new_signals: list[Signal] = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    horizons = horizons if horizons is not None else list(config.horizons)

    for horizon in horizons:
        window = replace(config.signal, window_days=horizon.window_days)
        for symbol in store.symbols():
            series = store.rsi_series(symbol, horizon.key)
            if not series:
                continue
            for pair in find_cross_pairs(series, config.rsi.threshold, window):
                if store.signal_exists(symbol, pair.up2_date, horizon.key):
                    continue

                valuation = store.valuation(symbol, pair.up2_date)
                price = valuation.price if valuation else None
                fair_value = valuation.fair_value if valuation else None
                known, confirms = valuation_passes(
                    price, fair_value, config.signal, horizon.margin
                )

                # The *current* growth, not the value on the pattern's own
                # bar. See `Store.update_signal_earnings_growth` for why.
                growth = _latest_earnings_growth(series)
                eg_known, eg_confirms = earnings_growth_passes(growth)

                signal = Signal(
                    symbol=symbol,
                    up1_date=pair.up1_date,
                    down_date=pair.down_date,
                    up2_date=pair.up2_date,
                    price=price,
                    fair_value=fair_value,
                    valuation_known=known,
                    valuation_pass=confirms,
                    fired=signal_fires(confirms, config.signal),
                    recorded_at=now,
                    earnings_growth=growth,
                    earnings_growth_known=eg_known,
                    earnings_growth_pass=eg_confirms,
                    horizon=horizon.key,
                )
                store.record_signal(signal)
                append_signal_csv(config.storage.csv_dir, signal)
                new_signals.append(signal)

    if announce:
        fired = [s for s in new_signals if s.fired]
        if fired:
            for signal in fired:
                horizon = config.horizon(signal.horizon)
                message = format_signal(signal, config.signal.describe_rule(), horizon)
                print(message)
                if send_webhook(message):
                    print("  (sent to webhook)")
        else:
            near_misses = [s for s in new_signals if not s.fired]
            print("No buy signals today.")
            for signal in near_misses:
                reason = (
                    "no Morningstar valuation for that day"
                    if not signal.valuation_known
                    else "valuation gate not satisfied"
                )
                print(
                    f"  {signal.symbol} [{signal.horizon}]: RSI pattern completed "
                    f"{signal.up2_date}, but not fired — {reason}."
                )
    return new_signals


def cmd_fair_value(config: Config, args) -> int:
    """Record a fair value read by hand off the Morningstar page.

    This is the v1 path: rather than scraping, you click through from the
    dashboard, read the number, and record it here. The price is taken from
    the latest stored close so the valuation gate has both sides to compare.
    """
    symbol = args.symbol.upper()
    try:
        config.ticker(symbol)
    except KeyError as exc:
        print(f"{exc}")
        return 1

    date = args.date or dt.date.today().isoformat()

    with Store(config.storage.database) as store:
        series = store.rsi_series(symbol, DEFAULT_HORIZON)
        if not series:
            print(f"No price history for {symbol} yet — run `backfill` or `run` first.")
            return 1

        # YAML first: that file is the committed, shareable record. The
        # database is derived from it and rebuilt freely.
        save_fair_value(
            config.storage.fair_values, symbol, args.value, checked=date, note=args.note
        )
        count = sync_fair_values(store, config, quiet=True)

        price = series[-1].close
        daily = config.horizon(DEFAULT_HORIZON)
        known, confirms = valuation_passes(price, args.value, config.signal, daily.margin)
        eg_known, eg_confirms = earnings_growth_passes(series[-1].earnings_growth)
        if is_strong((known, confirms), (eg_known, eg_confirms)):
            verdict = "STRONG BUY — every known factor confirms"
        elif confirms and eg_known and not eg_confirms:
            verdict = "valuation confirms, but earnings are shrinking — not a strong buy"
        else:
            verdict = "buy signal stands, but it's trading above fair value"
        print(f"{symbol}: fair value {args.value:,.2f} vs close {price:,.2f}")
        print(f"  {verdict}  (1d gate needs {daily.margin_pct} headroom)")
        print(f"  ({config.signal.describe_rule()})")
        print(f"  saved to {config.storage.fair_values.name} ({count} recorded in total)")

        updated = _apply_valuation_to_pending_signals(store, config, symbol, price, args.value)
        if updated:
            plural = "pattern" if updated == 1 else "patterns"
            print(f"  applied to {updated} pending {plural} for {symbol}")
        print("  commit that file and the published dashboard will pick it up.")
    return 0


def sync_fair_values(store: Store, config: Config, quiet: bool = False) -> int:
    """Fold the YAML file into the database so the gate can be applied.

    Runs before every `run` and `dashboard`, so a value edited by hand on
    GitHub takes effect without anyone touching the database.
    """
    try:
        values = load_fair_values(config.storage.fair_values)
    except FairValueError as exc:
        print(f"  ! {exc}")
        return 0

    # The YAML file is authoritative: anything it no longer lists must not
    # linger in the database showing a fair value that isn't in the file.
    for stale in set(store.manual_valuation_symbols()) - set(values):
        store.delete_manual_valuations(stale)
        store.clear_signal_valuation(stale, config.signal.fire_without_valuation)
        if not quiet:
            print(f"  {stale} removed from {config.storage.fair_values.name} — cleared")

    applied = 0
    for symbol, entry in values.items():
        # Daily close is the reference price for the gate regardless of which
        # horizon is being scored -- Morningstar's own comparison is daily.
        series = store.rsi_series(symbol, DEFAULT_HORIZON)
        if not series:
            if not quiet:
                print(f"  ! {symbol} has a fair value but no price history — skipped")
            continue
        latest = series[-1]
        store.upsert_valuation(
            Valuation(
                symbol=symbol,
                date=latest.date,
                price=latest.close,
                fair_value=entry.fair_value,
                fair_value_date=entry.checked,
                source="manual",
            )
        )
        # Re-score per horizon: the same fair value clears 1h's 10% margin
        # while failing 1w's 50%, so `confirms` genuinely differs by horizon.
        for horizon in config.horizons:
            known, confirms = valuation_passes(
                latest.close, entry.fair_value, config.signal, horizon.margin
            )
            fired = signal_fires(confirms, config.signal)
            for signal in store.all_signals(symbol, horizon.key):
                store.update_signal_valuation(
                    symbol, signal.up2_date, latest.close, entry.fair_value,
                    known, confirms, fired, horizon.key,
                )
        applied += 1
    return applied


def _apply_valuation_to_pending_signals(
    store: Store, config: Config, symbol: str, price: float, fair_value: float
) -> int:
    """Re-score every recorded pattern for `symbol` against one fair value."""
    updated = 0
    for horizon in config.horizons:
        known, confirms = valuation_passes(price, fair_value, config.signal, horizon.margin)
        fired = signal_fires(confirms, config.signal)
        for signal in store.all_signals(symbol, horizon.key):
            store.update_signal_valuation(
                symbol, signal.up2_date, price, fair_value,
                known, confirms, fired, horizon.key,
            )
            updated += 1
    return updated


def _signalled_symbols(store: Store, config: Config) -> list[str]:
    """Symbols whose fired signal is recent enough to still be on the dashboard.

    This is what makes scraping cheap. A fair value only changes anything when a
    pattern has fired — it's what upgrades a plain buy to a strong one. With
    `fire_without_valuation` set, a ticker sitting at RSI 60 with no pattern
    gains nothing from being scraped, so don't fetch 35 subscriber pages to
    answer a question about three of them.

    The chart-window rule is mirrored from `dashboard._collect`, so a signal
    that's aged off the dashboard doesn't drag a scrape along with it.
    """
    window = config.dashboard.chart_days
    out: list[str] = []
    for ticker in config.tickers:
        # A signal on any horizon is reason enough to check the fair value --
        # the same Morningstar number feeds all four gates.
        for horizon in config.horizons:
            series = store.rsi_series(ticker.symbol, horizon.key)
            if not series:
                continue
            chart_start = series[-window:][0].date
            if any(
                s.fired and s.up2_date >= chart_start
                for s in store.all_signals(ticker.symbol, horizon.key)
            ):
                out.append(ticker.symbol)
                break
    return out


# Morningstar analysts revise a fair value on earnings or a thesis change --
# roughly quarterly. Re-reading the same page days later almost always returns
# the number already on file, so it is wasted requests against a logged-in
# session on a paid product.
DEFAULT_MAX_FAIR_VALUE_AGE_DAYS = 14


def _fresh_fair_values(config: Config, max_age_days: int) -> dict[str, int]:
    """Symbols checked recently enough to skip, mapped to how many days ago.

    An entry with no `checked:` date counts as stale — it was hand-written
    without one, and guessing that it's current would be worse than re-reading.
    """
    try:
        values = load_fair_values(config.storage.fair_values)
    except FairValueError:
        return {}

    fresh: dict[str, int] = {}
    today = dt.date.today()
    for symbol, entry in values.items():
        if not entry.checked:
            continue
        try:
            age = (today - dt.date.fromisoformat(entry.checked)).days
        except ValueError:
            continue  # unparseable hand-typed date: treat as stale
        if 0 <= age < max_age_days:
            fresh[symbol] = age
    return fresh


def _resolve_scrape_targets(store: Store, config: Config, args) -> list:
    """Work out which tickers `scrape` should visit."""
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        tickers = []
        for symbol in wanted:
            try:
                tickers.append(config.ticker(symbol))
            except KeyError as exc:
                print(f"  ! {exc}")
        return tickers
    if args.all:
        return list(config.tickers)
    return [config.ticker(s) for s in _signalled_symbols(store, config)]


def _drop_recently_checked(config: Config, tickers: list, args) -> tuple[list, list]:
    """Split targets into (to scrape, skipped as still fresh).

    Kept separate from target *resolution* so that "you named a symbol that
    doesn't exist" and "that symbol was checked on Tuesday" stay distinguishable
    — the first is a typo worth a non-zero exit, the second is the feature
    working.
    """
    if getattr(args, "force", False):
        return tickers, []

    max_age = getattr(args, "max_age", None) or DEFAULT_MAX_FAIR_VALUE_AGE_DAYS
    fresh = _fresh_fair_values(config, max_age)
    kept, skipped = [], []
    for ticker in tickers:
        if ticker.symbol in fresh:
            skipped.append((ticker.symbol, fresh[ticker.symbol]))
        else:
            kept.append(ticker)
    return kept, skipped


def _reference_prices(store: Store, tickers: list) -> dict[str, float]:
    """Latest stored close per symbol, for the scraper to lean on.

    TradingView already gave us an authoritative price for every listing, in
    the listing's own currency. Handing it to the scraper means a Morningstar
    page whose own price we can't parse still produces a usable fair value,
    and lets the extractor reject a fair-value candidate that's the wrong
    order of magnitude.
    """
    out: dict[str, float] = {}
    for ticker in tickers:
        series = store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
        if series:
            out[ticker.symbol] = series[-1].close
    return out


def cmd_scrape(config: Config, args) -> int:
    """Read fair values off Morningstar and record them in the YAML file.

    Runs from a laptop, not CI: the fair value is subscriber-only, so this needs
    a logged-in session, and a session cookie is a credential that has no place
    in a public repo's secrets. Estimates only move on earnings or a thesis
    change anyway, so an occasional local run keeps up fine.
    """
    from .morningstar import scrape_many

    date = args.date or dt.date.today().isoformat()

    with Store(config.storage.database) as store:
        targets = _resolve_scrape_targets(store, config, args)

        targets, fresh_skipped = _drop_recently_checked(config, targets, args)
        if fresh_skipped:
            max_age = getattr(args, "max_age", None) or DEFAULT_MAX_FAIR_VALUE_AGE_DAYS
            listed = ", ".join(
                f"{s} (today)" if d == 0 else f"{s} ({d}d ago)" for s, d in fresh_skipped
            )
            print(f"Already checked within {max_age} days, skipping {len(fresh_skipped)}: {listed}")
            print("  (--force re-reads them, --max-age changes the window)")

        if not targets:
            if fresh_skipped:
                print("Nothing left to scrape — every candidate is still fresh.")
                return 0
            print("Nothing to scrape.")
            if args.symbols:
                # Explicitly asked for tickers and got none — a typo, not an
                # empty result. Exit non-zero so a script notices.
                return 1
            if not args.all:
                print("  No ticker has a live signal, so no fair value would change anything.")
                print("  Use --all to scrape every ticker anyway, or --symbols SYM,SYM.")
            return 0

        names = ", ".join(t.symbol for t in targets)
        print(f"Scraping {len(targets)} ticker(s): {names}")
        if args.dry_run:
            print("\n(--dry-run: nothing fetched, nothing written)")
            return 0
        print("  Pacing requests — this is a logged-in session on a paid product.\n")

        recorded, failed = 0, 0

        def report(ticker, result, error):
            nonlocal recorded, failed
            if error is not None:
                print(f"  {ticker.symbol}: {error}")
                failed += 1
                return
            # A page can load fine, not be a bot challenge and not look logged
            # out, yet still yield nothing usable -- an unfamiliar layout, or a
            # name Morningstar publishes no fair value for. Treat that as a
            # failure rather than trusting the half-filled result: writing a
            # fair value with no price to compare it against would put an
            # ungradeable entry in the YAML, and computing the gap would divide
            # by a None.
            if not result.complete:
                print(
                    f"  {ticker.symbol}: page loaded but nothing usable came out "
                    f"(price={result.price}, fair_value={result.fair_value}) — "
                    f"see debug/ for what was actually served"
                )
                failed += 1
                return
            save_fair_value(
                config.storage.fair_values,
                ticker.symbol,
                result.fair_value,
                checked=date,
                note=args.note,
                source="scraped",
            )
            gap = (result.price / result.fair_value - 1) * 100
            print(
                f"  {ticker.symbol}: price {result.price:,.2f}  "
                f"fair value {result.fair_value:,.2f}  ({gap:+.1f}% vs FV)  "
                f"[{result.method or 'text'}]"
            )
            recorded += 1

        try:
            scrape_many(
                targets, config.morningstar, on_result=report,
                reference_prices=_reference_prices(store, targets),
            )
        except AuthenticationError as exc:
            print(f"\n  {exc}")
            return 1
        except BotChallengeError as exc:
            print(f"\n  {exc}")
            if recorded:
                print(f"  ({recorded} value(s) before the challenge were still recorded.)")
            return 1
        except MorningstarError as exc:
            print(f"\n  Scrape failed: {exc}")
            return 1

        if recorded:
            sync_fair_values(store, config, quiet=True)

    print(f"\n{recorded} recorded, {failed} failed → {config.storage.fair_values.name}")
    if recorded:
        if args.push:
            return _commit_and_push_fair_values(config, recorded)
        print("  Not pushed. Review the diff, then either:")
        print(f"    git add {config.storage.fair_values.name} && git commit && git push")
        print("    or re-run with --push to do that automatically.")
    return 1 if failed and not recorded else 0


def _commit_and_push_fair_values(config: Config, recorded: int) -> int:
    """Commit just the fair-value file and push it.

    Deliberately opt-in (`--push`), and deliberately narrow: it stages one file
    by name. A scrape run shouldn't sweep up whatever else happens to be dirty
    in the working tree.
    """
    import subprocess

    path = config.storage.fair_values
    repo = path.parent

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True
        )

    staged = git("add", str(path))
    if staged.returncode != 0:
        print(f"  ! git add failed: {staged.stderr.strip()}")
        return 1

    if git("diff", "--staged", "--quiet").returncode == 0:
        print("  Nothing changed — every scraped value matched what was already recorded.")
        return 0

    plural = "value" if recorded == 1 else "values"
    committed = git("commit", "-m", f"fair values: scraped {recorded} {plural}")
    if committed.returncode != 0:
        print(f"  ! git commit failed: {committed.stderr.strip() or committed.stdout.strip()}")
        return 1

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    pushed = git("push", "-u", "origin", branch)
    if pushed.returncode != 0:
        print(f"  ! git push failed: {pushed.stderr.strip()}")
        print("    The commit is made — push it by hand once that's sorted.")
        return 1

    print(f"  Committed and pushed to {branch}.")
    print("  The next scheduled run rebuilds the dashboard with these values.")
    return 0


def cmd_dashboard(config: Config, args) -> int:
    from .dashboard import build_all_dashboards, build_dashboard

    output = Path(args.output) if args.output else config.dashboard.output
    with Store(config.storage.database) as store:
        sync_fair_values(store, config, quiet=True)
        sync_earnings_growth(store, config)
        if args.horizon:
            paths = [build_dashboard(store, config, output, horizon=args.horizon)]
        else:
            paths = build_all_dashboards(store, config, output)

    for path in paths:
        print(f"Dashboard written to {path}")
    if args.open:
        import webbrowser

        webbrowser.open(paths[0].as_uri())
    return 0


def cmd_signals(config: Config, args) -> int:
    with Store(config.storage.database) as store:
        signals = store.all_signals(args.symbol, getattr(args, 'horizon', None))
        if not signals:
            print("No RSI patterns recorded yet.")
            return 0
        print(
            f"{'SYMBOL':<8}{'TF':<5}{'CROSS 1':<12}{'DIP':<12}{'CROSS 2':<12}"
            f"{'PRICE':>10}{'FAIR VAL':>10}{'EPS GROWTH':>12}  RESULT"
        )
        for s in signals:
            price = f"{s.price:,.2f}" if s.price is not None else "-"
            fair = f"{s.fair_value:,.2f}" if s.fair_value is not None else "-"
            growth = f"{s.earnings_growth:+.1f}%" if s.earnings_growth is not None else "-"
            strong = is_strong(
                (s.valuation_known, s.valuation_pass),
                (s.earnings_growth_known, s.earnings_growth_pass),
            )
            if s.fired and strong:
                verdict = "STRONG BUY (all known factors confirm)"
            elif s.fired and s.valuation_pass and s.earnings_growth_known and not s.earnings_growth_pass:
                verdict = "BUY SIGNAL (valuation confirms, earnings shrinking)"
            elif s.fired and s.valuation_known:
                verdict = "BUY SIGNAL (above fair value)"
            elif s.fired:
                verdict = "BUY SIGNAL (fair value unchecked)"
            else:
                verdict = "pattern only (valuation gate failed)"
            print(
                f"{s.symbol:<8}{s.horizon:<5}{s.up1_date:<12}{s.down_date:<12}{s.up2_date:<12}"
                f"{price:>10}{fair:>10}{growth:>12}  {verdict}"
            )
    return 0


def cmd_report(config: Config, args) -> int:
    with Store(config.storage.database) as store:
        valuations = {v.symbol: v for v in store.latest_valuations()}
        print(f"Valuation gate: {config.signal.describe_rule()}\n")
        print(
            f"{'SYMBOL':<8}{'DATE':<12}{'RSI':>8}{'PRICE':>11}{'FAIR VAL':>11}"
            f"{'EPS GROWTH':>12}  GATE"
        )
        for symbol in store.symbols():
            series = store.rsi_series(symbol, DEFAULT_HORIZON)
            last = series[-1] if series else None
            val = valuations.get(symbol)
            rsi = f"{last.rsi:.2f}" if last else "-"
            date = last.date if last else "-"
            price = f"{val.price:,.2f}" if val else "-"
            fair = f"{val.fair_value:,.2f}" if val else "-"
            growth = (
                f"{last.earnings_growth:+.1f}%"
                if last and last.earnings_growth is not None
                else "-"
            )
            if val:
                _, passed = valuation_passes(
                    val.price, val.fair_value, config.signal,
                    config.horizon(DEFAULT_HORIZON).margin,
                )
                gate = "pass" if passed else "fail"
            else:
                gate = "unknown"
            print(f"{symbol:<8}{date:<12}{rsi:>8}{price:>11}{fair:>11}{growth:>12}  {gate}")
            if series:
                print(f"        history: {len(series)} days, {series[0].date} → {series[-1].date}")
    return 0


def cmd_check_auth(config: Config, args) -> int:
    ticker = config.tickers[0]
    print(f"Testing the saved Morningstar session against {ticker.symbol}...")
    try:
        result = scrape_ticker(ticker, config.morningstar)
    except AuthenticationError as exc:
        print(f"  NOT SIGNED IN: {exc}")
        return 1
    except BotChallengeError as exc:
        print(f"  BLOCKED: {exc}")
        return 1
    except MorningstarError as exc:
        print(f"  Failed: {exc}")
        return 1

    if result.complete:
        print(f"  OK — price {result.price:,.2f}, fair value {result.fair_value:,.2f} (via {result.method})")
        return 0
    print(f"  Partial read: price={result.price}, fair_value={result.fair_value}")
    print("  A fair value of None usually means the session isn't a subscriber session.")
    print("  Check debug/ for the page that was actually served.")
    return 1


# ---------------------------------------------------------------- plumbing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screener",
        description="Daily RSI + Morningstar fair-value buy-signal screener.",
    )
    parser.add_argument("--config", help="path to config.yaml", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="sign in to Morningstar and save the session")
    p_login.add_argument("--timeout", type=int, default=10, help="minutes to wait for sign-in")
    p_login.set_defaults(func=cmd_login)

    p_backfill = sub.add_parser("backfill", help="seed RSI history from daily closes")
    p_backfill.add_argument(
        "--range", default=None,
        help="override history range (e.g. 6mo, 2y); default is per-horizon",
    )
    p_backfill.add_argument(
        "--force", action="store_true",
        help="refetch even tickers that already have a full history",
    )
    p_backfill.set_defaults(func=cmd_backfill)

    p_run = sub.add_parser("run", help="the daily job (RSI only by default)")
    p_run.add_argument(
        "--with-morningstar",
        action="store_true",
        help="also scrape price + fair value (v2; needs `login` first)",
    )
    p_run.add_argument("--date", help="override the run date (ISO), for testing")
    p_run.set_defaults(func=cmd_run)

    p_fv = sub.add_parser("fair-value", help="record a fair value you checked by hand")
    p_fv.add_argument("symbol", help="ticker, e.g. NVDA")
    p_fv.add_argument("value", type=float, help="Morningstar fair value estimate")
    p_fv.add_argument("--date", help="date to record it against (default: today)")
    p_fv.add_argument("--note", help="optional note, e.g. 'post-earnings cut'")
    p_fv.set_defaults(func=cmd_fair_value)

    p_scrape = sub.add_parser(
        "scrape", help="read fair values off Morningstar (needs `login` first)"
    )
    p_scrape.add_argument(
        "--all", action="store_true", help="every ticker, not just ones with a live signal"
    )
    p_scrape.add_argument("--symbols", help="explicit comma-separated list, e.g. IBM,NVDA")
    p_scrape.add_argument(
        "--dry-run", action="store_true", help="list what would be scraped, fetch nothing"
    )
    p_scrape.add_argument(
        "--push", action="store_true", help="commit and push fair_values.yaml when done"
    )
    p_scrape.add_argument(
        "--force", action="store_true",
        help="re-read fair values even if they were checked recently",
    )
    p_scrape.add_argument(
        "--max-age", type=int, default=None, dest="max_age",
        help=f"days a fair value stays fresh (default: {DEFAULT_MAX_FAIR_VALUE_AGE_DAYS})",
    )
    p_scrape.add_argument("--date", help="date to record against (default: today)")
    p_scrape.add_argument("--note", help="optional note stored with every value in this run")
    p_scrape.set_defaults(func=cmd_scrape)

    p_dash = sub.add_parser("dashboard", help="build the shareable HTML dashboard")
    p_dash.add_argument("--output", help="override the output path")
    p_dash.add_argument("--open", action="store_true", help="open it in a browser when done")
    p_dash.set_defaults(func=cmd_dashboard)

    p_signals = sub.add_parser("signals", help="list recorded patterns and signals")
    p_signals.add_argument("--symbol", help="limit to one symbol")
    p_signals.set_defaults(func=cmd_signals)

    p_report = sub.add_parser("report", help="current state of each ticker")
    p_report.set_defaults(func=cmd_report)

    p_check = sub.add_parser("check-auth", help="verify the saved Morningstar session")
    p_check.set_defaults(func=cmd_check_auth)

    for sub_parser in (p_run, p_backfill, p_dash, p_signals):
        sub_parser.add_argument(
            "--horizon", choices=[h.key for h in DEFAULT_HORIZONS],
            help="limit to one RSI timeframe (default: all four)",
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}")
        return 2
    return args.func(config, args)


if __name__ == "__main__":
    sys.exit(main())
