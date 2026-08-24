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
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DEFAULT_HORIZON, MARKET_LABELS, MARKETS, Config, Horizon
from .earnings import BEFORE, CLEAR_WINDOW, EarningsWindow, earnings_window
from .earnings import to_date as to_release_date
from .scoring import GREEN_AT, MAX_SCORE as SCORE_MAX, MIN_SCORE as SCORE_MIN
from .signals import (
    BUY,
    SELL,
    earnings_growth_passes,
    find_upward_crosses,
    is_strong,
    signal_is_fresh,
    signal_is_live,
    valuation_passes,
)
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
    markets: tuple[str, ...] = ()
    # Needed to judge freshness, which is measured in bars of this timeframe.
    horizon: Horizon | None = None
    # Where this company sits relative to its next results. A signal inside
    # that window is shown but not acted on -- see `screener.earnings`.
    earnings: EarningsWindow = CLEAR_WINDOW
    # Phil Town's Rule #1 reading, or None where it has never been computed.
    rule_one: object | None = None
    # The weighted conviction score, or None on a page built before it existed.
    # Shadow only: it ranks and explains, and decides nothing -- see
    # `screener.scoring.SHADOW`.
    conviction: object | None = None

    @property
    def suspended(self) -> bool:
        """Results are close enough that the technical signal isn't actionable."""
        return self.earnings.suspended and (self.fired or self.sell_fired)

    @property
    def earnings_note(self) -> str:
        """The warning as it reads on the card."""
        if not self.suspended:
            return ""
        side = "buy" if self.fired else "sell"
        if self.earnings.state == BEFORE:
            sessions = self.earnings.sessions
            when = "today" if sessions == 0 else (
                "tomorrow" if sessions == 1 else f"in {sessions} trading days"
            )
            return f"Earnings {when} — {side} signal suspended"
        return f"Earnings just reported — {side} signal suspended until the next session closes"

    @property
    def both_valuations_agree(self) -> bool:
        """Morningstar and Rule #1 independently like the same stock.

        Two methods with almost nothing in common: one is an analyst's
        discounted cash flow, the other a mechanical projection of earnings.
        Agreement is not proof — neither has been measured yet — but it is the
        rarest thing on the page, and worth a mark. Three of fifteen live
        strong buys have it.
        """
        reading = self.rule_one
        return bool(
            self.strong and reading is not None
            and reading.applicable and reading.band == "green"
        )

    @property
    def fresh(self) -> bool:
        """A live signal that completed within the last couple of bars."""
        if self.horizon is None:
            return False
        return any(signal_is_fresh(s, self.series, self.horizon) for s in self.signals)

    @property
    def deal_discount(self) -> float | None:
        """Discount to fair value, if this row is a candidate deal of the day.

        Two conditions and no more: a *buy* pattern that just fired on the RSI,
        and a price below its fair value. The size of that gap is what ranks
        the candidates against each other, so this returns it.

        Deliberately **not** gated on the horizon's margin or on earnings
        growth. Those decide the rocket, and the rocket is a separate
        judgement that this must not disturb: a signal is strong when the
        valuation clears the bar by a wide margin, whereas the deal of the day
        is simply the pick of whatever fired today. Requiring both made the
        deal a strict subset of the rockets and left it empty for weeks.

        Sells are excluded by construction — exiting a position is not a
        bargain — and so is a gap that runs the wrong way: a fresh buy trading
        *above* fair value is timely, but it is not a deal.

        A suspended signal is excluded too. Leading the page with a pick the
        page itself calls un-actionable would be the one place the warning
        could not be read as a warning.
        """
        if self.horizon is None or self.suspended:
            return None
        best: float | None = None
        for s in self.buys:
            if not (s.fired and signal_is_fresh(s, self.series, self.horizon)):
                continue
            if not s.price or not s.fair_value:
                continue
            discount = (s.fair_value - s.price) / s.price
            if discount <= 0:
                continue
            if best is None or discount > best:
                best = discount
        return best

    @property
    def latest(self) -> RsiPoint | None:
        return self.series[-1] if self.series else None

    @property
    def rsi(self) -> float | None:
        return self.latest.rsi if self.latest else None

    @property
    def buys(self) -> list[Signal]:
        return [s for s in self.signals if s.direction == BUY]

    @property
    def sells(self) -> list[Signal]:
        return [s for s in self.signals if s.direction == SELL]

    @property
    def fired(self) -> bool:
        return any(s.fired for s in self.buys)

    @property
    def sell_fired(self) -> bool:
        return any(s.fired for s in self.sells)

    @property
    def sell_strong(self) -> bool:
        return any(
            s.fired and is_strong(
                (s.valuation_known, s.valuation_pass),
                (s.earnings_growth_known, s.earnings_growth_pass),
            )
            for s in self.sells
        )

    @property
    def strong(self) -> bool:
        """Pattern fired and every known grading factor backs it up.

        Fair value and earnings growth grade independently: whichever ones
        have actually been checked must all agree. A pattern with only one
        of the two known can still earn the rocket on that one alone — same
        lenient rule the dashboard already used for fair value by itself,
        just extended to a second factor.
        """
        return any(
            s.fired and is_strong(
                (s.valuation_known, s.valuation_pass),
                (s.earnings_growth_known, s.earnings_growth_pass),
            )
            for s in self.buys
        )

    @property
    def latest_signal(self) -> Signal | None:
        return self.signals[-1] if self.signals else None

    @property
    def state(self) -> str:
        """Bucket used for the status pill, the card's accent, and sort order."""
        # Checked before every verdict, because that is what "suspended" means:
        # the pattern is real and still on the page, but results are close
        # enough that acting on it is a coin flip rather than the model's edge.
        if self.suspended:
            return "suspended"
        if self.strong:
            return "strong"
        if self.fired:
            # A fired signal whose valuation was checked and disagreed is
            # still a signal — the fair value only grades it.
            checked = any(s.fired and s.valuation_known for s in self.buys)
            return "signal_checked" if checked else "signal"
        # Sells rank below buys: this is a screener for entries, and an exit
        # only matters for something already held.
        if self.sell_strong:
            return "sell_strong"
        if self.sell_fired:
            return "sell"
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
    store: Store,
    config: Config,
    output: Path,
    standalone: bool = True,
    horizon: str = DEFAULT_HORIZON,
) -> Path:
    h = config.horizon(horizon)
    rows = _collect(store, config, h)
    html_text = render(rows, config, h, standalone=standalone)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    return output


