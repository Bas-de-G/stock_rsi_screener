"""Tests for the dashboard's state model and rendering.

The state a card shows is the whole point of the page: an RSI pattern is a buy
signal on its own, and only a fair value that agrees with it earns the rocket.
"""

from __future__ import annotations

import pytest

from screener.config import load_config
from screener.dashboard import Row, build_dashboard, render
from screener.storage import RsiPoint, Signal, Store, Valuation

CONFIG_YAML = """
tickers:
  - {symbol: NVDA, tradingview: "NASDAQ:NVDA", morningstar: xnas/nvda}
  - {symbol: IBM,  tradingview: "NYSE:IBM",    morningstar: xnys/ibm}
rsi: {period: 14, threshold: 30, interval: "1D"}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: false
storage: {database: data/t.db, csv_dir: data}
dashboard: {output: data/t.html, chart_days: 90}
"""


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return load_config(path)


def signal(
    symbol="NVDA", up2="2026-07-23", known=False, confirms=False, fired=True,
    eg_known=False, eg_confirms=False,
):
    """A recorded pattern. `fired` defaults True: under the configured
    lenient mode the RSI pattern is a buy signal on its own. Earnings growth
    defaults unknown, matching a pattern recorded before this factor existed."""
    return Signal(
        symbol=symbol, up1_date="2026-07-16", down_date="2026-07-22", up2_date=up2,
        price=200.0 if known else None, fair_value=220.0 if known else None,
        valuation_known=known, valuation_pass=confirms, fired=fired, recorded_at="now",
        earnings_growth=15.0 if eg_known else None,
        earnings_growth_known=eg_known, earnings_growth_pass=eg_confirms,
    )


def row(**kw):
    base = dict(
        symbol="NVDA",
        morningstar_url="https://www.morningstar.com/stocks/xnas/nvda/quote",
        tradingview_url="https://www.tradingview.com/symbols/NASDAQ-NVDA/technicals/",
        series=[RsiPoint("NVDA", "2026-07-27", 100.0, 45.0, "live:tradingview")],
        crosses=[],
        valuation=None,
        signals=[],
        currency="USD",
    )
    base.update(kw)
    return Row(**base)


# ------------------------------------------------------------- states


def test_pattern_alone_is_a_buy_signal():
    """RSI pattern fires on its own — fair value only grades it."""
    r = row(signals=[signal(known=False, fired=True)])
    assert r.state == "signal"
    assert r.fired is True
    assert r.strong is False


def test_confirming_fair_value_makes_it_a_strong_buy():
    r = row(signals=[signal(known=True, confirms=True, fired=True)])
    assert r.state == "strong"
    assert r.strong is True


def test_contradicting_fair_value_leaves_it_a_plain_buy_signal():
    """Trading above fair value downgrades the signal, it doesn't cancel it."""
    r = row(signals=[signal(known=True, confirms=False, fired=True)])
    assert r.state == "signal_checked"
    assert r.fired is True
    assert r.strong is False


def test_earnings_growth_alone_makes_it_a_strong_buy():
    """No fair value recorded yet, but earnings growth alone confirms —
    same lenient rule the dashboard already used for fair value by itself."""
    r = row(signals=[signal(known=False, fired=True, eg_known=True, eg_confirms=True)])
    assert r.state == "strong"
    assert r.strong is True


def test_both_factors_confirming_is_strong():
    r = row(signals=[
        signal(known=True, confirms=True, fired=True, eg_known=True, eg_confirms=True)
    ])
    assert r.strong is True


def test_fair_value_confirms_but_earnings_shrinking_is_not_strong():
    """The value-trap case: cheap and oversold, but earnings declining —
    one dissenting known factor withholds the rocket."""
    r = row(signals=[
        signal(known=True, confirms=True, fired=True, eg_known=True, eg_confirms=False)
    ])
    assert r.state == "signal_checked"
    assert r.strong is False


def test_strict_mode_pattern_that_did_not_fire_is_rejected():
    r = row(signals=[signal(known=True, confirms=False, fired=False)])
    assert r.state == "rejected"
    assert r.fired is False


