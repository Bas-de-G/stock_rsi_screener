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
from collections import Counter
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
from .earnings import earnings_window
from .earnings import to_date as to_release_date
from .notified import COOLDOWN, Ledger, key_for
from .outcomes import FORWARD_BARS as OUTCOME_BARS
from .ruleone import FIELDS as RULE_ONE_FIELDS
from .notify import (
    format_signal,
    format_strong_buy,
    issue_title,
    send_github_issue,
    send_push,
    send_webhook,
)
from .rsi import wilder_rsi_series
from .signals import (
    BUY,
    SELL,
    earnings_growth_passes,
    find_cross_pairs,
    is_strong,
    signal_fires,
    signal_is_fresh,
    signal_is_live,
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
from .tradingview import (
    EARNINGS_FIELDS,
    EARNINGS_LAST_FIELD,
    EARNINGS_NEXT_FIELD,
    MarketDataError,
    NoHistoryYet,
    decode_quote,
    fetch_daily_closes,
    fetch_live_batch,
    fetch_live_rsi,
)


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


# How stale a horizon's Yahoo bars may get before they are pulled again. The
# batch scan writes live 1h and 4h rows every half hour in between, so a
# refresh is only topping up the true bar closes behind them -- and repairing
# whatever a missed run left as a gap, which is the reason to keep doing it at
# all. Measured against the newest bar rather than a stored timestamp, which
# makes it land roughly once a day per horizon without any bookkeeping.
INTRADAY_REFRESH_AFTER = dt.timedelta(hours=20)


def _refreshed_today(series) -> bool:
    """Whether Yahoo history for this horizon was pulled recently enough.

    Only backfilled rows count. The live rows in between come from the batch
    scan and say nothing about when Yahoo was last asked.
    """
    stamps = [p.date for p in series if p.source.startswith("backfill")]
    if not stamps:
        return False
    try:
        newest = dt.datetime.fromisoformat(max(stamps))
    except ValueError:
        return False
    return (dt.datetime.now() - newest) < INTRADAY_REFRESH_AFTER


def _new_tickers(store: Store, config: Config) -> list[str]:
    """Tickers with no history at all — the ones seeding is expensive for.

    Deliberately "nothing anywhere" rather than "short on this horizon". A
    ticker can be fully seeded daily and thin weekly simply by being a recent
    listing: BSP has 257 daily bars and not yet fifteen weekly ones. That is
    one cheap request that fills itself as bars accumulate, and capping it
    would defer it every run forever.
    """
    return [
        t.symbol for t in config.tickers
        if not store.rsi_series(t.symbol, DEFAULT_HORIZON)
    ]


def _new_ticker_budget(store: Store, config: Config, args) -> set[str]:
    """Which never-seen tickers get their history this run.

    Config order, so a batch added together arrives together rather than
    alphabetically interleaved. Returns every new ticker when uncapped.
    """
    new = _new_tickers(store, config)
    limit = getattr(args, "max_new", None)
    if not limit or limit >= len(new):
        if new:
            print(f"Seeding {len(new)} new ticker(s): {', '.join(new)}")
        return set(new)
    print(f"{len(new)} new ticker(s) to seed; taking {limit} this run, "
          f"{len(new) - limit} deferred.")
    return set(new[:limit])


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

    Two things keep it affordable as the watchlist grows, because unlike RSI
    this is one Yahoo request per ticker per horizon and cannot be batched:

    * Seeding a *new* ticker is capped per run (--max-new). Adding a hundred
      names then spreads over a few runs instead of timing one out.
    * Seeded intraday history is refreshed once a day rather than every run.
      It used to refetch on every run -- 153 x 2 = 306 requests, most of the
      job's wall time -- which was worth it when a run was the only thing
      writing intraday rows. It no longer is: the batch scan lays down live
      1h and 4h rows every half hour, and the daily refresh backfills the true
      bar closes behind them.
    """
    horizons = _selected_horizons(config, args)
    with Store(config.storage.database) as store:
        # Only ever holds back tickers we have never seen at all. A known
        # ticker that is thin on one horizon still refetches: that is a single
        # request, and it is how a recent listing's weekly history fills in.
        deferred = set(_new_tickers(store, config)) - _new_ticker_budget(store, config, args)
        for horizon in horizons:
            print(f"\n[{horizon.key}] {horizon.label} bars")
            for ticker in config.tickers:
                existing = store.rsi_series(ticker.symbol, horizon.key)
                seeded = len(existing) >= config.dashboard.chart_days
                if not args.force:
                    if ticker.symbol in deferred:
                        print(f"  {ticker.symbol}: new — deferred to a later run "
                              f"(--max-new is {args.max_new})")
                        continue
                    if seeded and not horizon.intraday:
                        print(f"  {ticker.symbol}: {len(existing)} bars already — skip (--force to refetch)")
                        continue
                    if seeded and _refreshed_today(existing):
                        print(f"  {ticker.symbol}: intraday history refreshed today — skip")
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

        # Every ticker, every horizon, in one request -- TradingView's scan
        # endpoint takes a list of symbols and a list of columns, and RSI|60,
        # RSI|240, RSI and RSI|1W are just four more columns. This used to be a
        # GET per (ticker, horizon): 153 x 4 = 612 sequential requests, which
        # was most of the run's wall time and the reason the watchlist could
        # not grow. Measured at 637 symbols x 4 horizons in 0.75 seconds.
        #
        # A failure here is a real outage and fails the run; a symbol simply
        # missing from the response is reported per ticker below, exactly like
        # a null field.
        try:
            rows = fetch_live_batch(
                [t.tradingview for t in config.tickers],
                [h.tv_interval for h in horizons],
                period=config.rsi.period,
                extra_fields=EARNINGS_FIELDS + RULE_ONE_FIELDS,
            )
        except MarketDataError as exc:
            print(f"\n  ! {exc}")
            return 1

        _record_earnings_dates(store, config, rows)
        _record_rule_one(store, config, rows, horizons)

        for horizon in horizons:
            print(f"\n[{horizon.key}] {horizon.label} bars")
            for ticker in config.tickers:
                try:
                    row = rows.get(ticker.tradingview)
                    if row is not None:
                        quote = decode_quote(
                            ticker.tradingview, row,
                            period=config.rsi.period,
                            interval=horizon.tv_interval,
                        )
                    else:
                        # The scan index is not quite the same set as the
                        # symbol endpoint, so a listing can resolve there and
                        # be absent here. Ask for it on its own rather than let
                        # it drop off the dashboard unremarked -- one extra
                        # request for a case that should never happen beats a
                        # ticker quietly going missing.
                        quote = fetch_live_rsi(
                            ticker.tradingview,
                            period=config.rsi.period,
                            interval=horizon.tv_interval,
                        )
                except NoHistoryYet as exc:
                    # Expected for a recent listing, and it fixes itself as the
                    # bars accumulate. Not a failure: letting it set exit_code
                    # meant one young ticker turned the whole scheduled run red
                    # and skipped the commit and publish steps behind it, so
                    # the other 129 tickers never reached the dashboard.
                    print(f"  {ticker.symbol}: no {horizon.label} RSI yet — {exc}")
                    continue
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


def _gate_for(signal_config, direction: str):
    """The valuation rule for one side of the trade.

    A sell is the mirror of a buy: what argues for buying is the price sitting
    below fair value, so what argues for selling is it sitting that far above.
    Flipping the configured rule keeps one setting meaningful for both.
    """
    if direction == BUY:
        return signal_config
    flipped = (
        "fair_value_below_price"
        if signal_config.valuation_rule == "price_below_fair_value"
        else "price_below_fair_value"
    )
    return replace(signal_config, valuation_rule=flipped)


def _growth_for(growth: float | None, direction: str) -> tuple[bool, bool]:
    """Earnings growth as a veto, oriented to the trade direction.

    Growing earnings argue against selling exactly as strongly as they argue
    for buying, so the sell side inverts.
    """
    known, confirms = earnings_growth_passes(growth)
    if known and direction == SELL:
        return known, not confirms
    return known, confirms


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

    Runs per horizon and per direction: each horizon has its own bar series,
    cross-spacing window and valuation margin, so the same symbol can
    legitimately have a fired 1h signal and no 1w signal at the same moment.

    Everything found is recorded. Whether a pattern is still a *tradeable*
    setup is a separate question, answered by `signals.signal_is_live` at
    display time — this table stays a complete historical log.
    """
    new_signals: list[Signal] = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    horizons = horizons if horizons is not None else list(config.horizons)

    # Buy off the oversold line, sell off the overbought one: the same shape,
    # mirrored, with a move back across the line in between.
    directions = ((BUY, config.rsi.threshold), (SELL, config.rsi.overbought))

    for horizon in horizons:
        window = replace(config.signal, window_days=horizon.window_days)
        for symbol in store.symbols():
            series = store.rsi_series(symbol, horizon.key)
            if not series:
                continue
            growth = _latest_earnings_growth(series)

            for direction, line in directions:
                gate = _gate_for(config.signal, direction)
                for pair in find_cross_pairs(series, line, window, direction):
                    if store.signal_exists(symbol, pair.up2_date, horizon.key, direction):
                        continue

                    valuation = store.valuation(symbol, pair.up2_date)
                    price = valuation.price if valuation else None
                    fair_value = valuation.fair_value if valuation else None
                    known, confirms = valuation_passes(
                        price, fair_value, gate, horizon.margin
                    )
                    eg_known, eg_confirms = _growth_for(growth, direction)

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
                        direction=direction,
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

        updated = _rescore_signals(store, config, symbol, price, args.value)
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
        _rescore_signals(store, config, symbol, latest.close, entry.fair_value)
        applied += 1
    return applied


def _record_earnings_dates(store: Store, config: Config, rows: dict) -> int:
    """Store when each company next reports, from the batch response.

    Free: the dates are two more columns on the request that already fetched
    RSI. A symbol the feed has no date for is recorded as such rather than
    skipped, so a date that disappears (the release happened, the next one is
    not scheduled) clears the old one instead of leaving it to go stale.
    """
    recorded = 0
    for ticker in config.tickers:
        row = rows.get(ticker.tradingview)
        if row is None:
            continue
        nxt = to_release_date(row.get(EARNINGS_NEXT_FIELD))
        last = to_release_date(row.get(EARNINGS_LAST_FIELD))
        store.upsert_earnings(
            ticker.symbol,
            nxt.isoformat() if nxt else None,
            _release_stamp(row.get(EARNINGS_NEXT_FIELD)),
            last.isoformat() if last else None,
        )
        recorded += 1
    return recorded


def _record_rule_one(store: Store, config: Config, rows: dict, horizons) -> int:
    """Compute and store each company's Rule #1 reading.

    Free, like the earnings dates: the six fundamentals it needs are more
    columns on the batch request that already fetched RSI.

    Priced off the live close from the batch response rather than a stored bar,
    so the sticker gap is against what the stock costs right now.
    """
    from .ruleone import from_scanner

    close_field = _close_field_for(horizons)
    recorded = 0
    for ticker in config.tickers:
        row = rows.get(ticker.tradingview)
        if row is None:
            continue
        store.upsert_rule_one(
            ticker.symbol, from_scanner(row, price=row.get(close_field))
        )
        recorded += 1
    return recorded


def _close_field_for(horizons) -> str:
    """The batch column carrying a usable current price.

    The scan is asked for a close per horizon, and they are the same number
    during a session. The daily one is preferred because it is the only one
    that survives a weekend.
    """
    from .tradingview import _close_field_name

    keys = [h.tv_interval for h in horizons]
    return _close_field_name("1D" if "1D" in keys else keys[0])


def _release_stamp(value) -> str | None:
    """The release timestamp in full, which says before-open vs after-close."""
    if not isinstance(value, (int, float)):
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="minutes")