def build_all_dashboards(store: Store, config: Config, output: Path) -> list[Path]:
    """One page per horizon, all in the same directory.

    Separate pages rather than one page carrying every horizon's chart data:
    four timeframes across 65 tickers inline would be roughly a megabyte, and
    the person this is built for reads it on a phone. The horizon selector is
    plain links between the files, so switching costs a page load but the
    market filter below it stays instant and needs no JavaScript.

    The default horizon becomes index.html so the published URL keeps working.
    """
    from .historical import build_historical

    output.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for h in config.horizons:
        name = output.name if h.key == DEFAULT_HORIZON else f"{h.key}{output.suffix}"
        written.append(build_dashboard(store, config, output.parent / name, horizon=h.key))
    # The track record sits alongside rather than inside: those pages answer
    # "what should I look at today", this one answers "should I believe them".
    written.append(build_historical(store, config, output.parent / "history.html"))
    return written


def _visible_crosses(full: list[RsiPoint], window: int, threshold: float) -> list[int]:
    """Upward crosses inside the chart window, as indices into the visible slice.

    Detected over one *extra* leading bar rather than the visible slice alone.
    A cross is defined by comparing a bar against its predecessor, and for the
    very first bar of the window that predecessor has been sliced off — so a
    genuine cross landing exactly on the window's left edge would go uncounted.
    That produced cards reading "1 upward cross of 30" directly above a
    completed up/down/up pattern, which plainly needs two.
    """
    lead = full[-(window + 1):]
    offset = len(lead) - min(len(full), window)
    return [i - offset for i in find_upward_crosses(lead, threshold) if i - offset >= 0]


def _window_for(symbol: str, releases: dict, sessions: dict) -> EarningsWindow:
    """Read one symbol's earnings window out of what the run recorded."""
    next_date, last_date = releases.get(symbol, (None, None))
    if next_date is None and last_date is None:
        return CLEAR_WINDOW
    trading_days = [
        day for day in (to_release_date(p.date) for p in sessions.get(symbol, []))
        if day is not None
    ]
    return earnings_window(
        to_release_date(next_date), to_release_date(last_date), trading_days
    )


def _conviction(row: Row, config: Config, horizon):
    """Score one row against the configured weights.

    Built from the row rather than from the store, so the score is made of
    exactly what the card shows -- there is no way for the number to be
    computed off a fact the reader cannot see.
    """
    from .scoring import (
        composite, earnings_factor, growth_factor, pattern_factor,
        rule_one_factor, valuation_factor,
    )

    val = row.valuation
    growth = row.latest.earnings_growth if row.latest else None
    dip = _dip_rsi(row)
    return composite(
        [
            valuation_factor(
                val.price if val else None,
                val.fair_value if val else None,
                horizon.margin,
            ),
            rule_one_factor(row.rule_one),
            growth_factor(growth),
            earnings_factor(row.earnings),
            pattern_factor(
                dip if dip is not None else row.rsi,
                config.rsi.threshold,
                at_dip=dip is not None,
            ),
        ],
        config.scoring.weights,
    )


def _dip_rsi(row: Row) -> float | None:
    """The trough between the two crosses of the newest buy pattern.

    A double cross completes *above* the threshold by construction, so today's
    RSI says nothing about how oversold the setup got -- the depth that matters
    is the one between the crosses, and it is what separates a graze from a
    sell-off.
    """
    buys = [s for s in row.signals if s.direction == BUY]
    if not buys:
        return None
    newest = max(buys, key=lambda s: s.up2_date)
    window = [
        p.rsi for p in row.series
        if newest.up1_date <= p.date <= newest.up2_date and p.rsi is not None
    ]
    return min(window) if window else None


def _collect(store: Store, config: Config, horizon=None) -> list[Row]:
    horizon = horizon or config.horizon(DEFAULT_HORIZON)
    valuations = {v.symbol: v for v in store.latest_valuations()}
    signals = store.all_signals(horizon=horizon.key)
    releases = store.earnings_dates()
    readings = store.rule_one_readings()
    # Whether results are near is a fact about the company, not the timeframe,
    # so it is judged once against the daily bars -- "three trading days" means
    # the same thing on the 1h page as on the 1w one.
    sessions = {t.symbol: store.rsi_series(t.symbol, DEFAULT_HORIZON) for t in config.tickers}
    window = config.dashboard.chart_days
    rows: list[Row] = []

    for ticker in config.tickers:
        full = store.rsi_series(ticker.symbol, horizon.key)
        series = full[-window:]
        # A recorded pattern is history; a *live* one is a setup you could act
        # on now. Both crosses must sit inside the horizon's lookback measured
        # back from the latest bar, and RSI must still be on the signalling
        # side of the line. See `signals.signal_is_live`.
        liveness = replace(config.signal, window_days=horizon.window_days)
        sigs = [
            s for s in signals
            if s.symbol == ticker.symbol
            and signal_is_live(
                s, full, liveness,
                config.rsi.threshold if s.direction == BUY else config.rsi.overbought,
            )
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
                crosses=_visible_crosses(full, window, config.rsi.threshold),
                valuation=valuations.get(ticker.symbol),
                signals=sigs,
                currency=ticker.currency,
                markets=ticker.markets,
                horizon=horizon,
                earnings=_window_for(ticker.symbol, releases, sessions),
                rule_one=readings.get(ticker.symbol),
            )
        )
        rows[-1] = replace(rows[-1], conviction=_conviction(rows[-1], config, horizon))

    # Most actionable first: confirmed signals, then patterns awaiting a
    # fair-value check, then how oversold things currently are.
    # Suspended sits below every actionable verdict and above the merely
    # interesting: it is a real pattern you are being told to wait on, so it
    # should be findable without being led with.
    order = {
        "strong": 0, "signal": 1, "signal_checked": 2,
        "sell_strong": 3, "sell": 4, "suspended": 5, "rejected": 6,
        "oversold": 7, "watch": 8, "neutral": 9, "nodata": 10,
    }
    # Within a state, the second valuation ranks but never gates. The rocket
    # category currently holds Rule #1 scores from 2 to 10 and treats them
    # identically, so this is discrimination the page already needed -- and it
    # cannot suppress anything, which matters while Rule #1 is still unmeasured.
    rows.sort(key=lambda r: (
        order[r.state],
        -_rule_one_rank(r),
        r.rsi if r.rsi is not None else 999,
    ))
    return rows