def test_signal_state_outranks_current_rsi_level():
    """A fired signal still shows even if RSI has since recovered."""
    r = row(
        series=[RsiPoint("NVDA", "2026-07-27", 100.0, 65.0, "live:tradingview")],
        signals=[signal(fired=True)],
    )
    assert r.state == "signal"


def test_a_strong_buy_wins_over_a_later_plain_signal():
    r = row(signals=[
        signal(up2="2026-01-05", known=True, confirms=True, fired=True),
        signal(up2="2026-07-23", known=False, fired=True),
    ])
    assert r.state == "strong"


@pytest.mark.parametrize(
    "rsi,expected", [(25.0, "oversold"), (35.0, "watch"), (55.0, "neutral")]
)
def test_states_without_a_pattern_follow_rsi(rsi, expected):
    r = row(series=[RsiPoint("NVDA", "2026-07-27", 100.0, rsi, "live:tradingview")])
    assert r.state == expected


def test_empty_series_is_nodata():
    assert row(series=[]).state == "nodata"


def test_any_checked_valuation_marks_the_signal_as_checked():
    """One pattern was checked (and disagreed), a later one wasn't. The card
    reports that a valuation exists rather than inviting a fresh check."""
    r = row(signals=[
        signal(up2="2026-01-05", known=True, confirms=False, fired=True),
        signal(up2="2026-07-23", known=False, fired=True),
    ])
    assert r.state == "signal_checked"
    assert r.strong is False


# ------------------------------------------------------------ rendering


def test_render_is_self_contained(config):
    """No external fetches: the CSP on a published page blocks them anyway."""
    html = render([row()], config)
    assert "<script" not in html.lower()
    assert "http://" not in html.replace("http://www.w3.org", "")
    for forbidden in ("cdn.", "googleapis", "unpkg", "@import"):
        assert forbidden not in html


def test_render_includes_both_theme_definitions(config):
    html = render([row()], config)
    assert "prefers-color-scheme: dark" in html
    assert ':root[data-theme="dark"]' in html
    assert ':root[data-theme="light"]' in html


def test_fragment_mode_omits_the_document_skeleton(config):
    """Published artifacts are wrapped by the host, so we must not double up."""
    fragment = render([row()], config, standalone=False)
    assert "<!doctype" not in fragment.lower()
    assert "<body" not in fragment.lower()
    assert "<title>" in fragment


def test_standalone_mode_is_a_full_document(config):
    html = render([row()], config)
    assert html.lower().startswith("<!doctype html>")
    assert "<body>" in html


def test_morningstar_button_points_at_the_right_ticker(config):
    html = render([row()], config)
    assert "https://www.morningstar.com/stocks/xnas/nvda/quote" in html
    assert 'rel="noopener noreferrer"' in html


def test_unconfirmed_signal_invites_a_fair_value_check(config):
    html = render([row(signals=[signal(known=False, fired=True)])], config)
    assert "Buy signal" in html
    assert "confirm the fair value" in html.lower()


def test_strong_buy_card_carries_the_rocket(config):
    html = render([row(signals=[signal(known=True, confirms=True, fired=True)])], config)
    assert "Strong buy 🚀" in html


def test_growing_earnings_are_shown_and_flagged_as_passing(config):
    r = row(series=[
        RsiPoint("NVDA", "2026-07-27", 100.0, 45.0, "live:tradingview", 32.6, "ttm")
    ])
    html = render([r], config)
    assert "+32.6%" in html
    assert 'class="earnings pass"' in html
    assert "TTM" in html


def test_shrinking_earnings_are_flagged_as_failing(config):
    r = row(series=[
        RsiPoint("NVDA", "2026-07-27", 100.0, 45.0, "live:tradingview", -47.1, "ttm")
    ])
    html = render([r], config)
    assert "-47.1%" in html
    assert 'class="earnings fail"' in html