def _rescore_signals(
    store: Store, config: Config, symbol: str, price: float, fair_value: float
) -> int:
    """Re-score every recorded pattern for `symbol` against one fair value.

    Loops horizon *and* direction, because the gate depends on both. The same
    fair value clears 1h's 10% margin while failing 1w's 50%, so `confirms`
    genuinely differs by horizon; and a sell is graded against the mirrored
    rule, so it differs by direction too. Scoring once with the buy rule and
    writing it everywhere left every sell permanently unvalued — and with
    `fire_without_valuation` set, that meant every sell pattern fired ungraded.
    """
    updated = 0
    for horizon in config.horizons:
        for direction in (BUY, SELL):
            known, confirms = valuation_passes(
                price, fair_value, _gate_for(config.signal, direction), horizon.margin
            )
            fired = signal_fires(confirms, config.signal)
            for signal in store.all_signals(symbol, horizon.key, direction):
                store.update_signal_valuation(
                    symbol, signal.up2_date, price, fair_value,
                    known, confirms, fired, horizon.key, direction,
                )
                updated += 1
    return updated


# How urgently a symbol wants a fair value. Lower sorts first, which is what a
# capped session spends its pages on.
_FRESH, _LIVE, _RECORDED = 0, 1, 2
_URGENCY_LABEL = {
    _FRESH: "just fired",
    _LIVE: "live signal",
    _RECORDED: "signal on file",
}


