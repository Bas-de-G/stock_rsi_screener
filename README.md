# Stock RSI screener

Watches a list of stocks and flags a signal when a double crossing appears:

> **Buy** — RSI drops below 30, climbs back through it, drops below again, and
> climbs back through a second time.
>
> **Sell** — the mirror: RSI rises above 70, falls back through it, rises
> above again, and falls back through a second time.

A signal counts as **live** only while both crossings sit inside the
timeframe's lookback measured from *now*, and RSI is still on the signalling
side of the line. A pattern that completed in March is history, not something
you can act on in August — the `signals` table keeps every one ever found, but
the dashboard shows only what's actionable.

Morningstar's fair value then grades it: a live signal far enough from fair
value is flagged a **strong buy 🚀** (or **strong sell 🔻**).

Data comes from two places:

| What | Source | Login needed |
|---|---|---|
| RSI (14) | TradingView | no |
| Price and fair value | Morningstar | **yes** — subscriber-only, so v1 checks it by hand |
| Historical closes (for backfill) | Yahoo Finance | no |

Tracks **253 tickers** out of the box across five market groups you can switch
between on the dashboard — **S&P 500**, **NASDAQ**, **Europe** (Amsterdam +
London), **Asia** and **Under $10**. A ticker can sit in more than one: Apple
is both S&P 500 and Nasdaq-listed. Every entry was checked against both data
sources first, so none of them 404. Push a new ticker to `main` and the next
scheduled run backfills its history automatically — nothing to run or push by
hand.

