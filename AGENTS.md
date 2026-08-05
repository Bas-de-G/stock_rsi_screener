# Working on this repo

Notes for anyone — human or coding agent — making changes here. Codex reads this
file by convention; `CLAUDE.md` points at it so both agents follow the same
rules.

## What this is

A screener that watches ~65 tickers for a double-crossing pattern in both
directions: **buy** on two upward crosses of RSI 30, **sell** on two downward
crosses of 70. Screened on four horizons (1h / 4h / 1d / 1w), each with its own
cross window, valuation margin and suggested leverage, and grouped into four
market filters (sp500 / nasdaq / europe / asia / penny) a ticker can belong to
more than one of.

A pattern only counts as a *live* signal while its **second** cross sits inside
the horizon's lookback from now and RSI is still on the signalling side. Age is
measured from the completing cross, not the first one: the pattern doesn't
exist until it completes, and measuring from the start charged a pattern's own
span against its freshness. Morningstar
fair value then grades it — required for the rocket — and earnings growth acts
as a veto on top (the value-trap case: cheap, but earnings shrinking).

The RSI half, and earnings growth alongside it, run themselves on GitHub
Actions — both come from TradingView's free scanner endpoint, no login needed.
The valuation half runs from a laptop, on purpose — see "Credentials" below.

## Layout

| Module | Owns |
|---|---|
| `screener/config.py` | `config.yaml` loading and validation |
| `screener/tradingview.py` | live RSI (TradingView) + historical closes (Yahoo) |
| `screener/rsi.py` | Wilder RSI, used for backfill only |
| `screener/signals.py` | pattern detection and the valuation gate |
| `screener/storage.py` | SQLite: `rsi_history` (+ earnings growth), `valuations`, `signals` |
| `screener/fairvalues.py` | the committed `fair_values.yaml` |
| `screener/morningstar.py` | logged-in scraping of price + fair value |
| `screener/dashboard.py` | the self-contained HTML page |
| `screener/cli.py` | commands, and the glue between all of the above |

## Rules that aren't obvious from the code

**Everything is scoped by horizon.** `rsi_history` and `signals` are both keyed
`(symbol, horizon, …)`, and `rsi_series`/`all_signals`/`signal_exists` all take
a horizon. Forgetting one silently mixes hourly and weekly bars into a single
series. `config.Horizon` carries the three things that scale with holding
period — `window_days`, `margin`, `leverage` — so nothing should hardcode them.

**Intraday bars are keyed by timestamp, daily/weekly by date.** A 1h bar's
label is `2026-08-04T18:49`; a daily one is `2026-08-04`. Both sort correctly
as strings, which is what `rsi_series`'s ORDER BY relies on. Crucially,
`date.fromisoformat` *rejects* the intraday form on every supported Python —
use `signals._moment`, which parses both. That bug made every intraday pattern
raise, so no intraday signal was ever recorded.

**`backfill` never skips an intraday horizon.** The skip-if-seeded rule applies
only to daily and weekly bars. An hourly history collected an hour ago is
already missing bars, so 1h/4h always refetch; the upsert dedupes.

**`fair_values.yaml` is the source of truth for fair values. SQLite is derived.**
Never write a fair value straight to the database. The `.db` is gitignored
locally, rebuilt from scratch by CI, and binary — a value written only there is
invisible in review and lost on the next rebuild. Write to the YAML via
`fairvalues.save_fair_value`, then call `cli.sync_fair_values` to fold it in.
`sync_fair_values` treats the file as authoritative: an entry removed from it is
cleared from the database on the next run.

**Never commit anything under `data/`.** It's gitignored. CI force-adds it, and
only on `main` (see the branch guard in `.github/workflows/daily.yml`). A human
committing it will collide with the bot.

**Running the screener locally modifies a tracked file, which blocks `git
pull`.** `data/screener.db` (and the CSVs) are force-added by CI, so they stay
tracked even though `.gitignore` covers them. Any local `backfill`/`run`/
`dashboard` invocation dirties that file; the next plain `pull` refuses rather
than overwrite it. Fix: discard it, don't merge it — it's fully regenerable —
`git checkout -- data/screener.db data/latest.csv data/signals.csv && git pull`.

**The signal predicates are deliberately separate** (`screener/signals.py`):

- `valuation_passes(price, fair_value, config)` → `(known, confirms)`. `known` is
  False when there's nothing to compare; `confirms` answers "does the valuation
  agree?" — not "is this a signal?"
- `earnings_growth_passes(growth)` → the same `(known, confirms)` shape, for YoY
  EPS growth. `confirms` is any positive number — no threshold to tune.
