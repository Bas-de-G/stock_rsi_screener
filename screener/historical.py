"""The Historical Dashboard: every past recommendation, and what happened next.

An event study, in the form a trading desk would recognise. Each signal is
rebased to 100 on the day it fired and drawn forward, so a $5 stock and a $500
one share an axis and the shape of the cohort is legible at a glance. The bold
line is the cohort mean; the dashed one is what a randomly-timed entry did over
the same span, because equities drift upward and a cohort that merely rises has
shown nothing.

Cohort and timeframe are chosen with radio buttons and CSS sibling selectors.
No JavaScript anywhere -- the dashboards have never had any, CI asserts it, and
pre-rendering sixteen panels costs less than a chart library would.

The honesty problem this page has to carry
------------------------------------------
"Strong buy" over history is not a clean cohort, and the page says so where it
is read rather than in a footnote. `cli._rescore_signals` overwrites every
recorded signal's price *and* fair value with today's, so the stored flag
answers "is this stock cheap now", not "was it cheap when it fired". Half of
that is repairable: the price on the signal's own bar is still in
`rsi_history`, so the gate is recomputed against it here. The fair value cannot
be -- none was recorded before 2026-07-27 -- so the cohort still leans on an
estimate from after the fact. Analyst fair values move a few times a year, so
this is a far weaker form of hindsight than the stored flag, and it is still
hindsight.

`recommendations.csv` is the clean sample, and it starts from today.
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_HORIZON, Config
from .dashboard import _CSS
from .outcomes import baseline_outcomes, mean_path, summarise, trajectory
from .signals import BUY, SELL, is_strong, valuation_passes
from .storage import Store

# Trading days drawn after the signal. Sixty is a quarter: long enough for a
# mean-reversion entry to have resolved, short enough that the sample has not
# thinned to a handful by the right-hand edge.
CHART_DAYS = 60

# Individual paths drawn per panel, most recent first. Enough to show the
# spread and the outliers without turning the panel into a solid block.
MAX_PATHS = 24

# Of those, how many are *named*: numbered on the chart and listed in the table
# underneath with the same number. Twenty-four numbered lines would be a column
# of digits down the right-hand edge, so the rest stay as faint context and the
# recent ones -- the ones still worth acting on, and the ones you might
# remember seeing -- are the identifiable ones.
NAMED_PATHS = 12

# Rows in the per-panel table.
MAX_ROWS = 16

# The window the headline figures quote.
HEADLINE_BARS = 20

_W, _H = 760.0, 300.0
# The right pad carries the path numbers: a full-length path ends hard against
# it, and a two-digit label needs room or it clips outside the viewBox.
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 40.0, 30.0, 16.0, 26.0


@dataclass(frozen=True)
class Cohort:
    key: str
    label: str
    blurb: str
    primary: bool = False   # shown in the top selector rather than behind a click


COHORTS = (
    Cohort("strong", "Strong buy", "Pattern fired, valuation confirms, nothing vetoes.", True),
    Cohort("buy", "Buy signal", "Every fired buy pattern, valued or not."),
    Cohort("sell_strong", "Strong sell", "The mirror: overbought, and dear against fair value."),
    Cohort("sell", "Sell signal", "Every fired sell pattern."),
)


@dataclass
class Entry:
    """One recommendation, with the path the price took after it."""

    symbol: str
    up2_date: str
    path: list[float]
    ret: float | None            # signed to the call, at HEADLINE_BARS
    right: bool | None
    # The conviction score this recommendation actually went out with, read
    # from the journal. None for anything published before the score existed.
    conviction: int | None = None
    conviction_band: str = ""


@dataclass
class Panel:
    cohort: str
    horizon: str
    entries: list[Entry] = field(default_factory=list)
    mean: list[float] = field(default_factory=list)
    base: list[float] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    base_stats: dict = field(default_factory=dict)


# ------------------------------------------------------------ gathering


def _retrospective_strong(signal, price_then, fair_value, config, margin) -> bool:
    """Whether this pattern would have earned a rocket at the price it fired at.

    Recomputed rather than read off `signal.valuation_pass`, which
    `_rescore_signals` has overwritten with a comparison between today's price
    and today's fair value -- a fact about the stock now, with nothing left of
    the moment the pattern completed.
    """
    if price_then is None or fair_value is None:
        return False
    gate = valuation_passes(price_then, fair_value, config.signal, margin)
    return is_strong(
        gate,
        (bool(signal.earnings_growth_known), bool(signal.earnings_growth_pass)),
    )


def collect_panels(store: Store, config: Config) -> dict[tuple[str, str], Panel]:
    """Build every (cohort, horizon) panel in one pass over the history."""
    from .journal import published_convictions

    valuations = {v.symbol: v for v in store.latest_valuations()}
    convictions = published_convictions(config.storage.recommendations)
    outcomes = {
        (o.symbol, o.horizon, o.direction, o.up2_date): o
        for o in store.all_outcomes(bars=HEADLINE_BARS)
    }
    panels: dict[tuple[str, str], Panel] = {
        (c.key, h.key): Panel(c.key, h.key) for c in COHORTS for h in config.horizons
    }

    daily: dict[str, list[tuple[str, float]]] = {}
    base_paths: list[list[float]] = []
    base_outcomes = []

    for ticker in config.tickers:
        daily[ticker.symbol] = [
            (p.date, p.close) for p in store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
        ]
        base_outcomes.extend(
            baseline_outcomes(ticker.symbol, daily[ticker.symbol], bars=(HEADLINE_BARS,))
        )
        # A random-entry path every twenty sessions: enough for a smooth mean
        # without walking the whole series for a line nobody reads individually.
        closes = daily[ticker.symbol]
        for i in range(0, len(closes), 20):
            path = trajectory(closes[i][1], closes, closes[i][0], CHART_DAYS)
            if len(path) > 1:
                base_paths.append(path)

    base_mean = mean_path(base_paths)
    base_stats = summarise(base_outcomes)

    for horizon in config.horizons:
        for ticker in config.tickers:
            prices = {
                p.date: p.close for p in store.rsi_series(ticker.symbol, horizon.key)
            }
            valuation = valuations.get(ticker.symbol)
            for signal in store.all_signals(ticker.symbol, horizon.key):
                if not signal.fired:
                    continue
                price_then = prices.get(signal.up2_date)
                path = trajectory(
                    price_then, daily[ticker.symbol], signal.up2_date, CHART_DAYS
                )
                if len(path) < 2:
                    continue
                outcome = outcomes.get(
                    (ticker.symbol, horizon.key, signal.direction, signal.up2_date)
                )
                scored = convictions.get(
                    (ticker.symbol, horizon.key, signal.direction, signal.up2_date)
                )
                entry = Entry(
                    symbol=ticker.symbol,
                    up2_date=signal.up2_date,
                    path=path,
                    ret=outcome.return_pct if outcome else None,
                    right=(outcome.return_pct > 0) if outcome else None,
                    conviction=scored[0] if scored else None,
                    conviction_band=scored[1] if scored else "",
                )
                strong = _retrospective_strong(
                    signal, price_then,
                    valuation.fair_value if valuation else None,
                    config, horizon.margin,
                )
                if signal.direction == BUY:
                    panels[("buy", horizon.key)].entries.append(entry)
                    if strong:
                        panels[("strong", horizon.key)].entries.append(entry)
                else:
                    panels[("sell", horizon.key)].entries.append(entry)
                    if strong:
                        panels[("sell_strong", horizon.key)].entries.append(entry)

    for panel in panels.values():
        # Newest first: the recent past is the part anyone can still remember.
        panel.entries.sort(key=lambda e: e.up2_date, reverse=True)
        if len(panel.entries) >= MIN_FOR_MEAN:
            panel.mean = mean_path([e.path for e in panel.entries])
        panel.base = base_mean
        panel.base_stats = base_stats
        rets = [e.ret for e in panel.entries if e.ret is not None]
        panel.stats = _stats(rets)
    return panels


def _stats(returns) -> dict:
    if not returns:
        return {"n": 0}
    ordered = sorted(returns)
    n = len(ordered)
    return {
        "n": n,
        "hit_rate": sum(1 for r in ordered if r > 0) / n,
        "mean": sum(ordered) / n,
        "median": ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2,
        "best": ordered[-1],
        "worst": ordered[0],
    }


# ------------------------------------------------------------- drawing


def _bounds(panel: Panel) -> tuple[float, float]:
    values = [v for e in panel.entries[:MAX_PATHS] for v in e.path]
    values += panel.mean + panel.base
    if not values:
        return 90.0, 110.0
    lo, hi = min(values), max(values)
    # Always show the 100 line with room either side, and never a band so tight
    # that a half-percent wobble reads as a crash.
    lo, hi = min(lo, 97.0), max(hi, 103.0)
    pad = (hi - lo) * 0.06
    return lo - pad, hi + pad


def _named(panel: Panel) -> list[Entry]:
    """The entries that get a number on the chart and a row in the table.

    Newest first, one per symbol. The dedupe is the same finding that produced
    the alert cooldown: an intraday pattern completes on almost every run, so
    the twelve newest 1h strong buys were eight companies from a single
    afternoon, four of them listed twice. Twelve numbered lines should be
    twelve different companies -- repeats of one name teach nothing about the
    cohort and waste half the key.

    Only the named subset is deduplicated. The context paths and every
    statistic on the page still run over the full sample.
    """
    out, seen = [], set()
    for e in panel.entries:
        if e.symbol in seen:
            continue
        seen.add(e.symbol)
        out.append(e)
        if len(out) == NAMED_PATHS:
            break
    return out


def _label_slots(ys: list[float], gap: float, top: float, bottom: float) -> list[float]:
    """Nudge labels apart so a stack of them stays readable.

    Paths that ran the full sixty days all end at the same x, so their numbers
    would pile up in a column. One pass down, one back up: the down pass pushes
    each label below the one before it, the up pass pulls the overflow back off
    the bottom edge. Order is preserved, so a number never crosses its
    neighbour and the key still reads top-to-bottom.
    """
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    placed = list(ys)
    cursor = top
    for i in order:
        placed[i] = cursor = max(placed[i], cursor)
        cursor += gap
    cursor = bottom
    for i in reversed(order):
        placed[i] = cursor = min(placed[i], cursor)
        cursor -= gap
    return placed


def _end_numbers(named, x, y) -> str:
    """A number at the end of each named path, keyed to the table below.

    The lines used to carry a hover `<title>` and nothing else, which answers
    "which recommendation is this?" only for someone with a mouse. This is a
    phone-first page.
    """
    if not named:
        return ""
    ends = [(x(len(e.path) - 1), y(e.path[-1])) for e in named]
    slots = _label_slots([p[1] for p in ends], 11.0, _PAD_T + 6, _H - _PAD_B - 4)
    out = ""
    for n, (e, (ex, ey), sy) in enumerate(zip(named, ends, slots), start=1):
        # A leader only where the label had to move far enough to look detached.
        if abs(sy - ey) > 4:
            out += (f'<line class="leader" x1="{ex:.1f}" y1="{ey:.1f}" '
                    f'x2="{ex + 5:.1f}" y2="{sy - 3:.1f}"/>')
        out += (f'<text class="pathnum" x="{ex + 7:.1f}" y="{sy:.1f}">{n}'
                f'<title>{html.escape(e.symbol)} · {html.escape(e.up2_date[:10])}'
                f'</title></text>')
    return out


def _plot(panel: Panel) -> str:
    if not panel.entries:
        return ('<p class="nodata">No signals in this cohort yet — '
                'nothing to measure.</p>')

    lo, hi = _bounds(panel)
    span = hi - lo or 1.0

    def x(i: int) -> float:
        return _PAD_L + (_W - _PAD_L - _PAD_R) * (i / CHART_DAYS)

    def y(v: float) -> float:
        return _PAD_T + (_H - _PAD_T - _PAD_B) * (1 - (v - lo) / span)

    def path_points(path) -> str:
        return " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(path))

    named = _named(panel)
    seen = {id(e) for e in named}
    context = [e for e in panel.entries[:MAX_PATHS] if id(e) not in seen]

    def trace(e, klass: str) -> str:
        return (
            f'<polyline class="{klass} '
            f'{"win" if e.right else "loss" if e.right is False else "open"}" '
            f'points="{path_points(e.path)}"><title>{html.escape(e.symbol)} · '
            f'{html.escape(e.up2_date[:10])}'
            + (f' · {e.ret * 100:+.1f}% at {HEADLINE_BARS}d' if e.ret is not None
               else f' · still running, day {len(e.path) - 1} of {HEADLINE_BARS}')
            + "</title></polyline>"
        )

    lines = "".join(trace(e, "trace ctx") for e in context)
    lines += "".join(trace(e, "trace") for e in named)
    lines += _end_numbers(named, x, y)

    # Horizontal rules every 5 index points, labelled — the graph-paper squares
    # behind them carry the fine grid, so these only mark the readable steps.
    rules = ""
    step = 5 if span <= 40 else 10 if span <= 90 else 25
    level = int(lo / step) * step
    while level <= hi:
        if lo < level < hi:
            klass = "base" if abs(level - 100) < 1e-9 else "rule"
            rules += (
                f'<line class="{klass}" x1="{_PAD_L:.1f}" y1="{y(level):.1f}" '
                f'x2="{_W - _PAD_R:.1f}" y2="{y(level):.1f}"/>'
                f'<text class="ytick" x="{_PAD_L - 6:.1f}" y="{y(level) + 3.5:.1f}">{level:g}</text>'
            )
        level += step

    marks = "".join(
        f'<line class="daymark" x1="{x(d):.1f}" y1="{_PAD_T:.1f}" '
        f'x2="{x(d):.1f}" y2="{_H - _PAD_B:.1f}"/>'
        f'<text class="xtick" x="{x(d):.1f}" y="{_H - 9:.1f}">+{d}d</text>'
        for d in (5, 20, 60) if d <= CHART_DAYS
    )

    mean_line = (
        f'<polyline class="mean" points="{path_points(panel.mean)}"/>'
        if len(panel.mean) > 1 else ""
    )
    base_line = (
        f'<polyline class="base-mean" points="{path_points(panel.base)}"/>'
        if len(panel.base) > 1 else ""
    )
    end_label = ""
    if len(panel.mean) > 1:
        final = panel.mean[-1]
        end_label = (
            f'<text class="endval" x="{x(len(panel.mean) - 1) - 4:.1f}" '
            f'y="{y(final) - 7:.1f}">{final - 100:+.1f}%</text>'
        )

    shown = min(len(panel.entries), MAX_PATHS)
    numbered = len(named)
    mean_key = (
        '<span class="k mean-k">Cohort mean</span>' if panel.mean
        else f'<span class="k count">too few signals to average '
             f'(under {MIN_FOR_MEAN})</span>'
    )
    return f"""<svg class="study" viewBox="0 0 {_W:.0f} {_H:.0f}" role="img"
     aria-label="Price paths after {panel.stats.get('n', 0)} signals, rebased to 100
                 on the signal day; cohort mean ends at
                 {panel.mean[-1] - 100 if panel.mean else 0:+.1f} percent">
  <text class="xtick left" x="{_PAD_L:.1f}" y="{_H - 9:.1f}">signal</text>
  {rules}{marks}
  {lines}
  {base_line}{mean_line}{end_label}
