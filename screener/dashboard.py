"""Builds the shareable HTML dashboard.

One self-contained file: no CDN, no external fonts, no JavaScript needed to
read it. That matters because the page is meant to be opened from disk, mailed
around, or published as-is.

Charts are inline SVG generated here rather than by a charting library — the
whole plot is a polyline, a threshold rule and a handful of markers, which is
less code than configuring a library and has no runtime dependency.
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .signals import find_upward_crosses, is_strong, valuation_passes
from .storage import RsiPoint, Signal, Store, Valuation

# Plot geometry, in SVG user units.
_W, _H = 460.0, 150.0
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 4.0, 4.0, 8.0, 14.0


@dataclass
class Row:
    """Everything the page needs about one ticker."""

    symbol: str
    morningstar_url: str
    tradingview_url: str
    series: list[RsiPoint]
    crosses: list[int]
    valuation: Valuation | None
    signals: list[Signal]  # this symbol's patterns, ascending by date
    currency: str = "USD"

    @property
    def latest(self) -> RsiPoint | None:
        return self.series[-1] if self.series else None

    @property
    def rsi(self) -> float | None:
        return self.latest.rsi if self.latest else None

    @property
    def fired(self) -> bool:
        return any(s.fired for s in self.signals)

    @property
    def strong(self) -> bool:
        """Pattern fired and a recorded fair value backs it up."""
        return any(
            s.fired and is_strong(s.valuation_known, s.valuation_pass) for s in self.signals
        )

    @property
    def latest_signal(self) -> Signal | None:
        return self.signals[-1] if self.signals else None

    @property
    def state(self) -> str:
        """Bucket used for the status pill, the card's accent, and sort order."""
        if self.strong:
            return "strong"
        if self.fired:
            # A fired signal whose valuation was checked and disagreed is
            # still a signal — the fair value only grades it.
            checked = any(s.fired and s.valuation_known for s in self.signals)
            return "signal_checked" if checked else "signal"
        latest = self.latest_signal
        if latest is not None:
            return "rejected"
        if self.rsi is None:
            return "nodata"
        if self.rsi < 30:
            return "oversold"
        if self.rsi < 40:
            return "watch"
        return "neutral"


def build_dashboard(
    store: Store, config: Config, output: Path, standalone: bool = True
) -> Path:
    rows = _collect(store, config)
    html_text = render(rows, config, standalone=standalone)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    return output


def _collect(store: Store, config: Config) -> list[Row]:
    valuations = {v.symbol: v for v in store.latest_valuations()}
    signals = store.all_signals()
    window = config.dashboard.chart_days
    rows: list[Row] = []

    for ticker in config.tickers:
        series = store.rsi_series(ticker.symbol)[-window:]
        # Only show signals where the pattern completed (up2_date) falls within the chart window
        chart_start = series[0].date if series else None
        sigs = [
            s for s in signals
            if s.symbol == ticker.symbol and (not chart_start or s.up2_date >= chart_start)
        ]
        rows.append(
            Row(
                symbol=ticker.symbol,
                morningstar_url=ticker.morningstar_url,
                tradingview_url=(
                    "https://www.tradingview.com/symbols/"
                    f"{ticker.tradingview.replace(':', '-')}/technicals/"
                ),
                series=series,
                crosses=find_upward_crosses(series, config.rsi.threshold),
                valuation=valuations.get(ticker.symbol),
                signals=sigs,
                currency=ticker.currency,
            )
        )

    # Most actionable first: confirmed signals, then patterns awaiting a
    # fair-value check, then how oversold things currently are.
    order = {
        "strong": 0, "signal": 1, "signal_checked": 2, "rejected": 3,
        "oversold": 4, "watch": 5, "neutral": 6, "nodata": 7,
    }
    rows.sort(key=lambda r: (order[r.state], r.rsi if r.rsi is not None else 999))
    return rows


# ----------------------------------------------------------------- chart


def _x(i: int, n: int) -> float:
    if n <= 1:
        return _PAD_L
    span = _W - _PAD_L - _PAD_R
    return _PAD_L + span * i / (n - 1)