def _rule_one_rank(row: Row) -> int:
    """A row's Rule #1 score for sorting, with no reading ranking mid-table.

    A company Rule #1 cannot read is not the worst in its category, it is
    unknown -- so it sits where a middling score would rather than at the
    bottom.
    """
    reading = row.rule_one
    if reading is None or not reading.applicable:
        return 5
    return reading.score


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


def _deal_of_the_day(rows: list[Row], horizon, threshold: float) -> str:
    """The single best fresh, confirmed buy — or nothing at all.

    One pick rather than a badge on several, because the point is to answer
    "what do I look at first" on a phone. Ranked by the largest discount to
    fair value, which is the ranking that matters once freshness and the
    valuation gate have already been satisfied — and *tied* on Rule #1, so
    when two picks are equally cheap against Morningstar the one the second
    valuation also likes wins. Rule #1 breaks ties here; it does not do the
    ranking, which was deliberately tuned and is left alone.

    Renders empty on the days nothing qualifies, which will be most of them:
    it needs a buy that fired, cleared the horizon's margin, has nothing
    known arguing against it, *and* completed within the last two bars. An
    empty slot is the honest answer, not a failure.
    """
    candidates = [r for r in rows if r.deal_discount is not None]
    if not candidates:
        return _no_deal(rows, horizon, threshold)

    best = max(candidates, key=lambda r: (r.deal_discount, _rule_one_rank(r)))
    val = best.valuation
    price = f"{val.price:,.2f}" if val else "—"
    fair = f"{val.fair_value:,.2f}" if val else "—"
    ccy = "" if best.currency == "USD" else f" {html.escape(best.currency)}"
    return f"""
<section class="lead" aria-label="Deal of the day">
  <p class="lead-kicker">Deal of the day</p>
  <div class="lead-line">
    <h2 class="lead-symbol">{html.escape(best.symbol)}</h2>
    <span class="lead-leader" aria-hidden="true"></span>
    <p class="lead-figure">{best.deal_discount * 100:.0f}<span class="lead-unit">%</span></p>
  </div>
  {_deal_rule_one(best)}
  <p class="lead-note">The pick of what fired today: second cross of
     {threshold:g} within the last {html.escape(horizon.fresh_label)}, and the
     widest gap to fair value of any of them — {price}{ccy} against
     {fair}{ccy}. Check the card for whether the valuation clears the
     {horizon.margin_pct} a strong buy needs.
     <span class="lead-lev">{horizon.leverage}× suggested</span></p>
</section>"""


def _deal_rule_one(row: Row) -> str:
    """The second valuation's read on the day's pick.

    Shown rather than ranked on. The deal is chosen by discount to fair value
    -- a ranking tuned deliberately and left alone -- and this says whether the
    other method agrees with the choice.
    """
    reading = row.rule_one
    if reading is None:
        return ""
    if not reading.applicable:
        return ('<p class="lead-r1">Rule #1 has no read on this one — '
                f'{html.escape(reading.reason)}.</p>')
    verdict = {
        "green": "agrees", "amber": "is lukewarm", "red": "disagrees",
    }.get(reading.band, "")
    return (f'<p class="lead-r1 lead-r1-{reading.band}">Rule&nbsp;#1 {verdict}: '
            f'<strong>{reading.score}/10</strong> — price demands '
            f'{reading.implied_growth:.1f}% a year, delivered '
            f'{reading.conservative_growth:.1f}–{reading.growth:.1f}%.</p>')


def _no_deal(rows: list[Row], horizon, threshold: float) -> str:
    """What the page says on the days nothing qualifies — which is most of them.

    Rendering nothing at all was the first instinct and it is the wrong one: an
    absent block is indistinguishable from a broken one, and the reader is left
    wondering whether the screener looked. This says it looked, says what the
    bar is, and says how close today came.

    Deliberately monochrome. The moment an empty state borrows the accent
    colour it starts competing with the real thing, and the whole point of one
    pick a day is that its absence is information too.
    """
    fresh = sum(1 for r in rows if r.fresh)
    if fresh:
        near = (
            f"{fresh} pattern{'' if fresh == 1 else 's'} fired in the last "
            f"{html.escape(horizon.fresh_label)}, "
            "but none was a buy trading below its fair value."
        )
    else:
        near = f"Nothing has fired in the last {html.escape(horizon.fresh_label)}."
    return f"""
<section class="lead lead-quiet" aria-label="No deal today">
  <p class="lead-kicker">No deal today</p>
  <p class="lead-note">{near} A deal needs both at once: a second cross of
     {threshold:g} within the last {html.escape(horizon.fresh_label)}, and a
     price below fair value.</p>
</section>"""


