"""The page that says whether any of this has worked.

Deliberately a separate page from the four horizon dashboards. Those answer
"what should I look at today"; this one answers "should I believe them", and
mixing the two would let a good-looking card borrow authority from a track
record it has not earned.

Everything here is read from `outcomes`, which `evaluate` derives from price
history. The one number that carries the argument is the random-entry baseline:
equities drift upward, so any long strategy posts a hit rate above half and a
positive mean over a rising sample. A cohort is only interesting to the extent
it beats the coin, so the coin is on the same table, first.
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from .config import DEFAULT_HORIZON, Config
from .dashboard import _CSS
from .outcomes import FORWARD_BARS, baseline_outcomes, summarise
from .signals import BUY, SELL
from .storage import Store

# The window the page leads with. A month is long enough for a mean-reversion
# entry to have resolved and short enough that the sample is not mostly
# unmeasured.
HEADLINE_BARS = 20


def build_scoreboard(store: Store, config: Config, output: Path) -> Path:
    """Write history.html next to the dashboards."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_scoreboard(store, config), encoding="utf-8")
    return output


def _cohorts(store: Store, config: Config, bars: int):
    """(label, outcomes) for the baseline and every direction × horizon."""
    measured = store.all_outcomes(bars=bars)

    base = []
    for ticker in config.tickers:
        closes = [
            (p.date, p.close) for p in store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
        ]
        base.extend(baseline_outcomes(ticker.symbol, closes, bars=(bars,)))

    rows = [("Random entry", "baseline", base)]
    for direction in (BUY, SELL):
        for horizon in config.horizons:
            rows.append((
                f"{direction.capitalize()} · {horizon.label}",
                direction,
                [o for o in measured
                 if o.direction == direction and o.horizon == horizon.key],
            ))
        rows.append((
            f"{direction.capitalize()} · all timeframes", f"{direction} total",
            [o for o in measured if o.direction == direction],
        ))
    return rows


def _row_html(label: str, kind: str, outcomes, baseline: dict) -> str:
    s = summarise(outcomes)
    if not s["n"]:
        return (f'<tr class="k-{kind}"><th scope="row">{html.escape(label)}</th>'
                f'<td colspan="5" class="none">not measured yet</td></tr>')

    # Against the coin, not against zero. A cohort that returns +1.5% where
    # random entry returned +1.4% has found essentially nothing.
    edge = s["mean"] - baseline.get("mean", 0.0)
    edge_class = "good" if edge > 0.005 else "bad" if edge < -0.005 else ""
    return f"""<tr class="k-{kind}">
  <th scope="row">{html.escape(label)}</th>
  <td class="num">{s['n']:,}</td>
  <td class="num">{s['hit_rate'] * 100:.1f}%</td>
  <td class="num">{s['mean'] * 100:+.1f}%</td>
  <td class="num">{s['median'] * 100:+.1f}%</td>
  <td class="num {edge_class}">{edge * 100:+.1f}%</td>
</tr>"""