def _y(rsi: float) -> float:
    """RSI 0-100 mapped to the plot box, 100 at the top.

    The scale is fixed rather than fitted to each stock's range so the 30 line
    sits at the same height on every card — the whole point is comparing them
    at a glance.
    """
    span = _H - _PAD_T - _PAD_B
    return _PAD_T + span * (1.0 - max(0.0, min(100.0, rsi)) / 100.0)


# Morningstar analysts revise a fair value on earnings or a thesis change --
# call it quarterly. Past this, the number is still worth showing but shouldn't
# look as authoritative as one from last week.
_STALE_AFTER_DAYS = 90


def _freshness(val: Valuation) -> tuple[str, str]:
    """Human phrasing for how old a fair value is, plus a CSS class if it's stale.

    Reads `fair_value_date`, which carries the `checked:` date from the YAML
    file — `val.date` is when the row was last written, which is today on every
    run and so never looks old.
    """
    checked = val.fair_value_date
    if not checked:
        return f"Recorded {html.escape(val.date)}", ""

    try:
        age = (dt.date.today() - dt.date.fromisoformat(checked)).days
    except ValueError:
        # A hand-typed date that isn't ISO. Show it rather than guessing.
        return f"Checked {html.escape(checked)}", ""

    if age <= 0:
        text = "Checked today"
    elif age == 1:
        text = "Checked yesterday"
    elif age < 30:
        text = f"Checked {age} days ago"
    elif age < 60:
        text = "Checked about a month ago"
    else:
        text = f"Checked about {age // 30} months ago"
    return text, " stale" if age > _STALE_AFTER_DAYS else ""


def _chart_svg(row: Row, threshold: float) -> str:
    series = row.series
    if len(series) < 2:
        return '<p class="nodata">Not enough history yet — run backfill.</p>'

    n = len(series)
    points = " ".join(f"{_x(i, n):.1f},{_y(p.rsi):.1f}" for i, p in enumerate(series))
    area = (
        f"{_PAD_L:.1f},{_y(0):.1f} "
        + points
        + f" {_x(n - 1, n):.1f},{_y(0):.1f}"
    )
    y_thresh = _y(threshold)
    y_zero = _y(0)
    uid = f"g{row.symbol.lower()}"

    markers = "".join(
        f'<circle class="cross" cx="{_x(i, n):.1f}" cy="{_y(series[i].rsi):.1f}" r="3.4">'
        f"<title>Crossed {threshold:g} upward on {series[i].date} "
        f"(RSI {series[i].rsi:.1f})</title></circle>"
        for i in row.crosses
    )

    last = series[-1]
    end_dot = (
        f'<circle class="endpoint" cx="{_x(n - 1, n):.1f}" cy="{_y(last.rsi):.1f}" r="3">'
        f"<title>{last.date}: RSI {last.rsi:.1f}</title></circle>"
    )

    first_date = html.escape(series[0].date)
    last_date = html.escape(last.date)

    return f"""<svg class="plot" viewBox="0 0 {_W:.0f} {_H:.0f}" role="img"
     aria-label="RSI for {html.escape(row.symbol)} over {n} trading days,
                 currently {last.rsi:.1f}, crossing {threshold:g} upward
                 {len(row.crosses)} times">
  <defs>
    <linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--line)" stop-opacity=".20"/>
      <stop offset="100%" stop-color="var(--line)" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <rect class="oversold-band" x="0" y="{y_thresh:.1f}"
        width="{_W:.0f}" height="{(y_zero - y_thresh):.1f}"/>
  <line class="gridline" x1="0" y1="{_y(70):.1f}" x2="{_W:.0f}" y2="{_y(70):.1f}"/>
  <line class="threshold" x1="0" y1="{y_thresh:.1f}" x2="{_W:.0f}" y2="{y_thresh:.1f}"/>
  <text class="axis-label" x="3" y="{y_thresh - 4:.1f}">{threshold:g}</text>
  <text class="axis-label faint" x="3" y="{_y(70) - 4:.1f}">70</text>
  <polygon class="area" points="{area}" fill="url(#{uid})"/>
  <polyline class="line" points="{points}"/>
  {markers}
  {end_dot}
  <text class="axis-date" x="0" y="{_H - 2:.0f}">{first_date}</text>
  <text class="axis-date end" x="{_W:.0f}" y="{_H - 2:.0f}">{last_date}</text>
</svg>"""


