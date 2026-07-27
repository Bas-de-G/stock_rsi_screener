# Stock RSI screener

Watches a list of stocks every day and flags a buy signal when a specific
pattern appears:

> RSI drops below 30, climbs back through 30, drops below 30 again, and climbs
> back through 30 a second time — with both crossings inside a 14-day window —
> **and** the stock is on the required side of Morningstar's fair value estimate.

Data comes from two places:

| What | Source | Login needed |
|---|---|---|
| RSI (14) | TradingView | no |
| Price and fair value | Morningstar | **yes** — fair value is subscriber-only |
| Historical closes (for backfill) | Yahoo Finance | no |

Default tickers are **AMZN**, **NVDA** and **IBM**; edit `config.yaml` to change them.

---

## ⚠️ Read this first: which way round is the valuation gate?

You asked for a signal only when **fair value < price**. That is what's
configured, and it means: *only buy when the stock is trading **above** what
Morningstar thinks it's worth.*

That is the opposite of how Morningstar's fair value is normally used — the
usual "this is cheap" reading is price **below** fair value. In your own
screenshot IBM shows a fair value of \$225.00 against a price of \$214.19,
i.e. trading *below* fair value, which the current setting would **not** fire on.

It's one line in `config.yaml` either way, so pick deliberately:

```yaml
signal:
  # currently active — fires when the stock is ABOVE fair value:
  valuation_rule: fair_value_below_price

  # the "buy it while it's cheap" reading — fires when it's BELOW fair value:
  # valuation_rule: price_below_fair_value
```

Every run prints the active rule in plain English, so you can't drift into the
wrong one by accident:

```
Valuation gate: fair value < price (stock trading ABOVE Morningstar fair value)
```

---

## Setup

Needs Python 3.10+.

```bash
git clone https://github.com/bas-de-g/stock_rsi_screener.git
cd stock_rsi_screener

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # browser used for Morningstar

cp .env.example .env                      # optional; see "Credentials" below
```

### 1. Sign in to Morningstar (once)

```bash
python -m screener.cli login
```

A **real Chromium window opens** on the Morningstar sign-in page and waits.
Sign in there by hand, including any 2-factor prompt. When you're through,
the session cookies are saved to `auth/morningstar_state.json` and the window
closes.

That file is gitignored. It's the only thing this tool stores, and no password
is ever typed by the code or written to disk.

> **On your Safari login:** Playwright can't borrow Safari's cookies, so being
> signed in there doesn't carry over — this command signs in once in its own
> browser. You only do it again when the session expires (typically weeks).

Check it worked:

```bash
python -m screener.cli check-auth
# OK — price 217.24, fair value 225.00 (via network-json)
```

### 2. Build RSI history (once)

```bash
python -m screener.cli backfill --range 1y
```

Without this the tool would have to watch for 14+ days before it could
recognise a pattern. Backfill computes RSI from a year of real daily closes,
so signals work from day one.

The computed RSI matches TradingView's exactly — same Wilder smoothing,
verified to the cent (see `tests/test_rsi.py`).

### 3. Run it

```bash
python -m screener.cli run
```

```
Run for 2026-07-27
Valuation gate: fair value < price (stock trading ABOVE Morningstar fair value)
Window: two upward RSI crosses of 30 within 14 calendar days

  AMZN: RSI  37.30   close 231.86
  NVDA: RSI  42.61   close 196.75
  IBM: RSI  36.57   close 216.96

  AMZN: price 231.86  fair value 200.00  (+15.9% vs FV)
  ...

No buy signals today.
```

---

## Automating the daily run

### On a Mac (recommended — this is where the Morningstar login lives)

Run every weekday at 17:35 New York time, after the close:

```bash
crontab -e
```

```cron
35 17 * * 1-5 cd /path/to/stock_rsi_screener && .venv/bin/python -m screener.cli run >> data/cron.log 2>&1
```

Run it **after the market closes**. TradingView's RSI moves during the session,
so a midday reading can cross 30 and then cross back before the bell.

### On GitHub Actions (optional, RSI only)

`.github/workflows/daily.yml` runs on a schedule and commits the collected
history back to the repo. By default it skips Morningstar — a session cookie is
a credential, and it isn't in the repo.

To include the valuation half there too, see below.

---

## Credentials, and why none of them are in this repo

This repo is public, so nothing secret is in it, and `.gitignore` blocks the
files that would carry one:

```
.env          your own settings, if you make one
auth/         the saved Morningstar session
data/*.db     collected data (local runs)
debug/        page dumps
```

There is **no password setting anywhere in this tool** — not in `config.yaml`,
not in `.env`, not in the code. That's deliberate: the `login` command opens a
browser window so the account holder types their own password directly into
Morningstar, and only the resulting session cookie is kept.

So a Morningstar password never needs to be shared, pasted into a file, or sent
over chat to use this. If one already was shared, changing it is the safe move —
this tool won't need the new one.

### Running the valuation half in CI