</svg>
<p class="legend">
  {mean_key}
  <span class="k base-k">Random entry</span>
  <span class="k win-k">Individual, call was right</span>
  <span class="k loss-k">…was wrong</span>
  <span class="k open-k">…still running</span>
  <span class="k count">{shown} of {len(panel.entries):,} drawn ·
    <strong>1–{numbered}</strong> numbered, and named in the table below</span>
</p>"""


def _table(panel: Panel) -> str:
    """The key to the numbered lines above, newest first.

    This used to drop every entry without a `+20d` return, which sounds like
    tidiness and was a bug in effect: a return at twenty trading days cannot
    exist until twenty trading days have passed, so the newest row on the page
    was always a month old and the 1h panel -- where every drawn line is days
    old -- listed nothing recent at all. The page looked stale while working
    perfectly.

    So a recommendation still inside its twenty days is listed too, with the
    return it has *so far* and how far through it is. It is excluded from the
    cohort statistics, which still need the full window to compare like with
    like.
    """
    rows = _named(panel)
    if not rows:
        return ""
    body = ""
    for n, e in enumerate(rows, start=1):
        if e.ret is not None:
            outcome = (f'<td class="num {"good" if e.right else "bad"}">'
                       f'{e.ret * 100:+.1f}%</td>')
        else:
            done = len(e.path) - 1
            outcome = (f'<td class="num open" title="Measured at +{HEADLINE_BARS} '
                       f'trading days; this one is {done} in">day {done}'
                       f'<span class="of">/{HEADLINE_BARS}</span></td>')
        if e.conviction is None:
            # Published before the score existed. An em dash rather than a
            # blank, so "we did not score this" reads differently from "this
            # scored nothing".
            score = '<td class="num cv-na" title="Published before the '
            score += 'conviction score existed">—</td>'
        else:
            score = (f'<td class="num cvcell cv-{html.escape(e.conviction_band)}" '
                     f'title="The conviction this went out with, from the '
                     f'journal — not recomputed today">{e.conviction}</td>')
        body += (
            f'<tr><td class="num idx">{n}</td>'
            f'<th scope="row">{html.escape(e.symbol)}</th>'
            f'<td class="num date">{html.escape(e.up2_date[:10])}</td>'
            f'{score}'
            f'<td class="num">{e.path[-1] - 100:+.1f}%</td>{outcome}</tr>'
        )
    return f"""<table class="names">
  <caption>The {len(rows)} numbered lines above, newest first</caption>
  <thead><tr><th scope="col"><span class="vh">Line</span>#</th>
    <th scope="col">Symbol</th><th scope="col">Signal</th>
    <th scope="col" title="The weighted conviction score this went out with">Conv.</th>
    <th scope="col">So far</th><th scope="col">At +{HEADLINE_BARS}d</th></tr></thead>
  <tbody>{body}</tbody>