def _card(row: Row, config: Config, horizon) -> str:
    threshold = config.rsi.threshold
    rsi_text = f"{row.rsi:.1f}" if row.rsi is not None else "—"
    close_text = f"{row.latest.close:,.2f}" if row.latest else "—"
    ccy = "" if row.currency == "USD" else f' <span class="ccy">{html.escape(row.currency)}</span>' 

    pill_label = {
        "strong": "Strong buy 🚀",
        "signal": "Buy signal",
        "signal_checked": "Buy signal",
        "suspended": "Suspended ⚠️",
        "sell_strong": "Strong sell 🔻",
        "sell": "Sell signal",
        "rejected": "Pattern, gate failed",
        "oversold": "Oversold",
        "watch": "Near threshold",
        "neutral": "Neutral",
        "nodata": "No data",
    }[row.state]

    earnings_block = (
        f'<p class="earnings-warning">⚠️ {html.escape(row.earnings_note)}</p>'
        if row.suspended else ""
    )

    crosses = len(row.crosses)
    cross_note = (
        f"<strong>{crosses}</strong> upward cross{'' if crosses == 1 else 'es'} of "
        f"{threshold:g} in {len(row.series)} sessions"
    )

    if row.valuation:
        val = row.valuation
        _, passed = _gate(val, config, horizon.margin)
        upside = (val.fair_value / val.price - 1) * 100 if val.price else 0.0
        verdict = f"{upside:+.0f}% to fair value"
        gate_class = "pass" if passed else "fail"
        age_text, age_class = _freshness(val)
        valuation_block = f"""
        <dl class="valuation {gate_class}">
          <div><dt>Fair value</dt><dd>{val.fair_value:,.2f}</dd></div>
          <div><dt>Price</dt><dd>{val.price:,.2f}</dd></div>
          <div><dt>Verdict</dt><dd>{verdict}</dd></div>
        </dl>
        <p class="provenance{age_class}">{age_text} · needs {horizon.margin_pct}
           headroom on the {horizon.label} chart.</p>"""
    elif row.fired or row.sell_fired:
        side = "sell" if row.sell_fired and not row.fired else "buy"
        valuation_block = f"""
        <p class="valuation pending">{side.capitalize()} signal on RSI alone — confirm the
        fair value for a strong {side}.</p>"""
    else:
        valuation_block = """
        <p class="valuation none">No fair value recorded yet.</p>"""

    patterns = ""
    if row.signals:
        marks = ", ".join(
            f"{html.escape(s.up2_date)}"
            + ("↓" if s.direction == SELL else "")
            + (" ✓" if s.fired else " ✗" if s.valuation_known else "")
            for s in row.signals[-3:]
        )
        patterns = f'<p class="patterns">Pattern completed: {marks}</p>'

    growth = row.latest.earnings_growth if row.latest else None
    if growth is not None:
        _, eg_confirms = earnings_growth_passes(growth)
        eg_class = "pass" if eg_confirms else "fail"
        period_label = "TTM" if row.latest.earnings_growth_period == "ttm" else "FY"
        growth_block = (
            f'<p class="earnings {eg_class}">EPS growth ({period_label}): '
            f'<strong>{growth:+.1f}%</strong></p>'
        )
    else:
        growth_block = '<p class="earnings none">No earnings growth data yet.</p>'

    rule_one_block = _rule_one_block(
        row.rule_one, row.both_valuations_agree, row.currency
    )

    conviction_block = _conviction_block(row.conviction)

    leverage_block = ""
    if row.fired or row.sell_fired:
        leverage_block = (
            f'<p class="leverage"><span class="lev">{horizon.leverage}x</span>'
            f'<span class="lev-note">suggested for the {horizon.label} chart</span></p>'
        )

    symbol = html.escape(row.symbol)
    market_classes = " ".join(f"in-{m}" for m in row.markets)
    # Deliberately quiet: the timing is a modifier on the state, not a state
    # of its own, so it must not compete with the pill next to it.
    fresh_badge = (
        f'<span class="fresh" title="Second cross within the last '
        f'{html.escape(horizon.fresh_label)}">fresh</span>' if row.fresh else ""
    )
    return f"""<article class="card state-{row.state} {market_classes}">
  <header class="card-head">
    <div class="ident">
      <h3>{symbol}</h3>
      <span class="pill">{pill_label}</span>{fresh_badge}
    </div>
    <div class="readout">
      <div class="metric"><span class="k">RSI</span><span class="v">{rsi_text}</span></div>
      <div class="metric"><span class="k">Close</span><span class="v">{close_text}{ccy}</span></div>
    </div>
  </header>
  {_chart_svg(row, threshold)}
  {earnings_block}
  <p class="crosses">{cross_note}</p>
  {patterns}
  {valuation_block}
  {rule_one_block}
  {growth_block}
  {conviction_block}
  {leverage_block}
  <div class="actions">
    <a class="btn primary" href="{html.escape(row.morningstar_url)}"
       target="_blank" rel="noopener noreferrer">Check fair value on Morningstar</a>
    <a class="btn" href="{html.escape(row.tradingview_url)}"
       target="_blank" rel="noopener noreferrer">TradingView</a>
  </div>
</article>"""


