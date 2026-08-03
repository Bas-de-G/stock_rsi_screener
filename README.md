# Stock RSI screener

Watches a list of stocks every day and flags a buy signal when a specific
pattern appears:

> RSI drops below 30, climbs back through 30, drops below 30 again, and climbs
> back through 30 a second time — with both crossings inside a 14-day window.

Morningstar's fair value then grades it: a signal trading below fair value is
flagged a **strong buy 🚀**.

Data comes from two places:

| What | Source | Login needed |
|---|---|---|
| RSI (14) | TradingView | no |
| Price and fair value | Morningstar | **yes** — subscriber-only, so v1 checks it by hand |
| Historical closes (for backfill) | Yahoo Finance | no |

Tracks **36 market leaders** out of the box (AAPL, MSFT, NVDA, AMZN, IBM, JPM,
XOM, LLY, Rolls-Royce and more) — edit `config.yaml` to change the list. Every
entry was checked against both data sources first, so none of them 404.

Non-US listings work too, they just need their own identifiers. Rolls-Royce is
the worked example: `LSE:RR.` on TradingView (trailing dot), `RR.L` on Yahoo,
`xlon/rr.` on Morningstar, and quoted in **pence** — so it carries a `currency:
GBX` label that the dashboard shows next to the price.

---

## Which way round is fair value read?

A signal counts as **strong** when **price < fair value** — the stock is
trading below what Morningstar thinks it's worth. Flip it in one line if you
ever want the inverse:

```yaml
signal:
  valuation_rule: price_below_fair_value   # active: strong when BELOW fair value
  # valuation_rule: fair_value_below_price # strong when ABOVE fair value
```

Every run prints the active rule in plain English, so you can't drift into the
wrong one by accident:

```
Valuation gate: price < fair value (stock trading BELOW Morningstar fair value)
```

---

## Signal strength

An RSI pattern is a **buy signal on its own** — no fair value needed. Two
independent factors then *grade* it, and either can upgrade it to a
**strong buy 🚀** on its own:

- **Morningstar fair value** — is the price below what Morningstar thinks
  it's worth? Checked by hand or via `screener scrape`.
- **Earnings growth** — is YoY EPS growing? Pulled automatically from
  TradingView's own scanner (the same free endpoint RSI comes from — no
  Morningstar login needed for this one), so it's always there once a
  ticker has a live reading.

Neither factor is required to fire a signal — that's still the RSI pattern
alone (or `fire_without_valuation: false` for strict mode, below). They only
decide how much conviction the dashboard shows:

| On the dashboard | What it means |
|---|---|
| **Strong buy 🚀** | Pattern completed, and *every* factor that's been checked agrees. One checked factor confirming is enough if the other isn't known yet. |
| **Buy signal** | Pattern completed; nothing checked disagrees, but nothing confirms it either — or one known factor disagrees while fired stays true |
| **Pattern, gate failed** | Only in strict mode (below) |
| Oversold / Near threshold / Neutral | No pattern; just where RSI sits now |

The case worth watching for: a stock cheap and oversold by the numbers, but
with **shrinking** earnings — the classic value trap. That shows as a plain
buy signal, not a rocket, specifically because the two factors disagree.

So the flow is: the screener finds signals daily, earnings growth grades them
automatically, and for fair value you click **Check fair value on
Morningstar** on any card you like the look of and record what you read:

```bash
python -m screener.cli fair-value IBM 225
```

If you'd rather nothing fired until a fair value confirms it, set
`fire_without_valuation: false` in `config.yaml` for strict mode.

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

### 1. Build RSI history (once)

```bash
python -m screener.cli backfill --range 1y
```

Without this the tool would have to watch for 14+ days before it could
recognise a pattern. Backfill computes RSI from a year of real daily closes,
so signals work from day one.

The computed RSI matches TradingView's exactly — same Wilder smoothing,
verified to the cent (see `tests/test_rsi.py`).

### 2. Run it

```bash
python -m screener.cli run
```

```
Run for 2026-07-27
Valuation gate: price < fair value (stock trading BELOW Morningstar fair value)
Window: two upward RSI crosses of 30 within 14 calendar days

  AAPL: RSI  66.32   close 335.03
  NVDA: RSI  42.22   close 196.18
  IBM: RSI  36.51   close 216.84
  ... 31 more

  (RSI only — check fair value from the dashboard button,
   then record it with: python -m screener.cli fair-value SYM <value>)

No buy signals today.
```

### 3. Build the dashboard

```bash
python -m screener.cli dashboard --open
```

Writes a single self-contained `data/dashboard.html` — no JavaScript, no CDN,
no fonts to fetch. Open it locally, mail it, drop it in Dropbox, or publish it
(see below). Each stock gets a 90-day RSI plot with the 30 line marked, every
upward crossing ringed and counted, and a button through to its Morningstar
page.