</table>"""


def _readout(panel: Panel) -> str:
    s, b = panel.stats, panel.base_stats
    if not s.get("n"):
        return '<div class="readout-strip"><div class="ro"><dt>Signals</dt><dd>0</dd></div></div>'
    edge = s["mean"] - b.get("mean", 0.0)
    edge_class = "good" if edge > 0.005 else "bad" if edge < -0.005 else ""
    return f"""<div class="readout-strip">
  <div class="ro"><dt>Signals</dt><dd>{s['n']:,}</dd></div>
  <div class="ro"><dt>Hit rate</dt><dd>{s['hit_rate'] * 100:.1f}%</dd></div>
  <div class="ro"><dt>Mean +{HEADLINE_BARS}d</dt><dd>{s['mean'] * 100:+.1f}%</dd></div>
  <div class="ro"><dt>Median</dt><dd>{s['median'] * 100:+.1f}%</dd></div>
  <div class="ro"><dt>Edge vs random</dt><dd class="{edge_class}">{edge * 100:+.1f}%</dd></div>
  <div class="ro"><dt>Best / worst</dt><dd class="spread">{s['best'] * 100:+.0f}% / {s['worst'] * 100:+.0f}%</dd></div>
</div>"""


_STRONG_CAVEAT = """<p class="inline-caveat">
  <strong>This cohort is selected with hindsight.</strong> The gate is
  recomputed at the price each pattern actually fired at, but against a fair
  value first recorded in July&nbsp;2026 — none existed before. So membership
  partly means <em>“cheap today”</em>, which is a fact about the present being
  used to pick the past.
  <br>
  The <strong>strong sell</strong> panel is what that looks like when it goes
  wrong: “dear against fair value now” selects stocks that already rose, so
  their paths rise, and the cohort scores catastrophically against a call it
  was never really making. Treat the strong-buy edge as a hypothesis worth
  testing, not a result. <code>recommendations.csv</code> records the gate as it
  stood at the time, and starts from today.