`screener universe` proposes more from an index, deduplicated — see
[Growing the watchlist](#growing-the-watchlist).

Non-US listings work too, they just need their own identifiers:

| Market | TradingView | Yahoo | Morningstar | Currency |
|---|---|---|---|---|
| US | `NASDAQ:` / `NYSE:` | bare ticker | `xnas/` / `xnys/` | USD |
| Amsterdam | `EURONEXT:ASML` | `ASML.AS` | `xams/asml` | EUR |
| London | `LSE:RR.` | `RR.L` | `xlon/rr.` | **GBX** |

Two traps worth knowing about. TradingView uses one `EURONEXT:` prefix for the
whole exchange group rather than a per-city code — `AMS:` and `XAMS:` both
404. And Rolls-Royce is quoted in **pence**, not pounds, which is why
`currency` exists at all: a bare `1413.6` next to a dollar price would be
wildly misleading, so the dashboard prints the `GBX` label alongside it.

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

## Timeframes

The same pattern is screened on four RSI horizons, each its own page on the
dashboard. Three things scale with the holding period:

| Horizon | Two crosses within | Fair value must be above price by | Suggested leverage |
|---|---|---|---|
| 1 hour  | 2 days  | 10% | 10x |
| 4 hours | 5 days  | 20% | 5x  |
| 1 day   | 14 days | 30% | 2x  |
| 1 week  | 90 days | 50% | 1x  |

The cross window has to be per-horizon or the rule stops meaning anything — a
flat 14 days is only two weekly bars, so a 1w pattern could never form. The
margin rises with the horizon because a longer hold wants more headroom, and
the leverage falls for the same reason.

All four are tunable in `config.yaml` under `horizons:`. Set every `margin` to
0 and you get exactly the original "is it below fair value" gate back.

> **On the leverage figure:** it's a fixed number attached to the timeframe,
> not something calculated from the signal's quality. Leverage multiplies
> losses exactly as readily as gains, and 10x on an hourly signal is
> aggressive by any standard. The dashboard carries the same caveat.

Data collection runs **every 30 minutes on weekdays, 07:00–21:00 UTC**, plus two
probes during the Hong Kong session. That's what makes the intraday horizons
meaningful — a once-a-day sample of an hourly RSI is up to 24 hours stale by the
time you read it. The window starts at the European open because the watchlist
is no longer US-only.

---

## Signal strength

An RSI pattern is a **buy signal on its own** — no fair value needed. Two
independent factors then *grade* it, and either can upgrade it to a
**strong buy 🚀** on its own:

- **Morningstar fair value** — *required* for a rocket. It's the thesis: the
  reason to think the stock is worth more than it costs. A signal nobody has
  valued is never strong, however well the company is doing.
- **Earnings growth** — a **veto**, not a second opinion. Pulled automatically
  from TradingView's scanner. Unknown costs nothing; known-and-disagreeing
  withholds the rocket. On a sell it inverts: shrinking earnings *back* the
  sell.

That asymmetry is deliberate. Earnings growth exists to catch the value trap —
cheap by fair value, but earnings shrinking — not to promote a stock nobody
has valued.

Neither factor is required to fire a signal — that's still the RSI pattern
alone (or `fire_without_valuation: false` for strict mode, below). They only
decide how much conviction the dashboard shows:

| On the dashboard | What it means |
|---|---|
| **Strong buy 🚀** | Live pattern, **and** the fair value confirms, **and** nothing else known contradicts it |
| **Buy signal** | Live pattern, but no fair value recorded — or one that disagrees |
| **Strong sell 🔻** / **Sell signal** | The same two tiers on the overbought side |
| **Suspended ⚠️** | A real pattern, but results are days away — see below |
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

### Rule #1, run backwards

Phil Town's method, on every card. Run forwards — pick a growth rate, compute a
sticker price, compare — it marked **204 of 226 companies red**. That is
faithful to Phil, who expects to find a handful of businesses a year, and
useless as a screen: a verdict that says "no" 90% of the time carries almost no
information and cannot rank the 204.

So it runs **backwards**. Instead of asking what a company is worth at a rate we
picked, it asks what rate today's price already demands:

> **Price demands 15.3% a year for ten years · this company has delivered 2.6–7.6%**

That sentence needs no verdict to be useful. It is then scored against what the
company has actually delivered, expressed as a **range** rather than a point:

| | |
|---|---|
| **conservative** | the lowest of its growth rates — the pessimistic case |
| **base** | the median, capped at sales growth + 5pp — the central case |
| **headroom** | base minus what the price demands, in percentage points |

Headroom is continuous, so it ranks. On the live watchlist the score fills all
ten buckets with a median of 5, and the bands land at **18% green / 39% amber /
43% red**.

The two valuations run side by side. Morningstar's number is analyst-vetted and
independent; Rule #1 is a formula dominated by one assumption — `MOS ≈ EPS ×
(1+g)¹⁰ × P/E ÷ 8`, where g at 10% versus 15% moves the answer about 2.4×. The
card therefore shows a score, the rate demanded and the range delivered, never a
price to act on. `recommendations.csv` records both so it can eventually be
settled.

**Where it departs from the book**, since three of Phil's inputs are in no free
feed:

| His input | Substitute | Why |
|---|---|---|
| Equity (book value) growth, or analysts' estimate — the lower | Lowest of trailing EPS growth, full-year EPS growth **and sales growth** | No feed serves historical book value. Sales is in there because earnings can be engineered: without it AT&T scored a fictitious 20%/yr off one good year. |
| Historical average P/E, capped at 2× growth | 2× growth, held between 8 and 25 | No historical average available, and current P/E is worst here — signals fire when a stock is oversold, i.e. at its multiple's trough. `2 × 0%` is a P/E of zero, hence the floor. |
| Normalised EPS | Trailing EPS, with a large jump flagged | Can't normalise from here. LYFT reads +3,166% trailing growth — one year in the costume of a decade. Flagged readings can't be green. |

Two further corrections. The **base case is capped at sales growth + 5pp**,
because earnings cannot outgrow sales for a decade — without it AT&T's median
reads 20% off the same one good year and scores 9/10 green. And growth is capped
at **20%, not 15%**: capping at exactly the required return makes the model
degenerate, since the `(1+g)¹⁰` and the `1.15¹⁰` cancel and every fast grower
lands at a sticker identical to its market price — all 226, exactly 0.0% away.

The **Big Four**, not five: ROIC, EPS growth, sales growth and cash-flow growth
each clearing 10%. The fifth is equity growth, and counting an absent test as a
pass would flatter every company equally. Quality shifts the score by up to
1.5 points either way; it does not set it.

27 of 253 get **no reading at all** — no positive earnings to project. Refusing
is a real answer; refusing *everything* was the bug.

On the card it is **one small coloured box**, between the fair value it seconds
and the earnings growth it partly rests on:

> **`10/10`  Buffett score · worth 761.63   ✓ agrees with fair value**

Called the *Buffett score* rather than *Rule #1* because that is what it is —
Phil Town's method is an explicit mechanisation of Buffett's — and because
"Rule #1" means nothing to anyone who hasn't read the book, whose title stays in
the tooltip. Written `10/10` rather than `10` because a bare score says neither
its scale nor whether high is good; in finance it is as often a risk rating.

The price rides on the same line, and it is one number. The sticker started as
a low–high band with the margin-of-safety price beside it and a footnote naming
both growth rates — five numbers and two pieces of jargon to say *this company
looks worth about seven hundred*. The caveat behind the band is real (a sticker
is one growth assumption compounded ten times, and the pessimistic and base-case
readings can differ by a factor of nineteen) but it is not what someone scanning
twenty cards on a phone needs shouted at them.

So hover for the reasoning: the rate the price demands, both growth rates, the
margin-of-safety price, the Big Four count — and, where the two assumptions
disagree by more than 3x, a sentence saying what the pessimistic case gives
instead and to trust the score rather than the price. A second opinion that
ranks the page and decides nothing should not out-shout the verdict beside it.

No price is shown at all for a flat or shrinking earner — 71 of the cards. Both
growth rates floor at zero and the future P/E floors at 8, so such a company
prices out at `EPS × 8 / 1.15¹⁰` — a P/E of 2, whatever the company is. The
score still stands, because it is built on implied growth rather than on that.

**What it does with that verdict: ranks, never gates.** Rule #1 cannot add or
remove a 🚀. It sorts cards *within* their category — the rocket category holds
Rule #1 scores from 2 to 10 today and treated them identically — breaks ties for
the deal of the day, and marks the rare cards where the two valuations agree
(3 of 15 live strong buys) — beside the score, where it can name both parties,
rather than as a bare "both agree" badge that named neither. A company Rule #1 cannot read ranks mid-table, not
last: no opinion is not a bad opinion.

It stays that way until it has been measured, and measuring it can only happen
forwards. Its inputs are *current* fundamentals with no history behind them, so
Rule #1 is no more backtestable than fair value is. `recommendations.csv`
records the score on every published verdict from today, and that is the
sample — months of it, not weeks.

### Earnings suspend a signal

RSI cannot tell an ordinary correction from positioning ahead of results. Both
look like a stock going oversold; only one is a technical setup. The other gaps
on the release whatever the chart said.

So from **three trading sessions before** a results date until a session has
closed **after** it, the signal is suspended: the card stays, with an amber
badge saying when results land, but it earns no rocket, cannot be the deal of
the day, and sends no alert.

> ⚠️ Earnings tomorrow — sell signal suspended

Symmetrical, because an overbought stock gapping *down* on results is the same
risk from the other side. The dates come from TradingView in the same request
as the RSI, so this costs nothing. A ticker the feed has no date for is never
suspended — refusing to signal on every stock whose calendar we can't see would
quietly disable the screener.

Three sessions rather than two because the count is in weekdays: real trading
days only exist for the past, so a market holiday inside the window makes the
release one session closer than the count says. Being a day early costs a
deferred signal; being a day late means holding through the gap.

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

### 1. Build RSI history

```bash
python -m screener.cli backfill --range 1y
```

Without this the tool would have to watch for 14+ days before it could
recognise a pattern. Backfill computes RSI from a year of real daily closes,
so signals work from day one.

The computed RSI matches TradingView's exactly — same Wilder smoothing,
verified to the cent (see `tests/test_rsi.py`).

Safe to run any time, not just once: it skips any ticker that already has a
full chart's worth of history, so re-running it after adding a ticker to
`config.yaml` only fetches the new one. (`--force` refetches everyone anyway.)
This is also why the daily CI job calls it unconditionally on every scheduled
run — a newly added ticker backfills itself on the next run, with nothing to
push by hand.

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

Writes one self-contained page per horizon — `index.html` (1 day) plus
`1h.html`, `4h.html` and `1w.html` — with no JavaScript, no CDN, and no fonts
to fetch. The market filter across the top is pure CSS (hidden radio buttons),
so it works from a `file://` URL and with JavaScript disabled; the timeframe
selector is plain links between the four pages, because each horizon has
genuinely different data behind it. Open it locally, mail it, drop it in Dropbox, or publish it
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

## The Historical Dashboard

The four screener pages say what to look at. `history.html` says whether to
believe them, and it is linked from every one.

Every past recommendation is rebased to **100** on the day it fired and drawn
forward for 60 trading days, so a $5 stock and a $500 one share an axis. The
heavy line is the cohort average, the dashed one a randomly-timed entry over
the same span. Pick a **cohort** (strong buy by default; buy and sell sit
behind *Other signals*) and a **timeframe**, and the panel redraws — with no
JavaScript, because the selectors are radio buttons and CSS sibling rules.

**The lines are numbered, and the table underneath is the key.** The twelve
most recent recommendations get a number at the end of their line and a row
below with the symbol, the date and how it has done; the rest are drawn faintly
behind them for the shape of the cohort. One company per number — an intraday
pattern completes on almost every run, so the twelve newest 1h strong buys were
eight companies from a single afternoon with four of them listed twice. Only
the key is deduplicated; every figure on the page still runs over the whole
sample.

**Recommendations still inside their twenty days are listed too**, dotted
rather than coloured, showing the return so far and how far through they are.
They are excluded from the cohort statistics, which need the full window to
compare like with like. This used to be a filter on "has a +20d return", which
sounds like tidiness and meant the newest row on the page was always a month
old — on the 1h panel, where every drawn line is days old, nothing recent
appeared at all. The page looked stale while working perfectly.

```bash
python -m screener.cli evaluate                       # measure every past pattern
python -m screener.cli backtest --bars 20             # hit rate vs a random entry
python -m screener.cli backtest --bars 60 --csv o.csv # ...and export the rows
```

Nothing is captured for this. Forward returns are derived from price history
already on disk, so the first run measures the whole back catalogue and
re-running gives the same answer.

**Read the edge column, not the mean.** Equities drift upward, so any long
strategy beats zero over a rising sample — including entries chosen by a coin.
The baseline row *is* that coin. On the current data, at 20 trading days:

| Cohort | n | Hit rate | Mean | Edge |
|---|---|---|---|---|
| Random entry | 7,490 | 50.4% | +1.4% | — |
| Buy, all timeframes | 1,692 | 54.7% | +1.6% | **+0.3%** |
| Sell, all timeframes | 1,901 | 45.0% | −2.7% | **−4.0%** |

So the buy side has a small edge and the sell side has been actively wrong —
consistently, on every timeframe. That is roughly what you would expect of a
mean-reversion short in a market that spent the period rising, and it is the
first thing worth testing further rather than a verdict.

Two things these numbers are **not**. The watchlist is today's companies, all
still listed and still large, so the sample quietly excludes everything that
failed. And there is no "strong buy" cohort: fair values only exist from
2026-07-27, and re-scoring back-applies today's to every old pattern, so
splitting this sample by valuation would be reading the future.

That last comparison is what `recommendations.csv` is for. Every verdict the
dashboard publishes is appended to it once, as it stood at the time, and never
rewritten — including the ones suspended for earnings that nobody was alerted
to, because those are what make "did suspending them help?" answerable. It
grows forward from now, and it is the honest sample.

---

## Growing the watchlist

```bash
python -m screener.cli universe                      # propose S&P 500 / NASDAQ 100 names
python -m screener.cli universe --limit 50 --write   # append the 50 largest to config.yaml
python -m screener.cli universe --market netherlands --indexes "STOXX Europe 600"
```

It prints `config.yaml` lines and, with `--write`, appends them — leaving the
file's comments intact, because a YAML round-trip would erase them. Review with
`git diff config.yaml` before committing.

**It only ever adds.** A ticker that has left an index keeps its entry, its
history and its card. Dropping it would take its chart and its recorded
patterns with it, and those are the record the screener gets judged against.

Three kinds of duplicate get removed first, and all three are real:

| | |
|---|---|
| the same company abroad | NVIDIA is also `MUN:NVD`, `EUROTLX:4NVDA`, `SIX:NVDA.USD` — Germany's scanner alone returns 15,079 "stocks", mostly foreign listings |
| the same company's other share class | Alphabet is GOOGL *and* GOOG; the more traded one is kept |
| not a share at all | `NASDAQ:GOOGN` is preferred stock reporting Alphabet's market cap against its own $48 price |

Index membership and share class are read from TradingView's own `indexes` and
`typespecs` fields rather than inferred, so a NYSE company outside the S&P 500
isn't labelled as being in it.

**Adding many at once is paced.** RSI is one batched request however long the
list gets, but `backfill` is one Yahoo request per ticker *per horizon* and
can't be batched — 100 new names is 400 requests. `backfill --max-new 25` (what
CI runs) seeds 25 a run, so they arrive over a few runs instead of timing one
out. Already-seeded tickers are untouched by the cap, and their intraday
history is refreshed about once a day rather than every run.

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
python -m screener.cli scrape              # tickers with a signal, most urgent first
python -m screener.cli scrape --limit 40   # cap the session; the rest wait
python -m screener.cli scrape --dry-run    # see what it would visit first
python -m screener.cli scrape --push       # ...and commit + push the result
```

**It only visits tickers with a signal.** A fair value only changes anything
when a pattern has fired — it's what upgrades a plain buy to a strong one. A
ticker sitting at RSI 60 with no pattern gains nothing from being scraped. Use
`--all` to override, or `--symbols IBM,NVDA` to check something specific.

**And it visits them in order of how much they matter today**, which is what
makes `--limit` safe. Three tiers:

| | |
|---|---|
| **just fired** | becomes a strong buy today if the valuation confirms |
| **live signal** | on a dashboard page right now |
| **signal on file** | inside the chart window; worth reading ahead, since a value is cached 14 days |

Within a tier, the most recently completed pattern leads. On the current
watchlist that's 10 / 41 / 80 — so `--limit 40` covers every signal that just
fired plus most of the live ones, and leaves the read-ahead work for the next
session. Without the ordering a cap would spend its budget on whatever the
config file happened to list first.

With ~8s per page plus the pacing gap, budget roughly **13 seconds a ticker**:
40 pages is about 9 minutes.

Results are written to `fair_values.yaml`, the same file you'd edit by hand —
so scraped and hand-checked values flow through identical code, and every
change shows up as a readable diff.

### Sharing the results

`scrape` does **not** push by default. It writes the file and stops, so you can
look at the diff first:

```bash
git diff fair_values.yaml
```

**It skips anything checked in the last 14 days.** Morningstar revises a fair
value on earnings or a thesis change, so re-reading the same page days later
almost always returns the number already on file. `--force` re-reads anyway,
`--max-age N` changes the window.

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

**Things that will bite you otherwise:**

- **`git pull` refuses, complaining `data/screener.db` would be overwritten.**
  The most common trigger: you ran `backfill`, `run`, or `dashboard` locally,
  which modified that tracked file, and now a plain pull won't clobber your
  uncommitted changes. Since the file is fully regenerable — running the
  command again reproduces it — the fix is to discard your local copy, not
  merge it:
  ```bash
  git checkout -- data/screener.db data/latest.csv data/signals.csv
  git pull
  ```
  Treat everything under `data/` as disposable on your machine; the copy
  committed to `main` by CI is the shared one.
- **Rebase, don't merge, if you have local *commits* touching `data/`.**
  `daily.yml` commits that same binary file on every scheduled run, so a merge
  produces a conflict nobody can resolve by hand — `git pull --rebase` instead.
- **Never commit `data/` yourself.** It's gitignored for exactly this reason;
  CI force-adds it, and only on `main`.

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

`screener.db` is committed, so it can't be allowed to grow forever — and it
only ever grew, because bars are upserted and never removed. Three years of
hourly history had accumulated behind a dashboard that draws ninety bars, at
which point the file was 78 MB and GitHub warns above 50 MB.

```bash
python -m screener.cli prune      # drop history nothing can read
```

This drops intraday bars older than *both* the chart window and the symbol's
first daily bar. Those two conditions together mean unreadable, not merely old:
the chart never plots back that far, and the forward-return measurement already
refuses to score a signal the daily series doesn't cover, so an hourly bar from
before the daily history prices a pattern whose outcome is unknowable. Both
halves matter — SPCX listed recently enough that its 4h history predates its
daily history, and testing the daily line alone shortened its chart from 90
bars to 76.

Checked by replaying a copy of the live database: 283,005 bars removed, every
dashboard page byte-identical, all 17,828 measured outcomes unchanged, 78 MB →
46 MB. It runs on every scheduled run and is a no-op once the backlog is gone.

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

## The conviction score

Every card carries a **Conviction** out of 10 with a small segmented bar on the
same line. Five factors, each reduced to a strength between 0 and 1, weighted by
`scoring.weights` in `config.yaml`:

| Factor | Full marks at | Default weight |
|---|---|---|
| Fair value | double the horizon's margin under fair value | 3.0 |
| Buffett score | 10/10 on Rule #1 | 2.0 |
| EPS growth | +30% year on year | 1.5 |
| Earnings timing | no release near (binary) | 1.0 |
| RSI depth | 6 points below the threshold at the dip | 1.0 |

Each segment of the bar is one factor's actual contribution — its weight times
how well it scored — so its width answers *why is this an 8?* and the empty
remainder answers *what would have made it a 10?* Hovering the score names
every factor and what it was worth, which is where the detail belongs: the card
is for scanning, the tooltip is for asking.

It appears in three places on the site: the score and bar on every card, a
**Conviction ≥7** count in each page's summary strip, and a **Conv.** column in
the Historical Dashboard's key, showing what each recommendation actually went
out with. That last one is read from `recommendations.csv`, never recomputed —
a score worked out today would use today's fair value and today's Rule #1
reading, and be a fact about the present dressed up as one about the past. It
follows that rows published before the score existed show an em dash rather
than a number, and the column fills in from here.

**It decides nothing.** The rocket and the phone alert still come from the older
rule (fair value required, everything else a veto). That rule has a track record
on the Historical Dashboard and this has none, so the score is computed, shown
and written into `recommendations.csv` alongside the weights that produced it —
and once it has run forward for a few months the two can be compared on
evidence. Re-weighting is a config edit; the weights in force are stamped into
every journal row, so a re-weighting shows up in the results rather than
silently rewriting the past.

**An unread factor is dropped and the rest reweighted, not scored zero.**
Otherwise "nobody has recorded a fair value" and "this stock is dear" would give
the same number, and 88 of the 253 names have no fair value on file. The cost is
that a name with one factor known still gets a confident-looking score, so a
card built on less than half the weighted evidence is marked **thin** and the
tooltip names what was missing. Only when it is genuinely thin — an earlier
version showed a coverage figure whenever *anything* was unreadable, which is
most cards, and a warning that is always on is not a warning.

The saturation points above were calibrated against the live watchlist, not
picked. Two first guesses were wrong by a lot: scoring the dip at 10 points put
97% of patterns under full marks, and subtracting the margin from the discount
scored 87% of the watchlist at exactly zero — a factor that is zero for seven
names in eight cannot rank them, which is the one job it has.

## Notifications

Only one thing is worth interrupting someone for: a **newly fired strong buy**.
Not every fired signal — `fire_without_valuation` means everything fires, which
is 5 to 20 patterns a run and 251 the day new tickers are backfilled. And only
while it's *fresh*, not merely live: a strong buy stays actionable for a
fortnight on the daily chart, and announcing it for a fortnight is spam.

**On your phone**, for as many people as you like, with no app to build. Uses
[ntfy](https://ntfy.sh) — free, no account, and adding someone is them
installing an app and typing a topic name.

1. Invent a long, unguessable topic name — `openssl rand -hex 12` is fine.
2. Add it as the repository secret `SCREENER_NTFY_TOPIC`
   (*Settings → Secrets and variables → Actions*).
3. Everyone installs ntfy ([iOS](https://apps.apple.com/app/ntfy/id1625396347),
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
   taps **+**, and subscribes to that topic.

Alerts arrive titled with the ticker and the discount, and tap through to the
right dashboard page. Locally, put the same name in `.env` to test.

> The topic name **is** the password — ntfy has no other access control, and on
> a public repo an Actions secret is readable by anyone who can push a
> workflow. Worst case is a stranger reading which stocks the screener likes.
> To rotate: change the secret, re-subscribe in the app.

**By email, with no credential anywhere.** The scheduled run also opens a GitHub
issue; GitHub emails everyone watching the repository. Watch it with **Watch →
Custom → Issues** and check email notifications are on in your GitHub settings.
Nothing to configure in the repo — Actions injects its own token, scoped to
this repository and expiring with the run.

**By Slack or Discord**, optionally, with an incoming webhook in `.env`:

```
SCREENER_WEBHOOK_URL=https://hooks.slack.com/services/...
```

The three are independent; set any, all, or none. Without any of them
everything still lands in SQLite, the CSVs, and stdout.

### One signal, one email

A strong buy sits on the dashboard for hours and runs land every 30 minutes, so
the same pick would otherwise arrive four or five times in an afternoon. It's
deduplicated twice over:

- **`notifications.json`** records what's been announced, keyed by the pattern
  itself — kind, timeframe, symbol, and the second RSI cross that completed it.
  It's committed on every run, and deliberately *not* stored in
  `data/screener.db`: that file is only committed by the last run of the day,
  so a record written at 14:00 would be gone again by 14:30. That was a real
  bug, and it's why this file exists.
- **GitHub itself** is asked whether an issue for that exact signal already
  exists, matching on a hidden marker in the body rather than the title (the
  title quotes a discount that moves with the price). This is the
  authoritative check, so even a lost ledger doesn't produce a second email.
  A closed issue counts as filed — reading and closing one is not a request to
  resend it.

The dedupe sits above the transports, so the phone push and the webhook inherit
it rather than each carrying their own copy.

Only if the API can't be reached does it err towards sending: a repeat email is
an annoyance, a missed strong buy defeats the point.

### One name, one alert a session

Both checks above are keyed on the *pattern*, which was correct and turned out
not to be enough. Intraday bars are stamped with the minute the run happened, so
a genuinely new pattern can complete on almost every half-hourly run. ANET
announced itself eleven times on the 1 hour chart in six hours on 19 August, and
every one of those alerts passed both checks honestly. Thirty alerts that day
were really about a dozen opportunities.

So a symbol also stays quiet on a timeframe for **12 hours** after it's been
announced, however many new patterns complete in the meantime. Replaying the 58
alerts on file, that turns them into 34 — the worst day drops from 30 to 13 —
without losing a single distinct opportunity.

Twelve hours rather than a day, and the difference is not arbitrary: anywhere
from 8 to 18 hours gives exactly the same result, but at 24 hours four names
that came back the following morning (18 to 21 hours later) are silenced,
because the market opens at roughly the same time every day. Twelve sits in the
middle of that plateau — longer than a trading session, shorter than the gap to
the next one.

A held signal isn't written to the ledger. Recording it would push the quiet
period forward on every run and the name would never be heard from again.

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
python -m pytest tests/ -q      # 587 tests
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