# ----------------------------------------------------------------- markup


def _card(row: Row, config: Config) -> str:
    threshold = config.rsi.threshold
    rsi_text = f"{row.rsi:.1f}" if row.rsi is not None else "—"
    close_text = f"{row.latest.close:,.2f}" if row.latest else "—"
    ccy = "" if row.currency == "USD" else f' <span class="ccy">{html.escape(row.currency)}</span>' 

    pill_label = {
        "strong": "Strong buy 🚀",
        "signal": "Buy signal",
        "signal_checked": "Buy signal",
        "rejected": "Pattern, gate failed",
        "oversold": "Oversold",
        "watch": "Near threshold",
        "neutral": "Neutral",
        "nodata": "No data",
    }[row.state]

    crosses = len(row.crosses)
    cross_note = (
        f"<strong>{crosses}</strong> upward cross{'' if crosses == 1 else 'es'} of "
        f"{threshold:g} in {len(row.series)} sessions"
    )

    if row.valuation:
        val = row.valuation
        _, passed = _gate(val, config)
        verdict = "below fair value" if val.price < val.fair_value else "above fair value"
        gate_class = "pass" if passed else "fail"
        origin = "by hand" if val.source == "manual" else "scraped from Morningstar"
        age_text, age_class = _freshness(val)
        valuation_block = f"""
        <dl class="valuation {gate_class}">
          <div><dt>Fair value</dt><dd>{val.fair_value:,.2f}</dd></div>
          <div><dt>Price</dt><dd>{val.price:,.2f}</dd></div>
          <div><dt>Verdict</dt><dd>{verdict}</dd></div>
        </dl>
        <p class="provenance{age_class}">{age_text}, {origin}.</p>"""
    elif row.fired:
        valuation_block = """
        <p class="valuation pending">Buy signal on RSI alone — confirm the fair value
        for a strong buy.</p>"""
    else:
        valuation_block = """
        <p class="valuation none">No fair value recorded yet.</p>"""

    patterns = ""
    if row.signals:
        marks = ", ".join(
            f"{html.escape(s.up2_date)}"
            + (" ✓" if s.fired else " ✗" if s.valuation_known else "")
            for s in row.signals[-3:]
        )
        patterns = f'<p class="patterns">Pattern completed: {marks}</p>'

    symbol = html.escape(row.symbol)
    return f"""<article class="card state-{row.state}">
  <header class="card-head">
    <div class="ident">
      <h3>{symbol}</h3>
      <span class="pill">{pill_label}</span>
    </div>
    <div class="readout">
      <div class="metric"><span class="k">RSI</span><span class="v">{rsi_text}</span></div>
      <div class="metric"><span class="k">Close</span><span class="v">{close_text}{ccy}</span></div>
    </div>
  </header>
  {_chart_svg(row, threshold)}
  <p class="crosses">{cross_note}</p>
  {patterns}
  {valuation_block}
  <div class="actions">
    <a class="btn primary" href="{html.escape(row.morningstar_url)}"
       target="_blank" rel="noopener noreferrer">Check fair value on Morningstar</a>
    <a class="btn" href="{html.escape(row.tradingview_url)}"
       target="_blank" rel="noopener noreferrer">TradingView</a>
  </div>
  <p class="record-hint"><code>screener scrape --symbols {symbol}</code>
     or <code>screener fair-value {symbol} &lt;value&gt;</code></p>
</article>"""


def _gate(val: Valuation, config: Config) -> tuple[bool, bool]:
    return valuation_passes(val.price, val.fair_value, config.signal)


