"""Tests for the dashboard's state model and rendering.

The state a card shows is the whole point of the page — a pattern that nobody
has fair-value checked must never read as a confirmed buy signal.
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


def signal(symbol="NVDA", up2="2026-07-23", known=False, fired=False):
    return Signal(
        symbol=symbol, up1_date="2026-07-16", down_date="2026-07-22", up2_date=up2,
        price=200.0 if known else None, fair_value=220.0 if known else None,
        valuation_known=known, valuation_pass=fired, fired=fired, recorded_at="now",
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
    )
    base.update(kw)
    return Row(**base)


# ------------------------------------------------------------- states


def test_unchecked_pattern_is_pending_not_a_signal():
    """The core guarantee: no fair value check means no buy signal."""
    r = row(signals=[signal(known=False, fired=False)])
    assert r.state == "pending"
    assert r.fired is False


def test_checked_and_passing_pattern_is_a_signal():
    r = row(signals=[signal(known=True, fired=True)])
    assert r.state == "signal"


def test_checked_but_failing_pattern_is_rejected():
    r = row(signals=[signal(known=True, fired=False)])
    assert r.state == "rejected"


def test_pattern_state_outranks_current_rsi_level():
    """A completed pattern still needs a human even if RSI has since recovered."""
    r = row(
        series=[RsiPoint("NVDA", "2026-07-27", 100.0, 65.0, "live:tradingview")],
        signals=[signal(known=False)],
    )
    assert r.state == "pending"


@pytest.mark.parametrize(
    "rsi,expected", [(25.0, "oversold"), (35.0, "watch"), (55.0, "neutral")]
)
def test_states_without_a_pattern_follow_rsi(rsi, expected):
    r = row(series=[RsiPoint("NVDA", "2026-07-27", 100.0, rsi, "live:tradingview")])
    assert r.state == expected


def test_empty_series_is_nodata():
    assert row(series=[]).state == "nodata"


def test_a_fired_signal_wins_over_a_later_unchecked_pattern():
    r = row(signals=[signal(up2="2026-01-05", known=True, fired=True), signal(up2="2026-07-23")])
    assert r.state == "signal"


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


def test_pending_card_says_it_is_unverified(config):
    html = render([row(signals=[signal()])], config)
    assert "Verify fair value" in html
    assert "fair value not checked yet" in html


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