def test_fy_fallback_is_labelled_distinctly_from_ttm(config):
    """SanDisk-style case: too new for a trailing year, so FY is what's shown."""
    r = row(series=[
        RsiPoint("NVDA", "2026-07-27", 100.0, 45.0, "live:tradingview", 22.7, "fy")
    ])
    html = render([r], config)
    assert "FY" in html


def test_no_earnings_data_shows_a_placeholder(config):
    html = render([row()], config)  # default row() series has no earnings_growth
    assert 'class="earnings none"' in html
    assert "No earnings growth data yet" in html


def test_non_usd_close_shows_its_currency(config):
    """Rolls-Royce quotes in pence; a bare number would read as dollars."""
    html = render([row(symbol="RR", currency="GBX")], config)
    assert "GBX" in html
    usd = render([row(symbol="IBM", currency="USD")], config)
    assert 'class="ccy"' not in usd


def test_symbols_are_html_escaped(config):
    html = render([row(symbol="A&B")], config)
    assert "A&amp;B" in html
    assert "<h3>A&B</h3>" not in html


def test_chart_marks_every_upward_cross(config):
    series = [
        RsiPoint("NVDA", f"2026-07-{d:02d}", 100.0, rsi, "backfill:yahoo")
        for d, rsi in [(1, 25.0), (2, 34.0), (3, 27.0), (4, 36.0)]
    ]
    html = render([row(series=series, crosses=[1, 3])], config)
    assert html.count('class="cross"') == 2
    assert "Crossed 30 upward on 2026-07-02" in html


def test_short_series_degrades_instead_of_drawing_a_broken_plot(config):
    html = render([row(series=[])], config)
    assert "Not enough history" in html
    assert "<polyline" not in html


# --------------------------------------------------------- end to end


def test_build_dashboard_writes_a_file(tmp_path, config):
    db = tmp_path / "s.db"
    with Store(db) as store:
        for day, rsi in [(20, 25.0), (21, 34.0), (22, 27.0), (23, 36.0)]:
            store.upsert_rsi_point(
                RsiPoint("NVDA", f"2026-07-{day}", 190.0, rsi, "backfill:yahoo")
            )
        store.upsert_valuation(
            Valuation("NVDA", "2026-07-23", 190.0, 220.0, source="manual")
        )
        out = build_dashboard(store, config, tmp_path / "dash.html")

    assert out.exists()
    text = out.read_text()
    assert "NVDA" in text
    assert "220.00" in text
    assert "below fair value" in text


# ------------------------------------------------------- freshness line


import datetime as dt

from screener.dashboard import _freshness


def valuation(checked, source="manual"):
    return Valuation(
        symbol="IBM", date="2026-07-28", price=217.0, fair_value=225.0,
        fair_value_date=checked, source=source,
    )


def days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def test_freshness_reads_the_checked_date_not_the_row_date():
    """`date` is rewritten every run, so it never looks old — `fair_value_date`
    carries the `checked:` value from the YAML and is what actually ages."""
    text, _ = _freshness(valuation(days_ago(5)))
    assert text == "Checked 5 days ago"


def test_freshness_today_and_yesterday_read_naturally():
    assert _freshness(valuation(days_ago(0)))[0] == "Checked today"
    assert _freshness(valuation(days_ago(1)))[0] == "Checked yesterday"


def test_freshness_rolls_up_to_months():
    assert "month" in _freshness(valuation(days_ago(75)))[0]
    assert _freshness(valuation(days_ago(120)))[0] == "Checked about 4 months ago"


def test_recent_values_are_not_flagged_stale():
    assert _freshness(valuation(days_ago(30)))[1] == ""
    assert _freshness(valuation(days_ago(90)))[1] == ""


def test_values_past_a_quarter_are_flagged_stale():
    """Morningstar revises estimates roughly quarterly."""
    assert _freshness(valuation(days_ago(91)))[1] == " stale"
    assert _freshness(valuation(days_ago(400)))[1] == " stale"


def test_a_missing_checked_date_falls_back_to_the_row_date():
    text, css = _freshness(valuation(None))
    assert text == "Recorded 2026-07-28"
    assert css == ""