def _signalled_symbols(store: Store, config: Config) -> list[str]:
    """Symbols wanting a fair value, most actionable first.

    This is what makes scraping cheap. A fair value only changes anything when a
    pattern has fired — it's what upgrades a plain buy to a strong one. With
    `fire_without_valuation` set, a ticker sitting at RSI 60 with no pattern
    gains nothing from being scraped, so don't fetch 35 subscriber pages to
    answer a question about three of them.

    The *order* matters as much as the membership once `--limit` caps a
    session, and it is where this used to fall down. "Has a fired pattern in
    the chart window" barely filters at all when every pattern fires: measured
    on the live database it selected 131 of 153 tickers, while only 50 had a
    signal actually live on a dashboard page. Nothing was wrong with scraping
    the other 81 — a fair value is cached for a fortnight, so reading one early
    often pays off later — but they were fetched in no particular order, so a
    capped run could spend every page on stale candidates and never reach the
    rocket that fired this morning.

    So they are ranked rather than filtered, in three tiers:

    * a pattern that *just* completed — the ones that become a strong buy today
    * a pattern still live, so it is on the dashboard now
    * a pattern on file inside the chart window — worth caching ahead

    and within a tier, most recently completed first. Both directions count:
    the same Morningstar number grades a sell against the mirrored rule.
    """
    return [symbol for symbol, _, _ in _ranked_targets(store, config)]