- `signal_fires(confirms, config)` → whether it counts as a buy signal at all.
  With `fire_without_valuation: true` the RSI pattern stands alone. Deliberately
  takes only the valuation's `confirms` — earnings growth never gates firing,
  only grading, so a soft quarter can't silently suppress a signal.
- `is_strong(valuation, *vetoes)` → whether it earns the rocket. The factors
  are **not** peers: the valuation is required (unknown or disagreeing means
  never strong), and everything after it can only veto. That's what keeps
  earnings growth a filter on the thesis rather than a substitute for it.
- `signal_is_live(signal, series, config, threshold)` → whether a recorded
  pattern is still tradeable: both crosses inside the lookback measured back
  from the latest bar, and RSI still on the signalling side. Detection records
  everything; liveness decides what gets shown and scraped.

Collapsing these back into one function breaks the "fire on the pattern, grade
on independent factors" behaviour the dashboard is built around. Adding a third
grading factor later is just another `(known, confirms)` pair passed to
`is_strong` — the signature doesn't need to change.

**`signals` is a complete log; the dashboard shows only live ones.** Detection
records every pattern it ever finds, both directions, all horizons — that's
the historical record. `signals.signal_is_live` decides what's actionable now,
and `dashboard._collect` filters through it. Don't conflate the two: dropping
a pattern at detection time would lose history you can't recover.

**Buy and sell are mirrors, and the mirroring is not just the crosses.** The
valuation rule flips (`cli._gate_for`) — what argues for buying is price below
fair value, so what argues for selling is price above it. Earnings growth
flips too (`cli._growth_for`): growing earnings argue *against* a sell. Get
either wrong and the sell side silently grades backwards.

**Cross detection needs one bar of lead-in.** A cross compares a bar against
its predecessor, so slicing the visible window throws away the predecessor of
its first bar — a cross landing exactly on the left edge then goes uncounted.
`dashboard._visible_crosses` detects over `window + 1` bars and shifts the
indices back. Without it, cards showed "1 upward cross of 30" directly above a
completed up/down/up pattern, which by definition needs two. Any future code
that counts crosses over a slice has the same trap.

**The scraper never trusts the page's own price.** `_price_from_text` takes
the first currency-prefixed figure on the page, which is regularly the wrong
one — on AMD it read 1.62 for a ~$200 stock, and on a sparse layout it returns
the fair value itself. The screener already holds an authoritative close from
TradingView in the listing's own currency, and `sync_fair_values` uses *that*
for the gate regardless, so `_extract` takes a `reference_price` that
overrides whatever the page said. The same reference disambiguates the fair
value: a candidate more than 10x off the real price is a parse error, not a
valuation — that's what fixed UNH reading $16 against a $409 price.

**`scrape` skips fair values checked within 14 days** (`--force` overrides,
`--max-age` retunes). Kept in `_drop_recently_checked`, deliberately separate
from `_resolve_scrape_targets`, so "you named a symbol that doesn't exist"
stays distinguishable from "that one is still fresh" — the first is a non-zero
exit, the second is the feature working.

**A scrape result can be incomplete without raising.** `_scrape_on_page`
returns a `ScrapeResult` with `price=None` or `fair_value=None` when the page
loads fine, isn't a bot challenge and isn't signed out, but extraction still
finds nothing. Callers must check `result.complete` — `cmd_scrape` and
`cmd_run` both do. Trusting a half-filled result writes an ungradeable fair
value with no price beside it and then divides by None, and `TypeError` is not
a `MorningstarError`, so nothing catches it.

**The dashboard's market filter is pure CSS, and must stay that way.** Hidden
radio inputs sit before `.sheet`, and `#mk-x:checked ~ .sheet .card:not(.in-x)`
hides the rest. That keeps the page working from `file://` and with JS off. The
timeframe selector can't work the same way — each horizon has different data —
so it's links between four separately-built pages. Adding a market means adding
it to `config.MARKETS` and `MARKET_LABELS`; the CSS rules generate from there.

**Currency is not always dollars, and identifiers differ per venue.** Every
non-US listing needs its own `tradingview` / `yahoo` / `morningstar` /
`currency` fields, because all four differ:

- Amsterdam: `EURONEXT:ASML` / `ASML.AS` / `xams/asml` / EUR. TradingView uses
  one `EURONEXT:` prefix for the whole exchange group — `AMS:` and `XAMS:`
  both 404.
- London: `LSE:RR.` (trailing dot) / `RR.L` / `xlon/rr.` / **GBX**.

Rolls-Royce is the dangerous one: quoted in pence, not pounds.
`morningstar.check_units` rejects a price/fair-value pair more than 10× apart,
which is what catches a pence-vs-pounds mix-up before it reaches the gate.
Euro-quoted names don't trip it — EUR and USD are the same order of magnitude.