Cards sort by what needs attention: confirmed signals, then patterns awaiting a
fair-value check, then whatever is most oversold right now.

---

## Automating the daily run

The easiest option is **GitHub Pages** (next section) — it runs on GitHub's
servers on a schedule, so nothing has to be switched on at home.

To run it on a Mac instead, every weekday at 17:35 New York time:

```bash
crontab -e
```

```cron
35 17 * * 1-5 cd /path/to/stock_rsi_screener && .venv/bin/python -m screener.cli run >> data/cron.log 2>&1
```

Run it **after the market closes** either way. TradingView's RSI moves during
the session, so a midday reading can cross 30 and cross back before the bell.

---

## Putting it on the web

The scheduled workflow publishes the dashboard to **GitHub Pages**, so there's
a live URL that stays current with nobody's laptop switched on. Your friend
needs no Python, no install, and no accounts — just the link.

**Setup:**

1. Merge to `main`. GitHub only runs scheduled workflows from the repository's
   **default branch** — on a feature branch the schedule never fires, and the
   workflow deliberately won't commit data or publish from anywhere else.
2. **Actions → Daily screener run → Run workflow** to publish straight away
   instead of waiting for the next weekday close.

The workflow enables Pages itself on that first run. If your account settings
don't allow that, turn it on manually under **Settings → Pages → Source:
GitHub Actions** and run it again — the run won't fail either way, it just
skips publishing and still collects the day's data.

The page then lands at:

```
https://bas-de-g.github.io/stock_rsi_screener/
```

It rebuilds every weekday at 21:30 UTC (~30 min after the US close), and again
straight away whenever `fair_values.yaml` changes.

Since the repo is public the page is public too. It carries only public market
data, which is fine to publish — just don't add position sizes to it.