def render(rows: list[Row], config: Config, standalone: bool = True) -> str:
    threshold = config.rsi.threshold
    generated = dt.datetime.now().strftime("%d %B %Y, %H:%M")

    tracked = len(rows)
    oversold = sum(1 for r in rows if r.rsi is not None and r.rsi < threshold)
    patterns = sum(len(r.signals) for r in rows)
    strong = sum(1 for r in rows if r.strong)
    fired = sum(1 for r in rows if r.fired)
    dated = [r.latest.date for r in rows if r.latest]
    as_of = max(dated) if dated else "—"

    cards = "\n".join(_card(r, config) for r in rows)

    masthead = f"""<header class="masthead">
  <div class="title-block">
    <p class="eyebrow">Relative Strength Screener</p>
    <h1>RSI Screener</h1>
    <p class="standfirst">
      Tracking {tracked} market leaders for a double crossing of RSI
      {threshold:g} within {config.signal.window_days} days — the entry pattern.
      A signal whose Morningstar fair value also agrees is marked a strong buy.
    </p>
  </div>
  <dl class="aggregates">
    <div><dt>Session</dt><dd>{html.escape(as_of)}</dd></div>
    <div><dt>Tracked</dt><dd>{tracked}</dd></div>
    <div><dt>Below {threshold:g}</dt><dd class="{'hot' if oversold else ''}">{oversold}</dd></div>
    <div><dt>Patterns</dt><dd>{patterns}</dd></div>
    <div><dt>Signals</dt><dd class="{'warn' if fired else ''}">{fired}</dd></div>
    <div><dt>Strong 🚀</dt><dd class="{'good' if strong else ''}">{strong}</dd></div>
  </dl>
</header>"""

    body = f"""<div class="sheet">
{masthead}
<main class="grid">
{cards}
</main>
<footer class="colophon">
  <p>Generated {generated} · RSI ({config.rsi.period}) from TradingView, daily bars ·
     History from Yahoo Finance · Fair value from Morningstar</p>
</footer>
</div>"""

    head = f"<title>RSI Screener</title>\n<style>{_CSS}</style>"
    if not standalone:
        return head + "\n" + body
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{head}
</head>
<body>
{body}
</body>
</html>"""


_CSS = """
:root {
  --paper:      #FFFFFF;
  --card:       #FFFFFF;
  --grid-fine:  rgba(38, 86, 132, .075);
  --grid-major: rgba(38, 86, 132, .155);
  --rule:       rgba(22, 40, 60, .16);
  --ink:        #16283C;
  --ink-2:      #4C6076;
  --ink-3:      #8293A6;
  --accent:     #0E4C75;
  --crimson:    #A8231B;
  --green:      #14624A;
  --line:       #0E4C75;
  --band:       rgba(168, 35, 27, .07);
  --warn:       #8A6100;
  --shadow:     0 1px 2px rgba(16, 38, 60, .06), 0 8px 24px -16px rgba(16, 38, 60, .28);
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper:      #0B1219;
    --card:       #101A24;
    --grid-fine:  rgba(120, 180, 230, .065);
    --grid-major: rgba(120, 180, 230, .13);
    --rule:       rgba(190, 215, 235, .17);
    --ink:        #DCE7F0;
    --ink-2:      #93A7B9;
    --ink-3:      #6B7F92;
    --accent:     #6FB3E0;
    --crimson:    #E0736A;
    --green:      #4FBE92;
    --line:       #6FB3E0;
    --band:       rgba(224, 115, 106, .10);
  --warn:       #D9A441;
    --shadow:     0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
  }
}

:root[data-theme="dark"] {
  --paper:      #0B1219;
  --card:       #101A24;
  --grid-fine:  rgba(120, 180, 230, .065);
  --grid-major: rgba(120, 180, 230, .13);
  --rule:       rgba(190, 215, 235, .17);
  --ink:        #DCE7F0;
  --ink-2:      #93A7B9;
  --ink-3:      #6B7F92;
  --accent:     #6FB3E0;
  --crimson:    #E0736A;
  --green:      #4FBE92;
  --line:       #6FB3E0;
  --band:       rgba(224, 115, 106, .10);
  --warn:       #D9A441;
  --shadow:     0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
}

:root[data-theme="light"] {
  --paper:      #FFFFFF;
  --card:       #FFFFFF;
  --grid-fine:  rgba(38, 86, 132, .075);
  --grid-major: rgba(38, 86, 132, .155);
  --rule:       rgba(22, 40, 60, .16);
  --ink:        #16283C;
  --ink-2:      #4C6076;
  --ink-3:      #8293A6;
  --accent:     #0E4C75;
  --crimson:    #A8231B;
  --green:      #14624A;
  --line:       #0E4C75;
  --band:       rgba(168, 35, 27, .07);
  --warn:       #8A6100;
  --shadow:     0 1px 2px rgba(16, 38, 60, .06), 0 8px 24px -16px rgba(16, 38, 60, .28);
}