</p>"""

# Below this, a "cohort average" is a handful of tickers wearing the authority
# of one. The sell-strong weekly cohort had three members and a mean path
# ending at +192%, which is not an average of anything.
MIN_FOR_MEAN = 8


def _panel_html(panel: Panel, cohort: Cohort, horizon) -> str:
    caveat = _STRONG_CAVEAT if cohort.key in ("strong", "sell_strong") else ""
    direction_note = (
        "" if cohort.key.startswith("sell") is False else
        '<p class="dir-note">These are <em>sell</em> calls, so a falling line is '
        'the winning one. The figures below are signed to the call either way.</p>'
    )
    return f"""<section class="panel p-{panel.cohort}-{panel.horizon}">
  <header class="panel-head">
    <h2>{html.escape(cohort.label)} · {html.escape(horizon.label)} chart</h2>
    <p class="panel-blurb">{html.escape(cohort.blurb)}
       Two crosses of the threshold within {horizon.window_days} days;
       fair value {horizon.margin_pct} clear.</p>
  </header>
  {direction_note}
  {_readout(panel)}
  <div class="plot-frame">{_plot(panel)}</div>
  {caveat}
  {_table(panel)}
</section>"""


# -------------------------------------------------------------- page


def _strategies_section(store: Store, config: Config) -> str:
    """The leaderboard: every strategy ranked, with a legend explaining each.

    Ranked by mean return per trade rather than by hit rate, because the two
    disagree and the disagreement is the interesting part -- a rule taking 5%
    profits against 15% stops can be right most of the time and still lose
    money. Total return is shown beside it so a rule that wins rarely and
    hugely is not hidden behind a rule that wins often and barely.

    Buys only. Sells are recorded and charted above, but "which strategy would
    I run" is a question about entries, and mixing a cohort that scores
    negatively on this sample into every row would move the ranking with the
    buy/sell mix rather than with the rule.
    """
    from .strategies import ALL_BUYS, leaderboard

    variants = list(config.strategies.variants)
    if not variants:
        return ""
    trades = store.all_trades(direction=BUY)
    if not trades:
        return ""

    rows = leaderboard(trades, variants)
    best = rows[0][1] if rows and rows[0][1].get("n") else None
    baselines = _baselines(store, config, sorted({r.max_bars for r in config.strategies.exits}))

    body = []
    for rank, (strategy, st) in enumerate(rows, start=1):
        if not st["n"]:
            body.append(
                f'<tr class="empty"><td class="rank">{rank}</td>'
                f'<th>{html.escape(strategy.name)}</th>'
                f'<td colspan="7" class="none">never triggered on this sample</td></tr>'
            )
            continue
        top = " top" if best is not None and st is best else ""
        pos = " up" if st["mean"] > 0 else " down"
        body.append(
            f'<tr class="{top.strip()}">'
            f'<td class="rank">{rank}</td>'
            f'<th>{html.escape(strategy.name)}</th>'
            f'<td>{st["n"]:,}</td>'
            f'<td>{st["hit_rate"] * 100:.0f}%</td>'
            f'<td class="num{pos}">{st["mean"] * 100:+.2f}%</td>'
            f'<td class="num">{st["median"] * 100:+.2f}%</td>'
            f'<td class="num">{st["total"] * 100:+,.0f}%</td>'
            f'<td class="muted">{st["target"]}/{st["stopped"]}/{st["timeout"]}</td>'
            f'<td class="muted">{st["mean_bars"]:.0f}</td>'
            f'</tr>'
        )

    base_rows = "".join(
        f'<tr class="baseline"><td class="rank">·</td>'
        f'<th>Random entry · held {bars} days</th>'
        f'<td>{st["n"]:,}</td><td>{st["hit_rate"] * 100:.0f}%</td>'
        f'<td class="num">{st["mean"] * 100:+.2f}%</td>'
        f'<td class="num">{st["median"] * 100:+.2f}%</td>'
        f'<td colspan="3" class="none">no rules at all — the number to beat</td></tr>'
        for bars, st in sorted(baselines.items()) if st.get("n")
    )

    exits = "".join(
        f'<div class="def"><dt>{html.escape(r.label)}</dt>'
        f'<dd>{html.escape(_exit_sentence(r))}</dd></div>'
        for r in config.strategies.exits
    )
    picks = "".join(
        f'<div class="def"><dt>{html.escape(sel.label)}</dt>'
        f'<dd>{html.escape(_selection_sentence(sel))}</dd></div>'
        for sel in config.strategies.selections
    )

    return f"""