def test_an_unparseable_checked_date_is_shown_verbatim():
    """Someone typing `checked: last Tuesday` shouldn't crash the dashboard."""
    text, css = _freshness(valuation("last Tuesday"))
    assert text == "Checked last Tuesday"
    assert css == ""


def test_stale_marker_reaches_the_rendered_page(config, tmp_path):
    db = tmp_path / "t.db"
    with Store(db) as store:
        for i in range(20):
            store.upsert_rsi_point(
                RsiPoint("IBM", f"2026-07-{i + 1:02d}", 200.0, 45.0, "backfill:yahoo")
            )
        store.upsert_valuation(valuation(days_ago(200)))
        out = build_dashboard(store, config, tmp_path / "out.html")
    assert "provenance stale" in out.read_text()


def test_provenance_line_does_not_say_how_the_value_was_obtained(config, tmp_path):
    """The dashboard shouldn't read as "we do this by hand" — whether a value
    was typed in or scraped is an implementation detail, not something a
    friend looking at the page needs to know. Only freshness is shown."""
    db = tmp_path / "t.db"
    with Store(db) as store:
        for i in range(20):
            store.upsert_rsi_point(
                RsiPoint("IBM", f"2026-07-{i + 1:02d}", 200.0, 45.0, "backfill:yahoo")
            )
        store.upsert_valuation(valuation(days_ago(2), source="scraped"))
        out = build_dashboard(store, config, tmp_path / "out.html")
    text = out.read_text()
    assert "by hand" not in text
    assert "scraped from Morningstar" not in text
    assert "Checked 2 days ago" in text


# ------------------------------------- crosses at the chart-window left edge


from screener.dashboard import _collect, _visible_crosses
from screener.signals import find_upward_crosses


def pts(*rsis):
    return [
        RsiPoint("X", f"2026-01-{i + 1:02d}", 100.0, v, "live:tradingview")
        for i, v in enumerate(rsis)
    ]


def test_a_cross_on_the_windows_first_bar_is_still_counted():
    """Regression: PG showed "1 upward cross of 30" directly above a completed
    up/down/up pattern, which needs two. A cross is defined by comparing a bar
    to its predecessor, and slicing the window threw that predecessor away --
    so a cross landing exactly on the left edge vanished from the count."""
    full = pts(25, 26, 28, 31, 33, 29, 35)   # crosses at index 3 and 6
    # window=4 puts the first cross exactly on the visible slice's first bar
    assert _visible_crosses(full, 4, 30.0) == [0, 3]


def test_visible_crosses_matches_plain_detection_when_history_fits():
    full = pts(25, 26, 28, 31, 33, 29, 35)
    assert _visible_crosses(full, 99, 30.0) == find_upward_crosses(full, 30.0)


def test_visible_crosses_excludes_crosses_before_the_window():
    """A cross two bars before the window starts must not leak in."""
    full = pts(25, 31, 26, 25, 24, 23, 22)   # single cross at index 1
    assert _visible_crosses(full, 3, 30.0) == []


def test_visible_crosses_handles_empty_and_single_point_history():
    assert _visible_crosses([], 90, 30.0) == []
    assert _visible_crosses(pts(25), 90, 30.0) == []


def test_card_cross_count_agrees_with_its_pattern(config, tmp_path):
    """End-to-end: a card showing a fired pattern must not claim fewer than
    two crosses, since two is what forms the pattern in the first place."""
    db = tmp_path / "t.db"
    rsis = [40.0] * 30 + [25.0, 31.0, 27.0, 33.0]   # up/down/up at the tail
    with Store(db) as store:
        for i, v in enumerate(rsis):
            import datetime as dt
            d = (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat()
            store.upsert_rsi_point(RsiPoint("IBM", d, 200.0, v, "live:tradingview"))
        rows = {r.symbol: r for r in _collect(store, config)}
    ibm = rows["IBM"]
    assert len(ibm.crosses) >= 2
