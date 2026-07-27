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
from pathlib import Path

from .config import Config, load_config
from .fairvalues import FairValueError, load_fair_values, save_fair_value
from .morningstar import (
    AuthenticationError,
    MorningstarError,
    save_login_session,
    scrape_ticker,
    to_valuation,
)
from .notify import format_signal, send_webhook
from .rsi import wilder_rsi_series
from .signals import find_cross_pairs, is_strong, signal_fires, valuation_passes
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
    """
    with Store(config.storage.database) as store:
        for ticker in config.tickers:
            try:
                closes = fetch_daily_closes(ticker.yahoo, range_=args.range)
            except MarketDataError as exc:
                print(f"  {ticker.symbol}: {exc}")
                continue

            if len(closes) < config.rsi.period + 1:
                print(f"  {ticker.symbol}: only {len(closes)} closes, need {config.rsi.period + 1}")
                continue

            dates = [d for d, _ in closes]
            prices = [c for _, c in closes]
            rsis = wilder_rsi_series(prices, config.rsi.period)

            written = 0
            for date, close, rsi in zip(dates, prices, rsis):
                if rsi is None:
                    continue
                store.upsert_rsi_point(
                    RsiPoint(ticker.symbol, date, close, rsi, "backfill:yahoo")
                )
                written += 1
            print(f"  {ticker.symbol}: {written} days of RSI history ({dates[0]} → {dates[-1]})")

        _detect_and_record(store, config, announce=False)
    return 0


def cmd_run(config: Config, args) -> int:
    """The daily job: fetch, store, detect, notify."""
    today = args.date or dt.date.today().isoformat()
    print(f"Run for {today}")
    print(f"Valuation gate: {config.signal.describe_rule()}")
    print(f"Window: two upward RSI crosses of {config.rsi.threshold:g} "
          f"within {config.signal.window_days} {config.signal.window_unit} days\n")

    exit_code = 0
    with Store(config.storage.database) as store:
        recorded = sync_fair_values(store, config)
        if recorded:
            print(f"  {recorded} hand-checked fair value(s) loaded "
                  f"from {config.storage.fair_values.name}\n")
        # --- RSI, from TradingView -------------------------------------
        for ticker in config.tickers:
            try:
                quote = fetch_live_rsi(
                    ticker.tradingview,
                    period=config.rsi.period,
                    interval=config.rsi.interval,
                )
            except MarketDataError as exc:
                print(f"  {ticker.symbol}: RSI unavailable — {exc}")
                exit_code = 1
                continue
            store.upsert_rsi_point(
                RsiPoint(ticker.symbol, today, quote.close, quote.rsi, "live:tradingview")
            )
            print(f"  {ticker.symbol}: RSI {quote.rsi:6.2f}   close {quote.close:,.2f}")

        # --- Price + fair value, from Morningstar (v2, opt-in) ----------
        if not args.with_morningstar:
            print("\n  (RSI only — check fair value from the dashboard button,")
            print("   then record it with: python -m screener.cli fair-value SYM <value>)")
        else:
            print()
            for ticker in config.tickers:
                try:
                    result = scrape_ticker(ticker, config.morningstar)
                    valuation = to_valuation(result, today)
                except AuthenticationError as exc:
                    print(f"  {ticker.symbol}: {exc}")
                    exit_code = 1
                    break  # one expired session breaks every ticker
                except MorningstarError as exc:
                    print(f"  {ticker.symbol}: {exc}")
                    exit_code = 1
                    continue
                store.upsert_valuation(valuation)
                gap = (valuation.price / valuation.fair_value - 1) * 100
                print(
                    f"  {ticker.symbol}: price {valuation.price:,.2f}  "
                    f"fair value {valuation.fair_value:,.2f}  ({gap:+.1f}% vs FV)"
                )

        # --- Detect -----------------------------------------------------
        print()
        _detect_and_record(store, config, announce=True)

        snapshot = export_csv_snapshot(store, config.storage.csv_dir)
        print(f"\nSnapshot written to {snapshot}")
    return exit_code


def _detect_and_record(store: Store, config: Config, announce: bool) -> list[Signal]:
    """Scan stored history for the pattern and record anything new."""
    new_signals: list[Signal] = []
    now = dt.datetime.now().isoformat(timespec="seconds")

    for symbol in store.symbols():
        series = store.rsi_series(symbol)
        for pair in find_cross_pairs(series, config.rsi.threshold, config.signal):
            if store.signal_exists(symbol, pair.up2_date):
                continue

            valuation = store.valuation(symbol, pair.up2_date)
            price = valuation.price if valuation else None
            fair_value = valuation.fair_value if valuation else None
            known, confirms = valuation_passes(price, fair_value, config.signal)

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
            )
            store.record_signal(signal)
            append_signal_csv(config.storage.csv_dir, signal)
            new_signals.append(signal)

    if announce:
        fired = [s for s in new_signals if s.fired]
        if fired:
            for signal in fired:
                message = format_signal(signal, config.signal.describe_rule())
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
                    f"  {signal.symbol}: RSI pattern completed {signal.up2_date}, "
                    f"but not fired — {reason}."
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
        series = store.rsi_series(symbol)
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
        known, confirms = valuation_passes(price, args.value, config.signal)
        verdict = "STRONG BUY — valuation confirms" if is_strong(known, confirms) else (
            "buy signal stands, but it's trading above fair value")
        print(f"{symbol}: fair value {args.value:,.2f} vs close {price:,.2f}")
        print(f"  {verdict}")
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
        series = store.rsi_series(symbol)
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
        known, confirms = valuation_passes(latest.close, entry.fair_value, config.signal)
        fired = signal_fires(confirms, config.signal)
        for signal in store.all_signals(symbol):
            store.update_signal_valuation(
                symbol, signal.up2_date, latest.close, entry.fair_value, known, confirms, fired
            )
        applied += 1
    return applied


def _apply_valuation_to_pending_signals(
    store: Store, config: Config, symbol: str, price: float, fair_value: float
) -> int:
    """Re-score every recorded pattern for `symbol` against one fair value."""
    known, confirms = valuation_passes(price, fair_value, config.signal)
    fired = signal_fires(confirms, config.signal)
    updated = 0
    for signal in store.all_signals(symbol):
        store.update_signal_valuation(
            symbol, signal.up2_date, price, fair_value, known, confirms, fired
        )
        updated += 1
    return updated


def cmd_dashboard(config: Config, args) -> int:
    from .dashboard import build_dashboard

    output = Path(args.output) if args.output else config.dashboard.output
    with Store(config.storage.database) as store:
        sync_fair_values(store, config, quiet=True)
        path = build_dashboard(store, config, output)

    print(f"Dashboard written to {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def cmd_signals(config: Config, args) -> int:
    with Store(config.storage.database) as store:
        signals = store.all_signals(args.symbol)
        if not signals:
            print("No RSI patterns recorded yet.")
            return 0
        print(f"{'SYMBOL':<8}{'CROSS 1':<12}{'DIP':<12}{'CROSS 2':<12}{'PRICE':>10}{'FAIR VAL':>10}  RESULT")
        for s in signals:
            price = f"{s.price:,.2f}" if s.price is not None else "-"
            fair = f"{s.fair_value:,.2f}" if s.fair_value is not None else "-"
            if s.fired and is_strong(s.valuation_known, s.valuation_pass):
                verdict = "STRONG BUY (valuation confirms)"
            elif s.fired and s.valuation_known:
                verdict = "BUY SIGNAL (above fair value)"
            elif s.fired:
                verdict = "BUY SIGNAL (fair value unchecked)"
            else:
                verdict = "pattern only (valuation gate failed)"
            print(f"{s.symbol:<8}{s.up1_date:<12}{s.down_date:<12}{s.up2_date:<12}{price:>10}{fair:>10}  {verdict}")
    return 0


def cmd_report(config: Config, args) -> int:
    with Store(config.storage.database) as store:
        valuations = {v.symbol: v for v in store.latest_valuations()}
        print(f"Valuation gate: {config.signal.describe_rule()}\n")
        print(f"{'SYMBOL':<8}{'DATE':<12}{'RSI':>8}{'PRICE':>11}{'FAIR VAL':>11}  GATE")
        for symbol in store.symbols():
            series = store.rsi_series(symbol)
            last = series[-1] if series else None
            val = valuations.get(symbol)
            rsi = f"{last.rsi:.2f}" if last else "-"
            date = last.date if last else "-"
            price = f"{val.price:,.2f}" if val else "-"
            fair = f"{val.fair_value:,.2f}" if val else "-"
            if val:
                _, passed = valuation_passes(val.price, val.fair_value, config.signal)
                gate = "pass" if passed else "fail"
            else:
                gate = "unknown"
            print(f"{symbol:<8}{date:<12}{rsi:>8}{price:>11}{fair:>11}  {gate}")
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
    p_backfill.add_argument("--range", default="1y", help="history range (e.g. 6mo, 1y, 2y)")
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