def render_scoreboard(store: Store, config: Config) -> str:
    generated = dt.datetime.now().strftime("%d %B %Y, %H:%M")
    cohorts = _cohorts(store, config, HEADLINE_BARS)
    baseline = summarise(cohorts[0][2])

    body_rows = "\n".join(
        _row_html(label, kind, outcomes, baseline) for label, kind, outcomes in cohorts
    )

    windows = ", ".join(f"+{b}" for b in FORWARD_BARS)

    def page_for(h) -> str:
        return "index.html" if h.key == DEFAULT_HORIZON else f"{h.key}.html"

    links = "".join(
        f'<a class="tf" href="{page_for(h)}">{html.escape(h.label)}</a>'
        for h in config.horizons
    ) + '<a class="tf on" href="history.html">Track record</a>'

    body = f"""<div class="sheet">
<header class="masthead">
  <div class="title-block">
    <p class="eyebrow">Relative Strength Screener · Track record</p>
    <h1>Has this worked?</h1>
    <p class="standfirst">
      Every pattern the screener ever recorded, measured against the
      <strong>{HEADLINE_BARS} trading days</strong> that followed it. Returns are
      signed to the call, so a sell followed by a fall counts as a win exactly
      as a buy followed by a rise does.
    </p>
    <nav class="timeframes" aria-label="Page">{links}</nav>
  </div>
</header>

<p class="crosses">
  Read the <strong>edge</strong> column, not the mean. Equities drift upward, so
  any long strategy beats zero over a rising sample — including entries chosen
  by a coin. Edge is what a cohort returned <em>above</em> that coin.
</p>

<table class="record">
  <thead>
    <tr><th scope="col">Cohort</th><th scope="col">Signals</th>
        <th scope="col">Hit rate</th><th scope="col">Mean</th>
        <th scope="col">Median</th><th scope="col">Edge</th></tr>
  </thead>
  <tbody>
{body_rows}
  </tbody>
</table>

<div class="caveats">
  <h2>What these numbers are not</h2>
  <ul>
    <li><strong>Survivorship.</strong> The watchlist is today's companies —
        all still listed, all still large. A sample that quietly excludes
        everything that failed reads optimistically.</li>
    <li><strong>No valuation split.</strong> Fair values only exist from
        2026-07-27, and re-scoring back-applies today's to every old pattern,
        so a “strong buy” cohort here would be reading the future. That
        comparison starts from <code>recommendations.csv</code> and grows
        forward from now.</li>
    <li><strong>No costs.</strong> No spread, no commission, no slippage, and
        an entry at the close of the bar the pattern completed on.</li>
    <li><strong>One market regime.</strong> Measured over a period equities
        spent mostly rising. A short cohort losing money in a bull market is
        what you would expect, not proof it is wrong forever.</li>
  </ul>
  <p>Also measured at {windows} trading days in the <code>outcomes</code>
     table. <code>screener backtest --bars 60 --csv out.csv</code> exports any
     of them.</p>
</div>

<footer class="colophon">
  <p>Generated {generated} · Outcomes derived from Yahoo Finance daily closes ·
     Recomputed on every run, never captured</p>
  <p class="disclaimer">A back-tested edge is not a forecast. Nothing here is
     financial advice.</p>
</footer>
</div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSI Screener · Track record</title>
<style>{_CSS}{_EXTRA_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


# Only what the dashboard's stylesheet has no equivalent for. Everything else --
# the palette, the masthead, the timeframe nav -- is reused, so the two pages
# stay recognisably the same publication.
_EXTRA_CSS = """
.record { width: 100%; border-collapse: collapse; margin: 18px 0 28px; font-size: 14px; }
.record th, .record td { padding: 9px 10px; border-bottom: 1px solid var(--rule); }
.record thead th {
  font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); text-align: right; font-weight: 600;
}
.record thead th:first-child { text-align: left; }
.record tbody th { text-align: left; font-weight: 600; }
.record .num { text-align: right; font-variant-numeric: tabular-nums; }
.record .none { text-align: right; color: var(--ink-3); font-style: italic; }
.record .good { color: var(--green); font-weight: 700; }
.record .bad { color: var(--crimson); font-weight: 700; }
.record .k-baseline th, .record .k-baseline td {
  color: var(--ink-2); border-bottom: 2px solid var(--rule);
}
.record tr[class$="total"] th, .record tr[class$="total"] td { font-weight: 700; }
.caveats { margin: 8px 0 24px; }
.caveats h2 { font-size: 13px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); margin: 0 0 10px; }
.caveats ul { margin: 0 0 14px; padding-left: 18px; }
.caveats li { margin-bottom: 7px; font-size: 14px; line-height: 1.5; }
.caveats p { font-size: 13px; color: var(--ink-2); }
@media (max-width: 620px) {
  .record { font-size: 12.5px; }
  .record th, .record td { padding: 7px 5px; }
}
"""