def _rule_one_value(reading, ccy: str = "") -> str:
    """Rule #1's answer in money, as one number on the score's own line.

    Started as a two-cell list under the score -- a low-high band, plus the
    margin-of-safety price, plus a line naming the growth rates and a "wide"
    badge when the two rates disagreed. Every part of that was true and the
    whole was unreadable: five numbers and two pieces of jargon to say "this
    company looks worth about four pounds".

    So the card carries one number and the reasoning moved into the tooltip
    that was already there. The honest caveat has not gone away -- a sticker is
    one growth assumption compounded ten times, and the pessimistic and
    base-case readings can differ by a factor of nineteen -- it is just no
    longer shouted at someone scanning twenty cards on a phone. The range, both
    growth rates and the margin-of-safety price are all one hover away.

    The base case is the number shown, not the midpoint: it is the one Rule #1
    actually computes, and averaging two assumptions produces a third that
    nobody made.
    """
    if reading is None or not reading.applicable or reading.value_band is None:
        # A flat or shrinking earner prices out at a P/E of 2 whatever the
        # company -- see `RuleOne.value_band`. The score above still stands, so
        # the line simply carries no price rather than an explanation nobody
        # asked for.
        return ""
    unit = "" if ccy in ("", "USD") else f" {html.escape(ccy)}"
    return (f'<span class="r1-worth"><span class="r1-sep">·</span> worth '
            f'<strong>{reading.sticker:,.2f}</strong>{unit}</span>')


def _rule_one_block(reading, agrees: bool = False, ccy: str = "") -> str:
    """The Buffett score: a small coloured box, and what it is.

    Three things were unreadable when this said `[10] RULE #1`:

    * **the scale** -- ten out of what? So it reads `10/10`.
    * **the polarity** -- in finance a high score is as often a risk rating as
      a recommendation. Colour carries it, and `/10` beside green/amber/red is
      a pattern nobody has to be taught.
    * **the name** -- "Rule #1" is Phil Town's title and means nothing to
      someone who has not read him. "Buffett score" is what it actually is:
      his method is explicitly a mechanisation of Buffett's. The book's name
      stays in the tooltip for anyone who wants to look it up.

    The agreement mark rides here rather than in the header for the same
    reason. As a bare "both agree" badge beside the status pill it left the
    reader to guess both *what* and agreeing about *what*; next to the score it
    can simply say what it means, and the header goes back to carrying the
    verdict and its freshness alone.
    """
    if reading is None:
        return ""
    if not reading.applicable:
        return ('<p class="ruleone"><span class="r1-box r1-na" '
                'title="Buffett score (Phil Town&#39;s Rule #1): '
                f'{html.escape(reading.reason)}">–</span>'
                '<span class="r1-label">Buffett score</span></p>')

    detail = (
        f"Buffett score (Phil Town's Rule #1): {reading.score} out of 10. "
        f"Today's price demands {reading.implied_growth:.1f}% growth a year for "
        f"ten years; this company has delivered "
        f"{reading.conservative_growth:.1f}–{reading.growth:.1f}%. "
        f"Sticker {reading.sticker:,.2f}, margin of safety {reading.mos:,.2f}, "
        f"Big Four {reading.big_four}/4."
        # Everything that used to be printed under the score lives here now.
        # The spread between the two growth assumptions is the real caveat on
        # the price, and it belongs where someone asking about the price will
        # find it, not as a badge on a card being scanned.
        + _spread_note(reading)
        + (f" ⚠ {reading.caution}." if reading.caution else "")
    )
    warn = '<span class="r1-warn" title="See the score for why">⚠</span>' if reading.caution else ""
    agree = (
        '<span class="r1-agree" title="Morningstar&#39;s fair value and the Buffett '
        'score are worked out in completely different ways, and here they reach the '
        'same conclusion">✓ agrees with fair value</span>' if agrees else ""
    )
    return (f'<p class="ruleone"><span class="r1-box r1-{reading.band}" '
            f'title="{html.escape(detail)}">{reading.score}'
            f'<span class="r1-of">/10</span></span>'
            f'<span class="r1-label">Buffett score</span>'
            f'{_rule_one_value(reading, ccy)}{warn}{agree}</p>')


def _spread_note(reading) -> str:
    """How far apart the two growth assumptions put the price, in words.

    This was a "wide" badge on the card, which asked the reader to know what
    was wide and why it mattered. As a sentence in the tooltip it can just say
    so.
    """
    band = reading.value_band
    if band is None or not band[0]:
        return ""
    spread = band[1] / band[0]
    if spread < 3:
        return ""
    return (f" On the more pessimistic growth rate it works out at "
            f"{band[0]:,.2f} instead — a {spread:.0f}x spread, so trust the "
            f"score here rather than the price.")


def _conviction_block(comp) -> str:
    """The weighted score, and the bar that takes it apart.

    Asked for as "a coefficient of 1-10 for how good this is, and also
    visualize that". The bar is the visualisation and it is a segmented one on
    purpose: a single filled bar would show only the total, and the total is
    the least interesting part of a weighted model. Each segment is one
    factor's actual contribution -- its weight times how well it scored -- so
    the width of a segment answers "why is this an 8?" directly, and the empty
    remainder answers "what would have made it a 10?".

    Kept below the factors it summarises rather than beside the verdict pill,
    because in shadow mode it is a second opinion on the card, not the card's
    conclusion.
    """
    if comp is None:
        return ""
    span = SCORE_MAX - SCORE_MIN
    segments = ""
    for c in comp.contributions:
        # A factor that was read and scored nothing and one that could not be
        # read both contribute no width, which would make them look alike. The
        # "% known" chip beside the score is what separates them: it appears
        # only when something was unreadable and names it in its tooltip.
        if not c.weight or c.strength is None or c.points <= 0:
            continue
        width = 100.0 * c.points / span
        note = (f"{c.label}: {c.detail} · scored {c.strength:.0%} "
                f"at weight {c.weight:g}, worth {c.points:.1f} of {span}")
        segments += (f'<span class="seg seg-{c.key}" style="flex:{width:.2f}" '
                     f'title="{html.escape(note)}"></span>')

    missing = [c.label for c in comp.contributions
               if c.weight and c.strength is None]
    # Only when the score is genuinely thin. It used to appear whenever
    # anything at all was unreadable, which is most cards, so it read as
    # decoration rather than a warning -- and a warning that is always on is
    # not a warning.
    coverage = (
        f'<span class="cv-thin" title="Only {comp.coverage:.0%} of the evidence '
        f'was available — {html.escape(", ".join(missing))} could not be read. '
        f'The score is worked out from the rest.">thin</span>'
        if comp.thin else ""
    )
    # The whole model, in the order it contributed. This is the "why", and it
    # belongs on hover: nobody scanning twenty cards wants five factor
    # breakdowns competing with the price.
    parts = [f"{c.label} {c.points:.1f}"
             for c in sorted(comp.known_factors, key=lambda c: -c.points)
             if c.points > 0]
    detail = (f"{comp.score} of 10, from: " + ", ".join(parts)
              if parts else "nothing readable scored")
    if missing:
        detail += f". Not counted: {', '.join(missing)}"
    return (
        f'<p class="conviction"><span class="cv-box cv-{comp.band}" '
        f'title="{html.escape(detail)}">{comp.score}'
        f'<span class="cv-of">/10</span></span>'
        f'<span class="cv-label">Conviction</span>{coverage}'
        f'<span class="cv-bar" role="img" aria-label="{html.escape(detail)}">'
        f'{segments}<span class="seg seg-rest"></span></span></p>'
    )