def _ranked_targets(store: Store, config: Config) -> list[tuple[str, int, str]]:
    """(symbol, urgency, most recent completing cross), most urgent first."""
    window = config.dashboard.chart_days
    ranked: list[tuple[str, int, str]] = []

    for ticker in config.tickers:
        best: tuple[int, str] | None = None
        for horizon in config.horizons:
            series = store.rsi_series(ticker.symbol, horizon.key)
            if not series:
                continue
            chart_start = series[-window:][0].date
            # Mirrors dashboard._collect: liveness is judged against the
            # horizon's own lookback, not the global one.
            liveness = replace(config.signal, window_days=horizon.window_days)
            for signal in store.all_signals(ticker.symbol, horizon.key):
                if not (signal.fired and signal.up2_date >= chart_start):
                    continue
                threshold = (
                    config.rsi.threshold if signal.direction == BUY
                    else config.rsi.overbought
                )
                if signal_is_fresh(signal, series, horizon):
                    urgency = _FRESH
                elif signal_is_live(signal, series, liveness, threshold):
                    urgency = _LIVE
                else:
                    urgency = _RECORDED
                # The most urgent signal on any horizon speaks for the ticker;
                # within one tier, the one that completed most recently.
                if best is None or urgency < best[0] or (
                    urgency == best[0] and signal.up2_date > best[1]
                ):
                    best = (urgency, signal.up2_date)
        if best is not None:
            ranked.append((ticker.symbol, best[0], best[1]))

    # Two stable passes, least significant first — cheaper to read than one key
    # that has to invert a date to sort it descending. `ranked` starts in
    # config order and both sorts are stable, so anything the ranking cannot
    # separate stays in the order the watchlist lists it.
    ranked.sort(key=lambda row: row[2], reverse=True)      # latest cross first
    ranked.sort(key=lambda row: row[1])                    # urgency wins
    return ranked


# Morningstar analysts revise a fair value on earnings or a thesis change --
# roughly quarterly. Re-reading the same page days later almost always returns
# the number already on file, so it is wasted requests against a logged-in
# session on a paid product.
#
# 40 days, not the 14 this started at: 40 still re-reads every name at least
# twice between earnings, which is as often as the number can actually move,
# while cutting the pages a full sweep costs by roughly two thirds. Raising it
# past ~45 would risk a whole quarter passing unchecked.
DEFAULT_MAX_FAIR_VALUE_AGE_DAYS = 40


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
    """Work out which tickers `scrape` should visit, most urgent first.

    Named symbols keep the order they were typed in — that is an explicit
    instruction, not a suggestion. Everything else comes back ranked, so
    `--limit` cuts from the bottom rather than from wherever the config file
    happened to list things.
    """
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        tickers = []
        for symbol in wanted:
            try:
                tickers.append(config.ticker(symbol))
            except KeyError as exc:
                print(f"  ! {exc}")
        return tickers

    ranked = [config.ticker(s) for s in _signalled_symbols(store, config)]
    if not args.all:
        return ranked
    # --all still leads with the tickers a fair value would change something
    # for; the rest follow in config order.
    seen = {t.symbol for t in ranked}
    return ranked + [t for t in config.tickers if t.symbol not in seen]


def _cap_targets(tickers: list, args) -> tuple[list, list]:
    """Split into (this session's pages, deferred to the next run).

    Applied *after* the freshness filter so the budget counts pages actually
    fetched, not candidates considered.
    """
    limit = getattr(args, "limit", None)
    if not limit or limit >= len(tickers):
        return tickers, []
    return tickers[:limit], tickers[limit:]


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


def cmd_universe(config: Config, args) -> int:
    """Propose tickers to add to the watchlist.

    Prints config.yaml lines, ready to paste or to append with --write. It
    never removes anything: the watchlist only grows, and a name that has since
    left an index keeps its card and its history.
    """
    from .tradingview import discover_market
    from .universe import as_yaml_line, parse_candidates, select

    wanted_indexes = tuple(
        i.strip() for i in (args.indexes or "").split(",") if i.strip()
    )
    existing = {t.symbol.upper() for t in config.tickers}

    try:
        rows = discover_market(
            args.market,
            min_market_cap=args.min_cap,
            min_volume=args.min_volume,
            limit=args.scan_limit,
        )
    except MarketDataError as exc:
        print(f"  ! {exc}")
        return 1

    # What the watchlist already covers, by company rather than by symbol. One
    # extra request, and it is what stops GOOG being proposed as a new company
    # when GOOGL is already tracked.
    try:
        tracked = fetch_live_batch(
            [t.tradingview for t in config.tickers], [],
            extra_fields=("description",),
        )
        tracked_companies = {row.get("description") for row in tracked.values()}
    except MarketDataError as exc:
        print(f"  ! could not read the watchlist's company names ({exc}) — "
              f"a second share class of something you already track may slip through")
        tracked_companies = set()

    candidates = parse_candidates(rows)
    proposed = select(
        candidates,
        market=args.market,
        indexes=wanted_indexes,
        min_volume=args.min_volume,
        exclude=existing,
        exclude_companies=tracked_companies,
    )
    if args.limit:
        proposed = proposed[:args.limit]

    print(f"{args.market}: {len(rows)} listings scanned, "
          f"{len(existing)} already on the watchlist, {len(proposed)} proposed")
    if wanted_indexes:
        print(f"  restricted to: {', '.join(wanted_indexes)}")
    if not proposed:
        print("\nNothing new to add.")
        return 0

    missing_slug = [c.symbol for c in proposed if not c.morningstar]
    print()
    for candidate in proposed:
        print(as_yaml_line(candidate))

    if missing_slug:
        print(f"\n  ! no Morningstar slug for {', '.join(missing_slug)} — "
              f"fill in the TODO by hand before scraping those.")

    if not args.write:
        print(f"\n({len(proposed)} lines above. --write appends them to "
              f"{config_path_hint()}; review the diff before committing.)")
        return 0

    _append_tickers(proposed)
    print(f"\nAppended {len(proposed)} ticker(s). Next:")
    print("  git diff config.yaml")
    print("  python -m screener.cli backfill      # seeds their history")
    return 0