If you'd rather it not be public at all, build locally and drag
`data/dashboard.html` onto [netlify.com/drop](https://netlify.com/drop) for an
unlisted URL. It's a single self-contained file, so that genuinely works.

---

## Where fair values are stored

In **`fair_values.yaml`** — a committed text file, not the database:

```yaml
IBM:
  fair_value: 225.0
  checked: '2026-07-27'
  note: read off the Morningstar quote page
```

Keeping it out of SQLite is deliberate. The database is binary, gitignored
locally, and rewritten by the scheduled job — a value typed on your laptop
would never reach the published page, and two machines editing it would
produce a binary merge conflict nobody can resolve. A YAML file avoids all of
that, and gives you three ways to record a value:

| Who | How |
|---|---|
| You, locally | `python -m screener.cli fair-value IBM 225` |
| You or your friend, from a phone | Edit `fair_values.yaml` on github.com — pencil icon, change the number, commit |
| Nobody | Leave it; the pattern just stays "Verify fair value" |

The shorthand `IBM: 225` works too, so an edit on a phone is one line. Every
run folds this file into the database and re-applies the gate, so a hand edit
takes effect on the next publish without touching anything else.

**To give your friend edit access:** Settings → Collaborators → add him. He
can then edit the file directly. Without it he can still fork and open a pull
request, or just tell you the number.

Delete an entry and it's removed everywhere — the file is the source of truth,
and stale valuations are cleared from the database on the next run.

---

## Scraping fair values

Set up once:

```bash
python -m playwright install chromium
python -m screener.cli login          # opens a browser; sign in by hand
python -m screener.cli check-auth     # confirms the session works
```

A **real Chromium window opens** on the Morningstar sign-in page and waits. Sign
in there yourself, including any 2-factor prompt. The session cookies are saved
to `auth/morningstar_state.json` (gitignored) and reused for weeks.

> **On the Safari login:** Playwright can't borrow Safari's cookies, so being
> signed in there doesn't carry over — this signs in once in its own browser.

Then, whenever you want fair values refreshed:

```bash
python -m screener.cli scrape              # only tickers with a live signal
python -m screener.cli scrape --dry-run    # see what it would visit first
python -m screener.cli scrape --push       # ...and commit + push the result
```

**It only visits tickers with a live signal.** A fair value only changes
anything when a pattern has fired — it's what upgrades a plain buy to a strong
one. A ticker sitting at RSI 60 with no pattern gains nothing from being
scraped, so a typical run fetches three or four pages instead of thirty-five.
Use `--all` to override, or `--symbols IBM,NVDA` to check something specific.

Results are written to `fair_values.yaml`, the same file you'd edit by hand —
so scraped and hand-checked values flow through identical code, and every
change shows up as a readable diff.

### Sharing the results

`scrape` does **not** push by default. It writes the file and stops, so you can
look at the diff first:

```bash
git diff fair_values.yaml
```

Add `--push` to commit and push automatically when you trust it. Either way,
once the file lands on `main` the next scheduled run rebuilds the dashboard —
which is how the values reach anyone else looking at the page.

### Why this runs on your laptop and not in CI

Three reasons, and they compound:

1. **Fair values barely move.** Analysts revise them on earnings or a thesis
   change — roughly quarterly. Daily scraping of a number that changes four
   times a year is all cost.
2. **A session cookie is a credential, and this repo is public.** A GitHub
   Actions secret is readable by anyone who can push a workflow. It also
   expires every few weeks, turning into a recurring re-upload chore.
3. **Datacenter IP + headless Chromium + a subscriber-only page** is the
   textbook bot-block signature.

The scraper paces itself (a randomised 3–8 second gap between pages) for the
same reason.

> **Not implemented:** an earlier version of this README described a
> `MORNINGSTAR_STATE_B64` repository secret that `daily.yml` picked up
> automatically. There is no such step, by the reasoning above.

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

---

## Contributing

The repo is set up for more than one person working on it, with or without a
coding agent.

**Adding a collaborator:** Settings → Collaborators → Add people. That's enough
to clone, push, and run workflows. Coding agents (Codex, Claude Code, others)
are repo-agnostic — once someone has push access, whatever tool they prefer
works.

**Shared conventions** live in **[AGENTS.md](AGENTS.md)** — repo layout, the
rules that aren't obvious from the code, and how to run things. `CLAUDE.md`
points at the same file so the two can't drift apart. Read it before changing
`screener/signals.py` or anything that writes a fair value.

**Two things that will bite you otherwise:**

- **Rebase, don't merge, when pulling.** `daily.yml` commits the binary
  `data/screener.db` to `main` on every scheduled run. If you have local
  commits, `git pull --rebase` — a merge produces a binary conflict nobody can
  resolve by hand.
- **Never commit `data/`.** It's gitignored for exactly this reason; CI
  force-adds it, and only on `main`.

**Tests** are offline by design — no network, no browser, no credentials:

```bash
python -m pytest tests/ -q
```

They run on Python 3.10, 3.11 and 3.12 for every pull request. Mock Playwright
rather than driving it, so a Morningstar redesign can't turn the suite red.

---

## Where the data goes

Everything lands in `data/`:

- **`screener.db`** — SQLite, the source of truth. Three tables:
  `rsi_history` (one row per symbol per day, including that day's YoY EPS
  growth), `valuations` (Morningstar price and fair value per day), `signals`
  (every completed pattern, fired or not, with both grading factors as they
  stood on the day it completed).
- **`latest.csv`** — current snapshot, one row per symbol.
- **`signals.csv`** — running log, appended whenever a pattern completes.
- **`dashboard.html`** — the generated page (rebuilt on demand, not committed).

Hand-checked fair values live outside `data/`, in `fair_values.yaml` at the
repo root — see "Where fair values are stored".

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
SYMBOL  CROSS 1     DIP         CROSS 2       PRICE  FAIR VAL  EPS GROWTH  RESULT
IBM     2026-07-16  2026-07-22  2026-07-23   217.20    225.00       +8.4%  STRONG BUY (all known factors confirm)
ORCL    2026-07-15  2026-07-21  2026-07-27        -         -           -  BUY SIGNAL (fair value unchecked)
```

Recording a fair value by hand applies it to every pattern for that symbol
still waiting on one, so a backfilled pattern from months ago can be resolved
today. It never un-fires a signal that already went out.

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

**Earnings growth** isn't configurable the way the above are — there's no
threshold to tune. It's YoY EPS growth (trailing twelve months, falling back to
fiscal-year for a stock too newly listed to have one — SanDisk's case right
after its 2025 spin-off) read from TradingView's scanner endpoint, the same
unauthenticated one RSI comes from. Any positive number confirms; the sign is
the only question being asked, same as the valuation gate's plain boolean.

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
python -m pytest tests/ -q      # 201 tests
```

CI runs them on every push and pull request against Python 3.10, 3.11 and
3.12 (`.github/workflows/tests.yml`), plus a check that `config.yaml` loads
and the dashboard builds from an empty database.

They cover the RSI maths (pinned against TradingView's own published value),
the crossing and window logic including the exact boundary cases, both
directions of the valuation gate, the storage upsert and re-scoring rules,
the Morningstar page parsing, the fair-value file's parsing and rejection
rules, and the dashboard's state model — including the guarantee that an
a pattern only earns the rocket when a real fair value backs it up.

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
- **RSI period is 14 or 7** — the only two TradingView publishes. Config
  rejects anything else rather than mixing two different indicators in one series.