Only do this if you accept that a GitHub Actions secret is readable by anyone
who can push a workflow to the repo — **on a public repo that's a real
consideration**, and the safer answer is to run the Morningstar half on the Mac.

If you do want it:

```bash
base64 -i auth/morningstar_state.json | pbcopy
```

Paste it into a repository secret named `MORNINGSTAR_STATE_B64`. The workflow
picks it up automatically.

---

## Where the data goes

Everything lands in `data/`:

- **`screener.db`** — SQLite, the source of truth. Three tables:
  `rsi_history` (one row per symbol per day), `valuations` (Morningstar price
  and fair value per day), `signals` (every completed pattern, fired or not).
- **`latest.csv`** — current snapshot, one row per symbol.
- **`signals.csv`** — running log, appended whenever a pattern completes.

Reruns of the same day update in place rather than duplicating, and a
backfilled RSI never overwrites a live TradingView reading.

Inspect it without SQL:

```bash
python -m screener.cli report     # where each ticker stands now
python -m screener.cli signals    # every pattern found, fired or not
```

Patterns that match the RSI shape but fail the valuation gate are still
recorded — just marked as not fired. You keep the history either way:

```
SYMBOL  CROSS 1     DIP         CROSS 2       PRICE  FAIR VAL  RESULT
IBM     2026-07-16  2026-07-22  2026-07-23        -         -  pattern only (no valuation)
```

---

## A real example

Backfilling IBM found this genuine occurrence in July 2026:

| Date | RSI | |
|---|---|---|
| 2026-07-15 | 29.17 | below 30 |
| 2026-07-16 | 32.90 | **cross #1** — up through 30 |
| 2026-07-22 | 29.81 | dips back below 30 |
| 2026-07-23 | 30.35 | **cross #2** — up through 30 again |

Seven calendar days apart, comfortably inside the 14-day window — a textbook
match. It shows as "pattern only (no valuation)" because Morningstar's fair
value for a past date can't be backfilled; from now on, live runs record the
valuation alongside each day's RSI, so future patterns get judged properly.

---

## How the signal logic works

An **upward cross** on a given day means yesterday's RSI was below 30 and
today's is at or above it. Reaching exactly 30.00 from below counts as a cross,
matching "goes up to 30 (crosses it)".

Because a cross requires the previous day to be *below* 30, two crosses can't
happen without a dip below in between — the "goes below 30 again" leg comes for
free. The dip is still located and stored so the record shows the full shape.

Only **consecutive** crosses are paired. If RSI crosses up on days 1, 10 and 25,
the candidates are (1, 10) and (10, 25) — never (1, 25), which is a different
shape than the one specified.

`window_days: 14` counts **calendar** days by default. Set
`window_unit: trading` to count 14 trading bars instead (about three weeks).

Tuning knobs, all in `config.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `rsi.period` | 14 | RSI lookback |
| `rsi.threshold` | 30 | the line being crossed |
| `signal.window_days` | 14 | max gap between the two crossings |
| `signal.window_unit` | `calendar` | `calendar` or `trading` days |
| `signal.valuation_rule` | `fair_value_below_price` | see the warning above |
| `signal.fire_without_valuation` | `false` | fire when Morningstar data is missing? |

---

## Notifications

Put a Slack or Discord incoming webhook in `.env`:

```
SCREENER_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Fired signals get posted there. Without it everything still lands in SQLite,
the CSVs, and stdout.

---

## When Morningstar changes its layout

Scraping a site you don't control eventually breaks. Extraction tries three
strategies in order — Morningstar's own JSON caught off the network, then the
rendered valuation card, then a regex over the page text — so one change rarely
breaks everything.

If a run reports incomplete data, it saves what the site actually served:

```
debug/IBM-20260727-181500.html   # the page
debug/IBM-20260727-181500.png    # full-page screenshot
debug/IBM-20260727-181500.json   # the JSON the page fetched
```

Those are what you need to repair the selectors in `screener/morningstar.py`.
`debug/` is gitignored — a dump of a signed-in page can contain account details,
so don't commit one.

If fair value comes back empty but price works, the session is usually signed
in but not a subscriber session. Re-run `login`.

---

## Tests

```bash
python -m pytest tests/ -q      # 40 tests
```

They cover the RSI maths (pinned against TradingView's own published value),
the crossing and window logic including the exact boundary cases, both
directions of the valuation gate, and the storage upsert rules.

The tests are all offline — no network, no browser, no credentials.

---

## Limitations

- **Not investment advice.** It's a pattern detector; the pattern isn't a
  strategy and hasn't been backtested for profitability.
- **Fair value can't be backfilled.** Morningstar publishes the current
  estimate, not a history, so valuation only starts accruing from your first run.
- **RSI moves intraday.** Run after the close, or a cross may reverse by the bell.
- **Scraping is fragile by nature** — see the section above.
- **One TradingView interval at a time**, set by `rsi.interval` (daily by default).