def config_path_hint() -> str:
    from .config import DEFAULT_CONFIG

    return DEFAULT_CONFIG.name


def _append_tickers(candidates) -> None:
    """Insert new entries at the end of the `tickers:` block.

    Text insertion rather than a YAML round-trip on purpose: config.yaml is
    heavily commented -- sector headings, notes on individual names, the
    reasoning behind the horizons -- and dumping it back through PyYAML would
    erase all of it.
    """
    from .config import DEFAULT_CONFIG
    from .universe import as_yaml_line

    lines = DEFAULT_CONFIG.read_text().splitlines()
    last = None
    in_tickers = False
    for i, line in enumerate(lines):
        if line.startswith("tickers:"):
            in_tickers = True
            continue
        if in_tickers:
            if line.startswith("  - {"):
                last = i
            elif line and not line[0].isspace():
                break
    if last is None:
        raise ValueError("could not find the tickers: block in config.yaml")

    block = ["", "  # --- Added by `screener universe` ---"]
    block += [as_yaml_line(c) for c in candidates]
    lines[last + 1:last + 1] = block
    DEFAULT_CONFIG.write_text("\n".join(lines) + "\n")


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

        targets, deferred = _cap_targets(targets, args)
        if deferred:
            print(f"Capped at {len(targets)} this session; "
                  f"{len(deferred)} deferred to the next run.")
            print(f"  Next up: {', '.join(t.symbol for t in deferred[:8])}"
                  f"{' …' if len(deferred) > 8 else ''}")

        names = ", ".join(t.symbol for t in targets)
        print(f"Scraping {len(targets)} ticker(s): {names}")
        if not args.symbols:
            urgency = {s: u for s, u, _ in _ranked_targets(store, config)}
            tally = Counter(urgency[t.symbol] for t in targets if t.symbol in urgency)
            if tally:
                print("  " + ", ".join(
                    f"{count} {_URGENCY_LABEL[level]}"
                    for level, count in sorted(tally.items())
                ))
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


def _notify_new_strong_buys(store: Store, config: Config) -> int:
    """Post newly fired strong buys to the webhook, once each.

    Deliberately narrow, on two axes.

    *Strong*, not merely fired. Every pattern fires -- `fire_without_valuation`
    is set -- so announcing fired signals means 5 to 20 messages a run and 251
    the day new tickers are backfilled. The rocket is the rare one: 13% of
    recorded patterns, and only a handful live at a time.

    *Fresh*, not merely live. A strong buy stays actionable for a fortnight on
    the daily chart, and re-announcing it for two weeks would be its own kind
    of noise. Only a pattern that just completed is worth interrupting someone
    for.

    Two dedupe rules, because one was not enough. Once per *pattern*, ever --
    the ledger is a small committed file rather than a table in the database,
    because the database is only committed by the last run of the day. And
    once per *symbol per horizon* per trading session, because intraday
    patterns complete often enough that eleven distinct ones can be genuinely
    new and still be one piece of news. See `screener.notified`.
    """
    from .dashboard import _collect

    ledger = Ledger(config.storage.notifications)
    sent = 0
    for horizon in config.horizons:
        for row in _collect(store, config, horizon):
            # A signal the page itself badges as un-actionable must not also
            # ring someone's phone -- that is the one contradiction the warning
            # could not survive.
            if row.suspended:
                continue
            fresh = [
                s for s in row.buys
                if s.fired
                and signal_is_fresh(s, row.series, horizon)
                and is_strong(
                    (s.valuation_known, s.valuation_pass),
                    (s.earnings_growth_known, s.earnings_growth_pass),
                )
            ]
            if not fresh:
                continue
            up2 = max(s.up2_date for s in fresh)
            key = key_for("strong", horizon.key, row.symbol, up2)
            if ledger.seen(key):
                continue
            # A new pattern on a name announced hours ago is a better entry on
            # the same idea, not a new idea. Deliberately *not* recorded in the
            # ledger: recording it would restart the clock on every run and the
            # quiet period would never end. Not recording is safe because the
            # freshness window on the intraday charts is measured in hours, so
            # a pattern held back today has aged out by the time the cooldown
            # lifts.
            last = ledger.last_sent("strong", horizon.key, row.symbol)
            if last is not None and (elapsed := dt.datetime.now() - last) < COOLDOWN:
                hours = elapsed.total_seconds() / 3600
                print(f"  {row.symbol} [{horizon.key}] — new pattern, but announced "
                      f"{hours:.0f}h ago; holding off")
                continue
            best = max(fresh, key=lambda s: s.up2_date)
            discount = (
                (best.fair_value - best.price) / best.price
                if best.price and best.fair_value else None
            )
            page = "index.html" if horizon.key == DEFAULT_HORIZON else f"{horizon.key}.html"
            url = f"{config.dashboard.site_url}/{page}" if config.dashboard.site_url else page
            message = format_strong_buy(
                row.symbol, discount, best.price, best.fair_value,
                row.currency, horizon, config.rsi.threshold, url,
            )
            print(message)
            # Both transports are optional and independent: a laptop run has
            # neither and simply prints, CI has the token and may have the
            # webhook too.
            title = issue_title(row.symbol, discount, horizon)
            # The phone is the only channel with a timeframe filter, because it
            # is the only one that interrupts. The issue and the webhook still
            # carry every horizon, so nothing is lost -- it just waits to be
            # read. See `NotifyConfig`.
            if config.notify.pushes(horizon.key):
                if send_push(title, message, url):
                    print("  (pushed to phones)")
            else:
                print(f"  (no push — {horizon.label} is not a push timeframe)")
            if send_webhook(message):
                print("  (sent to webhook)")
            if send_github_issue(title, message, key):
                print("  (opened an issue — GitHub will email it)")
            ledger.record(key)
            sent += 1
    return sent