*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.sheet {
  max-width: 1180px;
  margin: 0 auto;
  padding: 40px 28px 64px;
}

@media (max-width: 720px) {
  .sheet { padding: 24px 14px 44px; }
}

/* ---------------------------------------------------------- masthead */

.masthead {
  display: flex;
  flex-wrap: wrap;
  gap: 32px;
  align-items: flex-end;
  justify-content: space-between;
  padding-bottom: 20px;
  border-bottom: 2px solid var(--ink);
}

.title-block { flex: 1 1 380px; max-width: 62ch; }

.eyebrow {
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--accent);
}

h1 {
  margin: 0;
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-size: clamp(30px, 4.5vw, 46px);
  font-weight: 400;
  letter-spacing: -.015em;
  line-height: 1.08;
  text-wrap: balance;
}

.standfirst {
  margin: 12px 0 0;
  max-width: 60ch;
  color: var(--ink-2);
  font-size: 14.5px;
}

.aggregates {
  /* Auto-flow by column keeps this a horizontal strip: as a flex child it
     has no width to divide up, so auto-fit would collapse it to one track. */
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(78px, auto);
  gap: 1px;
  margin: 0;
  border: 1px solid var(--rule);
  border-radius: 3px;
  background: var(--rule);
  overflow: hidden;
}

.aggregates > div {
  padding: 10px 14px;
  background: var(--card);
}

/* Six tiles: 3 and 2 columns both divide evenly, so no empty cell is left
   over the way auto-fit would leave one. */
@media (max-width: 720px) {
  .aggregates {
    width: 100%;
    grid-auto-flow: row;
    grid-template-columns: repeat(3, 1fr);
  }
  .aggregates > div { padding: 8px 12px; }
  .aggregates dd { font-size: 17px; }
}

@media (max-width: 480px) {
  .aggregates { grid-template-columns: repeat(2, 1fr); }
}

.aggregates dt {
  font-size: 10px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink-3);
}

.aggregates dd {
  margin: 2px 0 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 20px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;   /* never break a date mid-value */
}

.aggregates dd.hot { color: var(--crimson); font-weight: 600; }
.aggregates dd.warn { color: var(--accent); font-weight: 600; }
.aggregates dd.good { color: var(--green); font-weight: 600; }

/* -------------------------------------------------------------- grid */

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
  gap: 18px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px 18px;
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  box-shadow: var(--shadow);
}

.card.state-strong   { border-top: 3px solid var(--green); }
.card.state-signal,
.card.state-signal_checked { border-top: 3px solid var(--accent); }
.card.state-rejected { border-top: 3px dashed var(--ink-3); }
.card.state-oversold { border-top: 3px solid var(--crimson); }
.card.state-watch    { border-top: 3px solid color-mix(in srgb, var(--accent) 55%, var(--ink-3)); }

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.ident { display: flex; flex-direction: column; gap: 5px; }

.card h3 {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: .04em;
}

.pill {
  align-self: flex-start;
  padding: 2px 8px;
  border: 1px solid currentColor;
  border-radius: 2px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--ink-3);
}

.state-strong   .pill {
  color: var(--card);
  background: var(--green);
  border-color: var(--green);
}
.state-signal   .pill,
.state-signal_checked .pill { color: var(--accent); }
.state-rejected .pill { color: var(--ink-3); }
.state-oversold .pill { color: var(--crimson); }
.state-watch    .pill { color: var(--accent); }

.readout { display: flex; gap: 16px; text-align: right; }

.metric { display: flex; flex-direction: column; }

.metric .k {
  font-size: 10px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink-3);
}

.metric .v {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 19px;
  font-variant-numeric: tabular-nums;
}

.state-oversold .metric:first-child .v { color: var(--crimson); }

.ccy {
  font-size: 10px;
  letter-spacing: .06em;
  color: var(--ink-3);
  margin-left: 2px;
}