def _gate(val: Valuation, config: Config, margin: float = 0.0) -> tuple[bool, bool]:
    return valuation_passes(val.price, val.fair_value, config.signal, margin)


def render(rows: list[Row], config: Config, horizon=None, standalone: bool = True) -> str:
    horizon = horizon or config.horizon(DEFAULT_HORIZON)
    threshold = config.rsi.threshold
    generated = dt.datetime.now().strftime("%d %B %Y, %H:%M")

    tracked = len(rows)
    oversold = sum(1 for r in rows if r.rsi is not None and r.rsi < threshold)
    patterns = sum(len(r.signals) for r in rows)
    strong = sum(1 for r in rows if r.strong)
    fired = sum(1 for r in rows if r.fired)
    sells = sum(1 for r in rows if r.sell_fired)
    high_conviction = sum(
        1 for r in rows
        if r.conviction is not None and r.conviction.score >= GREEN_AT
    )
    dated = [r.latest.date for r in rows if r.latest]
    as_of = max(dated) if dated else "—"

    cards = "\n".join(_card(r, config, horizon) for r in rows)

    def page_for(h) -> str:
        return "index.html" if h.key == DEFAULT_HORIZON else f"{h.key}.html"

    horizon_links = "".join(
        f'<a class="tf{" on" if h.key == horizon.key else ""}" '
        f'href="{page_for(h)}">{html.escape(h.label)}</a>'
        for h in config.horizons
    ) + '<a class="tf" href="history.html">Historical</a>'
    market_tabs = '<label for="mk-all">All</label>' + "".join(
        f'<label for="mk-{m}">{html.escape(MARKET_LABELS[m])}</label>'
        for m in config.active_markets
    )
    # Radios sit ahead of the sheet so `:checked ~ .sheet .card` can reach the
    # cards. Whole filter is CSS -- no script, works with JS disabled.
    market_radios = '<input type="radio" name="mk" id="mk-all" checked>' + "".join(
        f'<input type="radio" name="mk" id="mk-{m}">' for m in config.active_markets
    )

    masthead = f"""<header class="masthead">
  <div class="title-block">
    <p class="eyebrow">Relative Strength Screener · {html.escape(horizon.label)} chart</p>
    <h1>RSI Screener</h1>
    <p class="standfirst">
      Tracking {tracked} market leaders on the {html.escape(horizon.label)}
      chart. <strong>Buy</strong> on two upward crossings of RSI {threshold:g},
      <strong>sell</strong> on two downward crossings of
      {config.rsi.overbought:g} — both within {horizon.window_days} days of now,
      with RSI still on the signalling side. A fair value at least
      {horizon.margin_pct} away confirms; this timeframe suggests
      {horizon.leverage}x.
    </p>
    <nav class="timeframes" aria-label="RSI timeframe">{horizon_links}</nav>
  </div>
  <dl class="aggregates">
    <div><dt>Session</dt><dd>{html.escape(as_of)}</dd></div>
    <div><dt>Tracked</dt><dd>{tracked}</dd></div>
    <div><dt>Below {threshold:g}</dt><dd class="{'hot' if oversold else ''}">{oversold}</dd></div>
    <div><dt>Patterns</dt><dd>{patterns}</dd></div>
    <div><dt>Signals</dt><dd class="{'warn' if fired else ''}">{fired}</dd></div>
    <div><dt>Strong 🚀</dt><dd class="{'good' if strong else ''}">{strong}</dd></div>
    <div><dt>Sells 🔻</dt><dd class="{'hot' if sells else ''}">{sells}</dd></div>
    <div title="Names scoring {GREEN_AT:g} or better on the weighted conviction
      score. It ranks and explains; it does not decide — the rocket above still
      comes from the fair-value rule."><dt>Conviction ≥{GREEN_AT:g}</dt>
      <dd class="{'good' if high_conviction else ''}">{high_conviction}</dd></div>
  </dl>
</header>"""

    body = f"""{market_radios}
<div class="sheet">
{masthead}
{_deal_of_the_day(rows, horizon, threshold)}
<nav class="market-tabs" aria-label="Market">{market_tabs}</nav>
<main class="grid">
{cards}
</main>
<footer class="colophon">
  <p>Generated {generated} · RSI ({config.rsi.period}) from TradingView,
     {html.escape(horizon.label)} bars · History from Yahoo Finance ·
     Fair value from Morningstar</p>
  <p class="disclaimer">Leverage figures are a fixed number attached to each
     timeframe, not a calculation from the signal. Leverage multiplies losses
     as readily as gains. Nothing here is financial advice.</p>
</footer>
</div>"""

    head = (f"<title>RSI Screener · {html.escape(horizon.label)}</title>\n"
            f"<style>{_CSS}</style>")
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
  --green-soft: #3F9A72;
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
    --green-soft: #8FD9B6;
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
  --green-soft: #8FD9B6;
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
  --green-soft: #3F9A72;
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
.aggregates dd.warn { color: var(--green-soft); font-weight: 600; }
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
/* Buy signals are the lighter green; a confirmed one (state-strong, above)
   keeps the dark saturated green. Same colour family on purpose -- the two
   differ in conviction, not in kind, and blue read as a third category. */