def _journal_recommendations(store: Store, config: Config) -> int:
    """Record every live verdict the dashboard just published, once each.

    Every fired signal on every horizon, in both directions, including the ones
    suspended for earnings and the ones nobody was notified about. The ledger
    is the sample that "does this work?" gets answered from, and a sample that
    only contains the picks we liked answers a different question.
    """
    from .dashboard import _collect
    from .journal import Journal, recommendation_from

    journal = Journal(config.storage.recommendations)
    for horizon in config.horizons:
        for row in _collect(store, config, horizon):
            for signal in row.signals:
                if not signal.fired:
                    continue
                journal.record(recommendation_from(row, signal, horizon))
    return journal.added


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
        # Logged before notifying, so a recommendation is on the record whether
        # or not anyone was told about it -- the ones that were suppressed are
        # exactly what makes "did suppressing them help?" answerable.
        logged = _journal_recommendations(store, config)
        if logged:
            print(f"Logged {logged} new recommendation(s) to "
                  f"{config.storage.recommendations.name}")
        if not args.no_notify:
            _notify_new_strong_buys(store, config)

    for path in paths:
        print(f"Dashboard written to {path}")
    if args.open:
        import webbrowser

        webbrowser.open(paths[0].as_uri())
    return 0


def cmd_prune(config: Config, args) -> int:
    """Throw away intraday bars no reader can reach.

    The database is committed to git and only ever grew: three years of hourly
    history behind a dashboard that draws ninety bars, 78 MB against GitHub's
    50 MB warning. See `Store.prune_unmeasurable_intraday` for why a bar older
    than both the chart window and the symbol's first daily bar is unreachable
    rather than merely old.

    Safe to run on every schedule -- it is a no-op once the backlog is gone.
    """
    path = config.storage.database
    before = path.stat().st_size if path.exists() else 0
    with Store(path) as store:
        removed = store.prune_unmeasurable_intraday(config.dashboard.chart_days)
    after = path.stat().st_size if path.exists() else 0
    if not removed:
        print("Nothing to prune — every intraday bar on file is still readable.")
        return 0
    print(f"Pruned {removed:,} unreadable intraday bar(s); "
          f"{before / 1e6:.1f} MB → {after / 1e6:.1f} MB.")
    return 0