<section class="strategies">
  <div class="divider"><span>Strategies</span></div>

  <h2>Which set of rules would actually have worked?</h2>
  <p class="lede">Every strategy below is the same three choices in a
     different combination: <strong>which signals to take</strong>,
     <strong>which charts to watch</strong>, and <strong>when to get
     out</strong>. All of them are run over the same recorded patterns, walked
     bar by bar, so the only thing separating two rows is the rule itself.</p>

  <h3>The exit rules</h3>
  <dl class="legend">{exits}</dl>

  <h3>Which signals each one takes</h3>
  <dl class="legend">{picks}</dl>

  <h3>Leaderboard</h3>
  <p class="lede">{len(variants)} permutations, buys only, ranked by mean
     return per trade.</p>
  <div class="scroll">
  <table class="board">
    <thead><tr>
      <th>#</th><th>Strategy</th><th>Trades</th><th>Hit</th><th>Mean</th>
      <th>Median</th><th>Total</th><th title="target / stopped / timeout">T/S/O</th>
      <th>Days</th>
    </tr></thead>
    <tbody>{base_rows}{"".join(body)}</tbody>
  </table>
  </div>

  <ul class="fine">
    <li><strong>The dotted rows are the number to beat.</strong> Equities drift
        upward, so <em>any</em> long strategy scores above half and returns
        positively over a rising sample — including buying on days picked by a
        coin. A strategy is only interesting where it parts company with the
        random entry held for the same length of time, and several of these do
        not.</li>
    <li><strong>Ranked by mean, not hit rate.</strong> They disagree, and the
        disagreement is the point: a rule taking small profits against large
        stops can be right most of the time and still lose money. <em>Total</em>
        is the sum of every trade's return — not compounded, because these
        trades overlap in time and compounding would imply a position size and
        a capital limit that nothing here models.</li>
    <li><strong>T/S/O</strong> is how each strategy ended: hit its target,
        stopped out, or timed out at the end of its window. A rule earning its
        return from timeouts rather than targets is not doing what its name
        says.</li>
    <li><strong>{len(variants)} permutations against one sample is
        {len(variants)} chances for the winner to have won by luck.</strong>
        Nothing here corrects for that. Read the leaderboard as a shortlist to
        test forward, not as a result — and prefer a rule that is good across
        several neighbouring settings over one that spikes alone.</li>
    <li><strong>"Strong only" is partly hindsight.</strong> Fair values only
        exist from 2026-07-27, so a signal's strength is recomputed here
        against the price on its own bar but today's fair value. Analyst fair
        values move a few times a year, so this is far weaker hindsight than
        the stored flag — and it is still hindsight. The clean comparison
        starts from <code>recommendations.csv</code>, going forward.</li>
    <li><strong>Exits are the close that breached the barrier</strong>, not the
        barrier. With daily closes a stop touched at 11am and recovered by the
        bell is invisible, and a gap opens straight through it. Tighter rules
        lose more to this than wide ones.</li>
  </ul>