/* ------------------------------------------------------------- plot */

.plot {
  width: 100%;
  height: auto;
  display: block;
  /* Finer squares inside the plot frame than on the page behind it. */
  background-color: color-mix(in srgb, var(--paper) 60%, transparent);
  background-image:
    linear-gradient(var(--grid-fine) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid-fine) 1px, transparent 1px);
  background-size: 10px 10px;
  border: 1px solid var(--rule);
}

.oversold-band { fill: var(--band); }

.gridline {
  stroke: var(--grid-major);
  stroke-width: 1;
  stroke-dasharray: 2 3;
}

.threshold {
  stroke: var(--crimson);
  stroke-width: 1.2;
  stroke-dasharray: 5 3;
  opacity: .85;
}

.line {
  fill: none;
  stroke: var(--line);
  stroke-width: 1.7;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.area { stroke: none; }

.cross {
  fill: var(--paper);
  stroke: var(--crimson);
  stroke-width: 1.6;
}

.endpoint { fill: var(--line); stroke: var(--card); stroke-width: 1.5; }

.axis-label {
  fill: var(--crimson);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 9px;
}
.axis-label.faint { fill: var(--ink-3); }

.axis-date {
  fill: var(--ink-3);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 8.5px;
}
.axis-date.end { text-anchor: end; }

.nodata {
  margin: 0;
  padding: 28px 0;
  text-align: center;
  color: var(--ink-3);
  font-size: 13px;
  border: 1px dashed var(--rule);
}

/* ------------------------------------------------------- card detail */

.crosses {
  margin: 0;
  font-size: 13px;
  color: var(--ink-2);
}

.crosses strong {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 15px;
  color: var(--crimson);
}

.patterns {
  margin: 0;
  font-size: 12.5px;
  color: var(--green);
}

.valuation {
  display: flex;
  gap: 0;
  margin: 0;
  border: 1px solid var(--rule);
}

.valuation > div {
  flex: 1;
  padding: 7px 10px;
  border-right: 1px solid var(--rule);
}
.valuation > div:last-child { border-right: 0; }

.valuation dt {
  font-size: 9.5px;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--ink-3);
}

.valuation dd {
  margin: 1px 0 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.valuation.pass { border-left: 3px solid var(--green); }
.valuation.fail { border-left: 3px solid var(--ink-3); }

.valuation.none {
  padding: 7px 10px;
  font-size: 12.5px;
  font-style: italic;
  color: var(--ink-3);
  border-left: 3px solid var(--rule);
}

.valuation.pending {
  padding: 7px 10px;
  font-size: 12.5px;
  color: var(--accent);
  border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}

.provenance { margin: -4px 0 0; font-size: 11px; color: var(--ink-3); }
/* Past a quarter the estimate may well have been revised since. Flagged
   rather than hidden -- an old number still beats no number. */
.provenance.stale { color: var(--warn); font-style: italic; }
.provenance.stale::after { content: " — worth re-checking"; }

/* ---------------------------------------------------------- actions */

.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: auto; padding-top: 4px; }

.btn {
  flex: 1 1 auto;
  padding: 8px 12px;
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--ink-2);
  font-size: 12.5px;
  font-weight: 500;
  text-align: center;
  text-decoration: none;
  transition: background-color .15s ease, color .15s ease, border-color .15s ease;
}

.btn:hover { border-color: var(--accent); color: var(--accent); }

.btn.primary {
  flex: 2 1 auto;
  background: var(--accent);
  border-color: var(--accent);
  color: var(--card);
}

.btn.primary:hover {
  background: color-mix(in srgb, var(--accent) 84%, var(--ink));
  color: var(--card);
}

.btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.record-hint { margin: 0; font-size: 11px; color: var(--ink-3); }

.record-hint code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  padding: 1px 5px;
  background: color-mix(in srgb, var(--ink) 6%, transparent);
  border: 1px solid var(--rule);
}

/* --------------------------------------------------------- colophon */

.colophon {
  margin-top: 36px;
  padding-top: 14px;
  border-top: 1px solid var(--rule);
  font-size: 11.5px;
  color: var(--ink-3);
}

.colophon p { margin: 0; }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
"""