def cmd_evaluate(config: Config, args) -> int:
    """Measure what happened after every recorded pattern.

    Reads only price history already on disk, so it is safe to re-run and
    always gives the same answer for the same data. The first run measures the
    whole back catalogue -- there is no need to have been collecting anything
    special, because the prices were always there.
    """
    from .outcomes import FORWARD_BARS, forward_outcomes
    from .strategies import walk

    variants = list(config.strategies.variants)

    with Store(config.storage.database) as store:
        # The daily series is the common ruler for every horizon, so it is
        # fetched once per symbol rather than once per signal.
        daily: dict[str, list[tuple[str, float]]] = {}
        entries: dict[tuple[str, str], dict[str, float]] = {}
        measured, skipped = [], 0
        trades = []

        for ticker in config.tickers:
            daily[ticker.symbol] = [
                (p.date, p.close) for p in store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
            ]

        for horizon in config.horizons:
            for ticker in config.tickers:
                key = (ticker.symbol, horizon.key)
                if key not in entries:
                    entries[key] = {
                        p.date: p.close
                        for p in store.rsi_series(ticker.symbol, horizon.key)
                    }
                for signal in store.all_signals(ticker.symbol, horizon.key):
                    # The close on the bar the pattern completed on -- the
                    # price you could actually have paid. Deliberately not
                    # `signal.price`, which `_rescore_signals` overwrites with
                    # whatever the latest valuation says.
                    entry = entries[key].get(signal.up2_date)
                    results = forward_outcomes(
                        ticker.symbol, horizon.key, signal.direction,
                        signal.up2_date, entry, daily[ticker.symbol],
                    )
                    if not results:
                        skipped += 1
                    measured.extend(results)

                    # The same signal under each exit rule. Walked here rather
                    # than derived later from max_gain/max_drawdown, which
                    # cannot say which barrier was touched first.
                    for strategy in variants:
                        trade = walk(
                            ticker.symbol, horizon.key, signal.direction,
                            signal.up2_date, entry, daily[ticker.symbol], strategy,
                        )
                        if trade is not None:
                            trades.append(trade)

        written = store.replace_outcomes(measured)
        traded = store.record_trades(trades)

    print(f"Measured {written} outcome(s) across {len(FORWARD_BARS)} windows "
          f"({', '.join(f'+{b}' for b in FORWARD_BARS)} trading days).")
    if variants:
        print(f"Took {traded} trade(s) under {len(variants)} exit rule(s): "
              f"{', '.join(v.label for v in variants)}.")
    if skipped:
        print(f"  {skipped} pattern(s) not yet measurable — too recent, or no "
              f"close recorded on the bar they completed on.")
    return 0


def _print_strategy_comparison(config: Config) -> None:
    """The exit rules, side by side over the same signals.

    Ordered by mean return rather than hit rate, because the two disagree: a
    rule taking 3% profits against 5% stops has to be right 62.5% of the time
    merely to break even, so it can lead on hit rate and still lose money. The
    breakeven column is printed next to the hit rate so the comparison is
    readable without doing that arithmetic in your head.
    """
    from .strategies import compare, summarise

    variants = list(config.strategies.variants)
    if not variants:
        return

    with Store(config.storage.database) as store:
        by_key = {v.key: store.all_trades(strategy=v.key) for v in variants}
    if not any(by_key.values()):
        print("Exit rules: no trades recorded yet — run `evaluate`.\n")
        return

    print("Exit rules, same signals\n")
    header = (f"{'rule':<22}{'n':>7}{'hit':>8}{'needs':>8}{'mean':>8}"
              f"{'median':>8}{'target':>8}{'stop':>7}{'timeout':>9}{'days':>7}")
    print(header)
    print("-" * len(header))

    def line(label: str, rows) -> None:
        s = summarise(rows)
        if not s["n"]:
            print(f"{label:<22}{'—':>7}")
            return
        print(f"{label:<22}{s['n']:>7}{s['hit_rate'] * 100:>7.1f}%"
              f"{breakeven:>7.1f}%{s['mean'] * 100:>7.2f}%"
              f"{s['median'] * 100:>7.2f}%{s['target']:>8}{s['stopped']:>7}"
              f"{s['timeout']:>9}{s['mean_bars']:>7.1f}")

    # Split by direction, because the blended row is not a strategy anyone
    # would run: buys and sells score in opposite directions on this sample,
    # so mixing them measures the ratio of buys to sells as much as the rule.
    for key, _ in compare(by_key):
        v = config.strategies.variant(key)
        breakeven = v.breakeven_hit_rate * 100
        rows = by_key[key]
        line(v.label, rows)
        for direction in (BUY, SELL):
            line(f"  {direction}", [t for t in rows if t.direction == direction])
    print()
    print("  · `needs` is the hit rate the rule would break even at IF every exit")
    print("    landed exactly on its barrier. Real ones overshoot — a gap opens")
    print("    through the stop, a timeout closes anywhere — so a row can sit")
    print("    below `needs` and still show a positive mean. Use it to compare")
    print("    what two rules are asking of the signal, not as a pass mark.")
    print("  · Exits are the close that breached the barrier, not the barrier —")
    print("    with daily closes an intraday touch is invisible, so tight rules")
    print("    under-trigger and their fills are worse than the level implies.")
    print()