.card.state-signal,
.card.state-signal_checked { border-top: 3px solid var(--green-soft); }
.card.card.state-sell_strong { border-left-color: var(--crimson); }
.card.state-sell        { border-left-color: var(--crimson); }
.state-sell_strong .pill, .state-sell .pill {
  background: color-mix(in srgb, var(--crimson) 12%, transparent);
  color: var(--crimson);
  border-color: color-mix(in srgb, var(--crimson) 40%, transparent);
}

/* Suspended: a real pattern the page is telling you to wait on. Amber rather
   than red because nothing is wrong -- the setup is fine, the timing isn't --
   and dashed because it resolves itself the session after results. */
.card.state-suspended { border-left-color: var(--warn); }
.state-suspended .pill {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 40%, transparent);
}
.earnings-warning {
  margin: 10px 0 0;
  padding: 8px 10px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  border: 1px dashed color-mix(in srgb, var(--warn) 38%, transparent);
  border-radius: 6px;
}

/* Rule #1: one small box with a number in it. A second opinion that ranks the
   page and decides nothing should not out-shout the verdict it is seconding —
   the reasoning lives in the box's tooltip. */
.ruleone { margin: 8px 0 0; display: flex; align-items: center; gap: 7px;
           flex-wrap: wrap; }
.r1-box {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; height: 22px; padding: 0 6px; border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums;
  cursor: help; border: 1px solid transparent;
}
.r1-green { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green);
            border-color: color-mix(in srgb, var(--green) 38%, transparent); }
.r1-amber { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn);
            border-color: color-mix(in srgb, var(--warn) 38%, transparent); }
.r1-red   { background: color-mix(in srgb, var(--crimson) 13%, transparent); color: var(--crimson);
            border-color: color-mix(in srgb, var(--crimson) 32%, transparent); }
.r1-na    { background: transparent; color: var(--ink-3); border-color: var(--rule); }
.r1-label {
  font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 700;
}
.r1-warn { color: var(--warn); font-size: 11px; cursor: help; }
.r1-of { font-size: 9.5px; font-weight: 500; opacity: .72; letter-spacing: -.02em; }
/* Reads as a sentence with the score it qualifies, which "both agree" beside
   the status pill never could. */
.r1-agree {
  margin-left: auto; font-size: 11px; color: var(--green); cursor: help;
  white-space: nowrap;
}

/* Rule #1's answer in money, riding on the score's own line. It used to be a
   bordered list underneath with a second price and a growth-rate footnote —
   five numbers to say "worth about four pounds". */
.r1-worth { font-size: 11.5px; color: var(--ink-2); }
.r1-sep { color: var(--ink-3); opacity: .6; margin-right: 1px; }
.r1-worth strong {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums; font-weight: 700;
}

/* The weighted conviction score. Same chip shape as the Buffett score — they
   are both 1–10 second opinions, and making them look alike is the point. */
.conviction { margin: 10px 0 0; display: flex; align-items: center; gap: 7px;
              flex-wrap: wrap; }
.cv-box {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; height: 22px; padding: 0 6px; border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums;
  cursor: help; border: 1px solid transparent;
}
.cv-green { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green);
            border-color: color-mix(in srgb, var(--green) 38%, transparent); }
.cv-amber { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn);
            border-color: color-mix(in srgb, var(--warn) 38%, transparent); }
.cv-red   { background: color-mix(in srgb, var(--crimson) 13%, transparent); color: var(--crimson);
            border-color: color-mix(in srgb, var(--crimson) 32%, transparent); }
.cv-label {
  font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 700;
}
.cv-of { font-size: 9.5px; font-weight: 500; opacity: .72; letter-spacing: -.02em; }
/* Shown only when the score really is built on too little — see the comment
   in `_conviction_block`. A warning that is always on is not a warning. */
.cv-thin {
  font-size: 9.5px; cursor: help; white-space: nowrap; color: var(--warn);
  text-transform: uppercase; letter-spacing: .08em; font-weight: 700;
}

/* One segment per factor, width proportional to what it actually contributed.
   A plain filled bar would show only the total, which is the least interesting
   part of a weighted score. It sits on the score's own line now rather than
   under it: the card had grown to five lines of scoring metadata. */
.cv-bar {
  display: flex; flex: 1 1 auto; margin-left: auto; max-width: 132px;
  height: 6px; gap: 1px; background: var(--rule); overflow: hidden;
}
.cv-bar .seg { display: block; min-width: 0; }
.seg-rest { flex: 1 1 auto; background: transparent; }
.seg-valuation { background: var(--green); }
.seg-ruleone   { background: color-mix(in srgb, var(--green) 55%, var(--accent)); }
.seg-growth    { background: var(--accent); }
.seg-earnings  { background: color-mix(in srgb, var(--accent) 45%, var(--ink-3)); }
.seg-pattern   { background: var(--ink-3); }

.state-rejected { border-top: 3px dashed var(--ink-3); }
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
.state-signal_checked .pill {
  color: var(--green-soft);
  background: color-mix(in srgb, var(--green-soft) 12%, transparent);
  border-color: color-mix(in srgb, var(--green-soft) 40%, transparent);
}
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