</section>"""


def _baselines(store: Store, config: Config, windows) -> dict:
    """Random-entry returns over each holding period the strategies use.

    Without these the leaderboard is unreadable: this sample covers a period
    equities spent mostly rising, so a 60-day hold posts a good number for
    reasons that have nothing to do with the screener. Comparing a 60-day
    strategy against a 20-day baseline would flatter it twice over, which is
    why there is one baseline per window rather than one for the page.
    """
    from .outcomes import baseline_outcomes
    from .outcomes import summarise as summarise_outcomes

    windows = tuple(windows)
    if not windows:
        return {}
    pooled = {bars: [] for bars in windows}
    for ticker in config.tickers:
        closes = [
            (p.date, p.close) for p in store.rsi_series(ticker.symbol, DEFAULT_HORIZON)
        ]
        for outcome in baseline_outcomes(ticker.symbol, closes, bars=windows):
            pooled[outcome.bars].append(outcome)
    return {bars: summarise_outcomes(rows) for bars, rows in pooled.items()}


def _exit_sentence(rule) -> str:
    """The legend line for one exit rule, in plain words."""
    if rule.take_profit is None and rule.stop_loss is None:
        return (f"No target and no stop. Hold for {rule.max_bars} trading days "
                f"and take whatever the price is then.")
    parts = []
    if rule.take_profit is not None:
        parts.append(f"sell at +{rule.take_profit:g}%")
    if rule.stop_loss is not None:
        parts.append(f"cut at -{rule.stop_loss:g}%")
    sentence = ", ".join(parts).capitalize()
    breakeven = rule.breakeven_hit_rate
    tail = (f" Needs to be right {breakeven:.0%} of the time to break even."
            if breakeven is not None else "")
    return (f"{sentence}, whichever comes first, or close after "
            f"{rule.max_bars} trading days.{tail}")


def _selection_sentence(selection) -> str:
    """The legend line for one selection."""
    from .strategies import STRONG_ONLY

    many = len(selection.horizons) > 1
    charts = (
        ", ".join(selection.horizons[:-1]) + " and " + selection.horizons[-1]
        if many else selection.horizons[0]
    )
    bar = (
        "only signals that also cleared the valuation gate with nothing "
        "vetoing them — the rocket on the cards above"
        if selection.entry == STRONG_ONLY
        else "every fired buy pattern, whether or not a fair value confirmed it"
    )
    return f"Takes {bar}, on the {charts} chart" + ("s." if many else ".")


def build_historical(store: Store, config: Config, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_historical(store, config), encoding="utf-8")
    return output


def render_historical(store: Store, config: Config) -> str:
    panels = collect_panels(store, config)
    generated = dt.datetime.now().strftime("%d %B %Y, %H:%M")
    horizons = list(config.horizons)
    default_h = next((h for h in horizons if h.key == "1d"), horizons[0])

    # Radios first, panels after: both `:checked ~` chains have to reach the
    # panel container, which is what lets one CSS rule express "this cohort AND
    # this timeframe".
    radios = "".join(
        f'<input class="sel" type="radio" name="co" id="co-{c.key}"'
        f'{" checked" if c.key == "strong" else ""}>' for c in COHORTS
    ) + "".join(
        f'<input class="sel" type="radio" name="tf" id="tf-{h.key}"'
        f'{" checked" if h.key == default_h.key else ""}>' for h in horizons
    )

    primary_tabs = "".join(
        f'<label class="tab" for="co-{c.key}">{html.escape(c.label)}</label>'
        for c in COHORTS if c.primary
    )
    secondary_tabs = "".join(
        f'<label class="tab" for="co-{c.key}">{html.escape(c.label)}</label>'
        for c in COHORTS if not c.primary
    )
    tf_tabs = "".join(
        f'<label class="tab tf-tab" for="tf-{h.key}">{html.escape(h.label)}</label>'
        for h in horizons
    )

    body_panels = "\n".join(
        _panel_html(panels[(c.key, h.key)], c, h)
        for c in COHORTS for h in horizons
    )
    exits = _strategies_section(store, config)

    show_rules = "\n".join(
        f'#co-{c.key}:checked ~ #tf-{h.key}:checked ~ .panels .p-{c.key}-{h.key} '
        f'{{ display: block; }}'
        for c in COHORTS for h in horizons
    )
    tab_rules = "\n".join(
        f'#co-{c.key}:checked ~ .controls label[for="co-{c.key}"],\n'
        f'#tf-{h.key}:checked ~ .controls label[for="tf-{h.key}"] '
        f'{{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}'
        for c in COHORTS for h in horizons
    )

    def page_for(h) -> str:
        return "index.html" if h.key == DEFAULT_HORIZON else f"{h.key}.html"

    links = "".join(
        f'<a class="tf" href="{page_for(h)}">{html.escape(h.label)}</a>'
        for h in horizons
    ) + '<a class="tf on" href="history.html">Historical</a>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSI Screener · Historical Dashboard</title>
<style>{_CSS}{_EXTRA_CSS}
{show_rules}
{tab_rules}
</style>
</head>
<body>
<div class="sheet hist">
<header class="masthead">
  <div class="title-block">
    <p class="eyebrow">Relative Strength Screener · Post-signal performance</p>
    <h1>Historical Dashboard</h1>
    <p class="standfirst">
      Every pattern on record, rebased to <strong>100</strong> on the day it
      fired and followed for {CHART_DAYS} trading days. The heavy line is the
      cohort average; the dashed one is a randomly-timed entry over the same
      span — cohorts are only interesting where they part company with it.
    </p>
    <nav class="timeframes" aria-label="Page">{links}</nav>
  </div>
</header>

{radios}

<div class="controls">
  <div class="ctl">
    <span class="ctl-label">Cohort</span>
    <div class="tabs">{primary_tabs}</div>
    <details class="more">
      <summary>Other signals</summary>
      <div class="tabs">{secondary_tabs}</div>
    </details>
  </div>
  <div class="ctl">
    <span class="ctl-label">Timeframe</span>
    <div class="tabs">{tf_tabs}</div>
  </div>
</div>

<div class="panels">
{body_panels}
</div>

{exits}

<div class="caveats">
  <h2>What these numbers are not</h2>
  <ul>
    <li><strong>Survivorship.</strong> The watchlist is today's companies — all
        still listed, all still large. A sample that quietly excludes
        everything that failed reads optimistically.</li>
    <li><strong>No costs.</strong> No spread, no commission, no slippage, and
        an entry at the close of the bar the pattern completed on.</li>
    <li><strong>One market regime.</strong> Measured over a period equities
        spent mostly rising. A short cohort losing money in a bull market is
        what you would expect, not proof it is wrong forever.</li>
    <li><strong>Valuation is applied after the fact.</strong> See the note in
        the strong-buy and strong-sell panels.</li>
  </ul>
</div>

<footer class="colophon">
  <p>Generated {generated} · Paths from Yahoo Finance daily closes ·
     Recomputed on every run, never captured</p>
  <p class="disclaimer">A back-tested edge is not a forecast. Nothing here is
     financial advice.</p>
</footer>
</div>
</body>
</html>"""