def cmd_backtest(config: Config, args) -> int:
    """Report how the recorded patterns actually did, against a baseline.

    The baseline is the point. Equities drift upward, so any long strategy
    scores above half over a rising sample -- including buying on days picked
    at random. What matters is the gap between the signal and the coin.
    """
    from .outcomes import FORWARD_BARS, baseline_outcomes, summarise

    window = args.bars
    with Store(config.storage.database) as store:
        measured = store.all_outcomes(bars=window)
        if not measured:
            print("No outcomes recorded yet — run `evaluate` first.")
            return 1

        base = []
        for ticker in config.tickers:
            closes = [
                (p.date, p.close) for p in store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
            ]
            base.extend(baseline_outcomes(ticker.symbol, closes, bars=(window,)))

    print(f"Outcome after {window} trading days\n")
    header = f"{'cohort':<22}{'n':>7}{'hit rate':>11}{'mean':>9}{'median':>9}{'worst dd':>10}"
    print(header)
    print("-" * len(header))

    def line(label: str, rows) -> None:
        s = summarise(rows)
        if not s["n"]:
            print(f"{label:<22}{'—':>7}")
            return
        print(f"{label:<22}{s['n']:>7}{s['hit_rate'] * 100:>10.1f}%"
              f"{s['mean'] * 100:>8.1f}%{s['median'] * 100:>8.1f}%"
              f"{s['worst_drawdown'] * 100:>9.1f}%")

    line("random entry (long)", base)
    print()
    for direction in (BUY, SELL):
        for horizon in config.horizons:
            rows = [o for o in measured
                    if o.direction == direction and o.horizon == horizon.key]
            line(f"{direction:<5} {horizon.label}", rows)
        line(f"{direction} — all", [o for o in measured if o.direction == direction])
        print()

    _print_strategy_comparison(config)

    print("Read with care:")
    print("  · Survivorship — the watchlist is today's companies, which all still")
    print("    exist and are still large. Historical returns read optimistically.")
    print("  · No valuation cohort. Fair values only exist from 2026-07-27, and")
    print("    `_rescore_signals` back-applies today's to every old pattern, so a")
    print("    'strong buy' split of this sample would be reading the future.")
    print("    That comparison starts from recommendations.csv, going forward.")

    if args.csv:
        import csv as _csv

        path = Path(args.csv)
        with path.open("w", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(["symbol", "horizon", "direction", "up2_date", "bars",
                             "entry", "exit", "return_pct", "max_gain", "max_drawdown"])
            for o in measured:
                writer.writerow([o.symbol, o.horizon, o.direction, o.up2_date, o.bars,
                                 o.entry, o.exit, o.return_pct, o.max_gain, o.max_drawdown])
        print(f"\nWrote {len(measured)} rows to {path}")
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
    p_backfill.add_argument(
        "--max-new", type=int, default=None, dest="max_new", metavar="N",
        help="seed at most N never-seen tickers this run; the rest wait for the "
             "next one (Yahoo is one request per ticker per horizon and cannot "
             "be batched)",
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

    p_eval = sub.add_parser(
        "evaluate", help="measure what happened after every recorded pattern"
    )
    p_eval.set_defaults(func=cmd_evaluate)

    p_prune = sub.add_parser(
        "prune", help="drop intraday bars older than both the chart and the daily history"
    )
    p_prune.set_defaults(func=cmd_prune)

    p_bt = sub.add_parser(
        "backtest", help="hit rate and returns per cohort, against a random-entry baseline"
    )
    p_bt.add_argument(
        "--bars", type=int, default=20, metavar="N",
        help=f"trading days after the signal to measure at; one of "
             f"{', '.join(str(b) for b in OUTCOME_BARS)} (default: 20)",
    )
    p_bt.add_argument("--csv", default=None, help="also write the raw rows here")
    p_bt.set_defaults(func=cmd_backtest)

    p_uni = sub.add_parser(
        "universe", help="propose tickers to add to the watchlist"
    )
    p_uni.add_argument(
        "--market", default="america",
        help="TradingView's regional scanner: america, netherlands, uk, … (default: america)",
    )
    p_uni.add_argument(
        "--indexes", default="S&P 500,NASDAQ 100",
        help='comma-separated index names to require, e.g. "S&P 500,NASDAQ 100" '
             'or "STOXX Europe 600". Empty means any listing that passes the filters.',
    )
    p_uni.add_argument(
        "--min-cap", type=float, default=5e9, dest="min_cap",
        help="minimum market capitalisation (default: 5e9)",
    )
    p_uni.add_argument(
        "--min-volume", type=float, default=5e5, dest="min_volume",
        help="minimum 10-day average volume, to exclude illiquid names (default: 5e5)",
    )
    p_uni.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="propose at most N tickers (largest first)",
    )
    p_uni.add_argument(
        "--scan-limit", type=int, default=1000, dest="scan_limit",
        help="how many listings to pull from the scanner before filtering",
    )
    p_uni.add_argument(
        "--write", action="store_true",
        help="append the proposals to config.yaml instead of only printing them",
    )
    p_uni.set_defaults(func=cmd_universe)

    p_scrape = sub.add_parser(
        "scrape", help="read fair values off Morningstar (needs `login` first)"
    )
    p_scrape.add_argument(
        "--all", action="store_true", help="every ticker, not just ones with a live signal"
    )
    p_scrape.add_argument("--symbols", help="explicit comma-separated list, e.g. IBM,NVDA")
    p_scrape.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="stop after N pages this session; the rest wait for the next run "
             "(targets are ordered most actionable first)",
    )
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
    p_dash.add_argument("--no-notify", action="store_true",
                        help="build the pages without posting a new deal to the webhook")
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