.earnings {
  margin: 0;
  padding: 6px 10px;
  font-size: 12.5px;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--ink-3);
}
.earnings strong {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.earnings.pass { border-left-color: var(--green); }
.earnings.fail { border-left-color: var(--crimson); }
.earnings.none {
  font-style: italic;
  color: var(--ink-3);
  border-left-color: var(--rule);
}

/* ---- timeframe + market selectors ------------------------------------
   The market filter is pure CSS: hidden radio inputs sit before .sheet, and
   each `:checked` state hides the cards that don't carry the matching class.
   No JavaScript, so it still works from a file:// URL or with JS disabled.
   The timeframe selector can't work that way -- each horizon has genuinely
   different RSI data -- so those are links to sibling pages. */
input[name="mk"] { position: absolute; opacity: 0; pointer-events: none; }

.timeframes { display: flex; gap: 4px; margin-top: 10px; flex-wrap: wrap; }
.timeframes .tf {
  padding: 4px 11px;
  font-size: 12px;
  letter-spacing: .02em;
  text-decoration: none;
  color: var(--ink-2);
  border: 1px solid var(--rule);
  border-radius: 999px;
  background: var(--card);
}
.timeframes .tf:hover { border-color: var(--accent); color: var(--accent); }
.timeframes .tf.on {
  background: var(--ink); color: var(--paper); border-color: var(--ink); font-weight: 600;
}

.market-tabs {
  display: flex; gap: 4px; flex-wrap: wrap;
  margin: 0 0 14px; padding-top: 12px; border-top: 1px solid var(--rule);
}
.market-tabs label {
  padding: 4px 11px;
  font-size: 12px;
  cursor: pointer;
  color: var(--ink-2);
  border: 1px solid var(--rule);
  border-radius: 999px;
  user-select: none;
}
.market-tabs label:hover { border-color: var(--accent); color: var(--accent); }

/* The per-market rules are appended below, generated from MARKETS. */


/* ---- leverage -------------------------------------------------------- */
.leverage {
  display: flex; align-items: baseline; gap: 8px;
  margin: 0; padding: 7px 10px;
  border: 1px solid var(--rule); border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.leverage .lev {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 15px; font-weight: 700; color: var(--accent);
}
.leverage .lev-note { font-size: 11px; color: var(--ink-3); }

.disclaimer { margin-top: 6px; font-size: 10.5px; color: var(--ink-3); max-width: 62ch; }

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



/* --------------------------------------------------------- colophon */

.colophon {
  margin-top: 36px;
  padding-top: 14px;
  border-top: 1px solid var(--rule);
  font-size: 11.5px;
  color: var(--ink-3);
}

.colophon p { margin: 0; }

/* ---- the lead, and the freshness marker -----------------------------
   Set as a front-page lead rather than a callout box: kicker in small caps,
   ticker in the masthead's serif, and the figure in the same tabular mono
   every other number on the page uses. The dotted leader between them is
   borrowed from a printed price list -- it is the one flourish here, and it
   costs nothing but a border. */
.lead {
  margin: 18px 0 16px;
  padding: 16px 18px 14px;
  border-top: 2px solid var(--green);
  border-bottom: 1px solid var(--rule);
  background: color-mix(in srgb, var(--green) 4%, transparent);
}
.lead-kicker {
  margin: 0;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--green);
}
.lead-line {
  display: flex;
  align-items: baseline;
  gap: 0;
  margin-top: 4px;
}
.lead-symbol {
  margin: 0;
  font-family: Georgia, "Iowan Old Style", "Times New Roman", serif;
  font-weight: 400;
  font-size: clamp(30px, 5.5vw, 46px);
  letter-spacing: -.015em;
  line-height: 1.05;
}
/* Grows to fill whatever the symbol and figure leave behind. */
.lead-leader {
  flex: 1 1 auto;
  min-width: 24px;
  margin: 0 10px 8px;
  border-bottom: 2px dotted color-mix(in srgb, var(--green) 45%, transparent);
}
.lead-figure {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-size: clamp(30px, 5.5vw, 46px);
  line-height: 1.05;
  font-weight: 600;
  color: var(--green);
  white-space: nowrap;
}
.lead-unit { font-size: .5em; margin-left: 2px; vertical-align: .55em; }
.lead-note {
  margin: 6px 0 0;
  max-width: 74ch;
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink-2);
}
.lead-lev {
  white-space: nowrap;
  font-weight: 600;
  color: var(--green);
}

/* The empty state. Monochrome on purpose: the day there is no deal, this must
   not read as though there were one. */
.lead-quiet {
  border-top-color: var(--rule);
  background: none;
  padding-bottom: 12px;
}
.lead-quiet .lead-kicker { color: var(--ink-3); }

/* Freshness rides alongside the state pill, so it has to stay quieter than
   one: no fill, a single dot, and the same tracked small caps as the eyebrow.
   A filled badge here read as a second status and fought the first. */
.fresh {
  margin-left: 8px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--green-soft);
  white-space: nowrap;
}
.fresh::before {
  content: "";
  display: inline-block;
  width: 5px; height: 5px;
  margin-right: 5px;
  border-radius: 50%;
  background: var(--green-soft);
  vertical-align: .12em;
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
"""


def _market_filter_css() -> str:
    """The `:checked` rules behind the market filter, one pair per market.

    Generated rather than hand-written because the chips already come from
    `MARKETS` (see `_page`). When the two were maintained separately, adding a
    market rendered a chip with no matching rule behind it — and a filter with
    no hide rule doesn't fail loudly, it just quietly shows everything.
    """
    highlight = (
        "  background: var(--accent); color: var(--paper); "
        "border-color: var(--accent);\n}"
    )
    blocks = [
        '#mk-all:checked ~ .sheet .market-tabs label[for="mk-all"] {\n' + highlight
    ]
    blocks += [
        f"#mk-{m}:checked ~ .sheet .card:not(.in-{m}) {{ display: none; }}\n"
        f'#mk-{m}:checked ~ .sheet .market-tabs label[for="mk-{m}"] {{\n' + highlight
        for m in MARKETS
    ]
    return "\n".join(blocks)


_CSS += "\n" + _market_filter_css()
