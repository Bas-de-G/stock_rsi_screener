# Working on this repo

Notes for anyone — human or coding agent — making changes here. Codex reads this
file by convention; `CLAUDE.md` points at it so both agents follow the same
rules.

## What this is

A screener that watches ~35 large-cap stocks for a double-oversold-recovery
pattern: RSI crosses 30 upward, falls back below, and crosses up again inside 14
days. A completed pattern is a buy signal on its own. A Morningstar fair value
that agrees upgrades it to a strong buy (🚀 on the dashboard).

The RSI half runs itself on GitHub Actions. The valuation half runs from a
laptop, on purpose — see "Credentials" below.

## Layout

| Module | Owns |
|---|---|
| `screener/config.py` | `config.yaml` loading and validation |
| `screener/tradingview.py` | live RSI (TradingView) + historical closes (Yahoo) |
| `screener/rsi.py` | Wilder RSI, used for backfill only |
| `screener/signals.py` | pattern detection and the valuation gate |
| `screener/storage.py` | SQLite: `rsi_history`, `valuations`, `signals` |
| `screener/fairvalues.py` | the committed `fair_values.yaml` |
| `screener/morningstar.py` | logged-in scraping of price + fair value |
| `screener/dashboard.py` | the self-contained HTML page |
| `screener/cli.py` | commands, and the glue between all of the above |

## Rules that aren't obvious from the code

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

**The three signal predicates are deliberately separate** (`screener/signals.py`):

- `valuation_passes(price, fair_value, config)` → `(known, confirms)`. `known` is
  False when there's nothing to compare; `confirms` answers "does the valuation
  agree?" — not "is this a signal?"
- `signal_fires(confirms, config)` → whether it counts as a buy signal at all.
  With `fire_without_valuation: true` the RSI pattern stands alone.
- `is_strong(known, confirms)` → whether it earns the rocket.

Collapsing these back into one function breaks the "fire on the pattern, grade
on the valuation" behaviour the dashboard is built around.

**The dashboard only shows signals inside the chart window.** `dashboard._collect`
filters on `up2_date >= ` the first date in the visible series, and
`cli._signalled_symbols` mirrors that rule so an aged-off signal doesn't trigger
a scrape. Change one, change the other.

**Currency is not always dollars.** Rolls-Royce (`RR`) is quoted in pence on LSE
and has a different identifier on all three services — `LSE:RR.` / `RR.L` /
`xlon/rr.`. `morningstar.check_units` rejects a price/fair-value pair more than
10× apart, which is what catches a pence-vs-pounds mix-up before it reaches the
gate.

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
python -m pytest tests/ -q          # 168 tests, fully offline

python -m screener.cli backfill     # once, seeds RSI history from Yahoo
python -m screener.cli run          # the daily job (RSI only)
python -m screener.cli dashboard --open
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