When adding a ticker, verify it against **both** TradingView and Yahoo before
committing, and check the two prices agree — that's what proves you've got the
same listing rather than a same-named symbol on another venue. The Morningstar
slug can't be verified from a script (their CDN returns HTTP 202 with an empty
body for valid *and* bogus URLs alike), so follow the MIC-code convention and
accept that a wrong slug means a dead dashboard button, nothing worse.

**Both grading factors are scored from *current* data, never from the signal
date.** `sync_fair_values` uses the latest close and today's fair value;
`sync_earnings_growth` is its counterpart and must be called alongside it.
Earnings growth used to be read from the bar at the pattern's own second
cross — which is almost always backfilled and carries no figure — so the
factor came back unknown on every signal and silently graded nothing: 0 of 72
on the live database. Both are quarterly fundamentals describing the company
now, not properties of one historical bar.

**Earnings growth only exists on live-fetched rows.** `rsi_history.earnings_growth`
comes from TradingView's scanner alongside RSI (`tradingview.fetch_live_rsi`) —
backfill has no historical source for it, so a signal whose `up2_date` was
backfilled rather than observed live shows earnings growth as unknown forever,
the same way an unchecked fair value does. TTM is preferred; FY is the fallback
for a stock too newly listed to have a trailing year (SanDisk, post its 2025
spin-off, is the live example).

**`signals.csv` rewrites, it doesn't append.** It's force-committed to `main`
by CI, so a copy from before any given schema change is sitting in git history.
A blind append under a grown header would misalign columns on every row after
that point — `storage.append_signal_csv` reads the existing rows back and
rewrites the whole file under the current header instead. Keep that pattern if
`Signal` grows again.

**History depth is sized to what's displayed, not to what Yahoo offers.** Only
`chart_days` (90) bars are ever plotted, plus 15 to seed Wilder's RSI and one
lead-in bar. Each horizon's `yahoo_range` lands at ~2.5x that. Asking for
Yahoo's full 730-day intraday depth gave 5,000 hourly bars a ticker — a 54 MB
database to show 2.8 MB worth. That matters because the database is committed
to git: at hourly runs it was ~9 GB a year, and GitHub warns at 1 GB. For the
same reason only the last scheduled run of the day commits it; intermediate
runs publish a fresh dashboard without snapshotting, which loses nothing
because `backfill` rebuilds every horizon from Yahoo each run.

**Tests must never touch the repo's real `config.yaml` or `fair_values.yaml`.**
`load_config` falls back to repo-root defaults for any storage path a test
config omits, so a fixture missing `fair_values:` points at the live file — and
a test that writes one overwrites committed data. That happened. `conftest.py`
now has an autouse guard that restores the file and fails the test.

**`backfill` skips a ticker that already has a full chart's worth of history**
(`>= dashboard.chart_days` rows), unless `--force`. That's what makes it safe
for `daily.yml` to call unconditionally on every scheduled run instead of the
old one-time-only guard (`if [ ! -f data/screener.db ]`), which meant a ticker
added to `config.yaml` after the database already existed — SanDisk was the
case that surfaced this — would never get backfilled by CI and would trickle
in one live row a day. If you add a threshold check like this elsewhere,
remember the point is "has enough for the *dashboard*," not "has the bare
minimum for RSI to compute."

## Credentials

There is **no password setting anywhere in this tool** — not in `config.yaml`,
not in `.env`, not in the code. `screener login` opens a real browser so the
account holder types their own password into Morningstar, and only the resulting
session cookie is saved, to `auth/` (gitignored).

Don't add a scraping step to `.github/workflows/daily.yml`. This repo is public;
an Actions secret is readable by anyone who can push a workflow, session cookies
expire every few weeks, and a datacenter IP running headless Chromium against a
subscriber-only page is the textbook bot-block signature. The valuation half runs
locally on purpose.

## Running things

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # 313 tests, fully offline

python -m screener.cli backfill              # seeds all four horizons
python -m screener.cli run                   # the scheduled job (RSI only)
python -m screener.cli dashboard --open      # writes all four pages

# Any of them can be pinned to one timeframe:
python -m screener.cli run --horizon 1h
```

Fair values, which need `python -m playwright install chromium` and a one-off
`screener login`:

```bash
python -m screener.cli scrape             # only tickers with a live signal
python -m screener.cli scrape --dry-run   # see what it would visit
python -m screener.cli scrape --push      # commit + push the YAML when done
```

## Tests

Offline by design — no network, no browser, no credentials — so they can't go red
because TradingView had a blip or Morningstar changed its layout. Mock Playwright
rather than driving it. `.github/workflows/tests.yml` runs them on Python 3.10,
3.11 and 3.12; 3.10 is the floor.