_EXTRA_CSS = """
/* ---- Strategies + leaderboard ---------------------------------------- */
.strategies { margin: 40px 0 8px; }
.divider { display: flex; align-items: center; gap: 16px; margin: 0 0 26px; }
.divider::before, .divider::after {
  content: ""; flex: 1; height: 2px; background: var(--ink); opacity: .85;
}
.divider span {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .18em;
  font-weight: 700; color: var(--ink);
}
.strategies h2 { font-size: 1.15rem; margin: 0 0 6px; letter-spacing: -0.01em; }
.strategies h3 {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--ink-3); margin: 26px 0 10px;
}
.strategies .lede { margin: 0 0 4px; color: var(--ink-2); max-width: 68ch; }

dl.legend {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 2px 22px; margin: 0;
}
dl.legend .def {
  display: grid; grid-template-columns: 8.5rem 1fr; gap: 10px;
  padding: 6px 0; border-bottom: 1px solid var(--rule);
}
dl.legend dt { font-weight: 600; font-size: .84rem; }
dl.legend dd { margin: 0; color: var(--ink-2); font-size: .82rem; line-height: 1.45; }

/* Wide table, narrow phone: scrolls inside its own box so the page never does */
.strategies .scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table.board {
  border-collapse: collapse; width: 100%; min-width: 680px;
  font-variant-numeric: tabular-nums; font-size: .84rem;
}
table.board th, table.board td {
  padding: 7px 10px; text-align: right; border-bottom: 1px solid var(--rule);
}
table.board thead th {
  font-size: .68rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--ink-3); border-bottom: 1px solid var(--ink); white-space: nowrap;
}
table.board th:nth-child(2), table.board thead th:nth-child(2) { text-align: left; }
table.board td.rank { color: var(--ink-3); width: 2.2rem; }
table.board tbody th { font-weight: 500; text-align: left; white-space: nowrap; }
table.board tr.top { background: color-mix(in srgb, var(--green) 9%, transparent); }
table.board tr.top th, table.board tr.top td { font-weight: 700; }
table.board tr.empty th, table.board tr.empty td { color: var(--ink-3); }
table.board tr.baseline { border-bottom: 2px solid var(--ink); }
table.board tr.baseline th, table.board tr.baseline td {
  color: var(--ink-2); font-style: italic;
}
table.board .muted { color: var(--ink-3); }
table.board .none { text-align: left; font-style: italic; }
table.board .up { color: var(--green); }
table.board .down { color: var(--crimson); }
.strategies ul.fine {
  margin: 16px 0 0; padding-left: 18px; color: var(--ink-2);
  font-size: .82rem; line-height: 1.55; max-width: 82ch;
}
.strategies ul.fine li { margin-bottom: 7px; }
.strategies ul.fine code {
  font-size: .95em; background: color-mix(in srgb, var(--ink) 6%, transparent);
  padding: 1px 4px; border-radius: 3px;
}

/* ---- Historical Dashboard -------------------------------------------- */
.sel { position: absolute; opacity: 0; pointer-events: none; }

.controls {
  display: flex; flex-wrap: wrap; gap: 22px 34px;
  padding: 14px 0 16px; margin-bottom: 20px;
  border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule);
}
.ctl-label {
  display: block; font-size: 10px; letter-spacing: .18em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 700; margin-bottom: 7px;
}
.tabs { display: flex; flex-wrap: wrap; gap: 6px; }
.tab {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase;
  padding: 6px 11px; cursor: pointer; user-select: none;
  color: var(--ink-2); background: color-mix(in srgb, var(--ink) 4%, transparent);
  border: 1px solid var(--rule); border-radius: 2px;
  transition: background .12s ease, color .12s ease, border-color .12s ease;
}
.tab:hover { border-color: var(--ink-2); color: var(--ink); }
.more { margin-top: 9px; }
.more summary {
  font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); cursor: pointer; padding: 3px 0;
}
.more summary:hover { color: var(--ink); }
.more[open] summary { margin-bottom: 7px; }

.panel { display: none; }
.panel-head { margin-bottom: 10px; }
.panel-head h2 {
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-size: 23px; margin: 0 0 4px; letter-spacing: -.01em;
}
.panel-blurb { margin: 0; font-size: 13px; color: var(--ink-2); line-height: 1.45; }
.dir-note {
  margin: 10px 0 0; font-size: 12.5px; color: var(--warn);
  border-left: 2px solid var(--warn); padding-left: 9px;
}

.readout-strip {
  display: flex; flex-wrap: wrap; gap: 0;
  margin: 14px 0 12px; border: 1px solid var(--rule);
  background: color-mix(in srgb, var(--ink) 3%, transparent);
}
.ro {
  flex: 1 1 110px; padding: 9px 12px;
  border-right: 1px solid var(--rule);
}
.ro:last-child { border-right: 0; }
.ro dt {
  font-size: 9.5px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 700; margin-bottom: 3px;
}
.ro dd {
  margin: 0; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 19px; font-weight: 600; font-variant-numeric: tabular-nums;
  letter-spacing: -.02em;
}
.ro dd.spread { font-size: 14px; }
.ro dd.good { color: var(--green); }
.ro dd.bad { color: var(--crimson); }

/* The graph-paper frame, same idea as the RSI cards but at desk scale. */
.plot-frame { border: 1px solid var(--rule); background: var(--card); padding: 0; }
.study {
  width: 100%; height: auto; display: block;
  background-color: color-mix(in srgb, var(--paper) 60%, transparent);
  background-image:
    linear-gradient(var(--grid-fine) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid-fine) 1px, transparent 1px);
  background-size: 12px 12px;
}
.trace { fill: none; stroke-width: 1.3; stroke: var(--ink-3); opacity: .55; }
.trace.win { stroke: var(--green-soft); }
.trace.loss { stroke: var(--crimson); }
/* Still inside its twenty days, so it has no verdict yet and must not borrow
   the colour of one. */
.trace.open { stroke: var(--ink-2); stroke-dasharray: 3 3; }
/* The unnumbered remainder: there for the shape of the cohort, not to be read
   individually. */
.trace.ctx { stroke-width: 1; opacity: .2; stroke-dasharray: none; }
.pathnum {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 9px; fill: var(--ink-2); text-anchor: start; cursor: help;
}
.leader { stroke: var(--rule); stroke-width: .8; }
.mean { fill: none; stroke: var(--line); stroke-width: 2.6; stroke-linejoin: round; }
.base-mean {
  fill: none; stroke: var(--ink-2); stroke-width: 1.6;
  stroke-dasharray: 5 4; opacity: .85;
}
.study .rule { stroke: var(--grid-major); stroke-width: 1; }
.study .base { stroke: var(--ink-2); stroke-width: 1.2; }
.study .daymark { stroke: var(--grid-major); stroke-width: 1; stroke-dasharray: 2 4; }
.ytick, .xtick {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 9.5px; fill: var(--ink-3);
}
.ytick { text-anchor: end; }
.xtick { text-anchor: middle; }
.xtick.left { text-anchor: start; }
.endval {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px; font-weight: 700; fill: var(--line); text-anchor: end;
}

.legend {
  display: flex; flex-wrap: wrap; gap: 4px 16px;
  margin: 0; padding: 8px 12px; border-top: 1px solid var(--rule);
  font-size: 10.5px; letter-spacing: .04em; color: var(--ink-3);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
.k::before {
  content: ""; display: inline-block; width: 15px; height: 0;
  border-top-width: 2px; border-top-style: solid; vertical-align: middle;
  margin-right: 6px; border-color: currentColor;
}
.mean-k { color: var(--line); }
.base-k::before { border-top-style: dashed; }
.win-k { color: var(--green-soft); }
.loss-k { color: var(--crimson); }
.open-k { color: var(--ink-2); }
.open-k::before { border-top-style: dotted; }
.count::before { content: none; margin: 0; }
.count { margin-left: auto; }

.inline-caveat {
  margin: 12px 0 0; padding: 10px 12px; font-size: 12.5px; line-height: 1.5;
  color: var(--ink-2); border-left: 2px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 6%, transparent);
}
.inline-caveat code { font-size: 11.5px; }

.names { width: 100%; border-collapse: collapse; margin: 18px 0 6px; font-size: 13px; }
.names caption {
  text-align: left; font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 700; padding-bottom: 6px;
}
.names th, .names td { padding: 6px 9px; border-bottom: 1px solid var(--rule); }
.names thead th {
  font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); text-align: right; font-weight: 600;
}
.names thead th:first-child { text-align: left; }
.names tbody th { text-align: left; font-weight: 700; }
.names .num {
  text-align: right; font-variant-numeric: tabular-nums;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
.names .date { color: var(--ink-3); }
.names .good { color: var(--green); font-weight: 600; }
.names .bad { color: var(--crimson); font-weight: 600; }
/* The line number, and the only column that is a cross-reference rather than
   a measurement — so it reads as a label, not a figure. */
.names .idx {
  color: var(--ink-3); text-align: left; width: 1%; padding-right: 2px;
  font-size: 11px;
}
.names .open { color: var(--ink-2); cursor: help; font-weight: 500; }
/* The conviction each row went out with. Muted next to the returns — this is
   the input being tested, not the result. */
.names .cvcell { font-weight: 700; cursor: help; }
.names .cv-green { color: var(--green); }
.names .cv-amber { color: var(--warn); }
.names .cv-red   { color: var(--crimson); }
.names .cv-na    { color: var(--ink-3); cursor: help; }
.names .open .of { opacity: .6; font-size: 10px; }
/* Visually hidden: "Line #" for a screen reader, "#" on the page. */
.vh {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
.nodata { padding: 40px 14px; text-align: center; color: var(--ink-3); font-size: 13px; }

.caveats { margin: 26px 0 22px; border-top: 1px solid var(--rule); padding-top: 16px; }
.caveats h2 {
  font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-3); margin: 0 0 10px;
}
.caveats ul { margin: 0; padding-left: 18px; }
.caveats li { margin-bottom: 7px; font-size: 13.5px; line-height: 1.5; color: var(--ink-2); }

@media (max-width: 620px) {
  .ro { flex-basis: 50%; border-bottom: 1px solid var(--rule); }
  .ro dd { font-size: 16px; }
  .legend .count { margin-left: 0; }
  .names { font-size: 12px; }
}
"""
