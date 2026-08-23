"""Tests for the Historical Dashboard.

The page is an event study: every recommendation rebased to 100 on its signal
day, drawn forward, with a random-entry line to read it against. Cohort and
timeframe are radio buttons and CSS sibling selectors — the dashboards have
never carried JavaScript and CI asserts it, so the panels are pre-rendered.

Offline throughout.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screener.config import load_config
from screener.historical import (
    CHART_DAYS,
    COHORTS,
    MIN_FOR_MEAN,
    build_historical,
    collect_panels,
    render_historical,
)
from screener.outcomes import mean_path, trajectory
from screener.storage import RsiPoint, Signal, Store, Valuation


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: AAA, tradingview: "NASDAQ:AAA", morningstar: xnas/aaa, markets: [nasdaq]}}
  - {{symbol: BBB, tradingview: "NASDAQ:BBB", morningstar: xnas/bbb, markets: [nasdaq]}}
rsi: {{period: 14, threshold: 30, overbought: 70, interval: "1D"}}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage:
  database: "{tmp_path / 't.db'}"
  csv_dir: "{tmp_path}"
  fair_values: "{tmp_path / 'fv.yaml'}"
  notifications: "{tmp_path / 'n.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
""")
    return load_config(path)


def seed(store, symbol="AAA", start=100.0, drift=1.0, days=90,
         signal_on=10, direction="buy", fair_value=None):
    """A daily series with a steady drift, and one pattern part-way through."""
    base = dt.date(2026, 1, 1)
    dates = []
    for i in range(days):
        date = (base + dt.timedelta(days=i)).isoformat()
        dates.append(date)
        store.upsert_rsi_point(
            RsiPoint(symbol, date, start + drift * i, 35.0, "backfill:yahoo", horizon="1d")
        )
    store.record_signal(Signal(
        symbol, dates[signal_on - 2], dates[signal_on - 1], dates[signal_on],
        start + drift * signal_on, fair_value,
        fair_value is not None, fair_value is not None, True, "now",
        horizon="1d", direction=direction,
    ))
    if fair_value is not None:
        store.upsert_valuation(Valuation(
            symbol, "2026-08-01", start + drift * signal_on, fair_value, "2026-08-01", "manual"
        ))
    return dates


# ----------------------------------------------------------- trajectories


def test_a_path_starts_at_one_hundred():
    closes = [(f"2026-01-{i + 1:02d}", 100.0 + i) for i in range(10)]
    path = trajectory(100.0, closes, "2026-01-01", 5)
    assert path[0] == 100.0
    assert path[-1] == pytest.approx(105.0)


def test_a_path_rebases_so_any_two_stocks_share_an_axis():
    """A $5 stock and a $500 one have to be readable on one chart."""
    cheap = [(f"2026-01-{i + 1:02d}", 5.0 * (1 + 0.01 * i)) for i in range(6)]
    dear = [(f"2026-01-{i + 1:02d}", 500.0 * (1 + 0.01 * i)) for i in range(6)]
    assert trajectory(5.0, cheap, "2026-01-01", 5) == pytest.approx(
        trajectory(500.0, dear, "2026-01-01", 5)
    )


def test_a_path_stops_where_the_history_does():
    """A line that stops is a signal too recent to have finished, not one that
    went flat."""
    closes = [(f"2026-01-{i + 1:02d}", 100.0) for i in range(4)]
    assert len(trajectory(100.0, closes, "2026-01-01", 60)) == 4


def test_a_path_before_the_history_is_refused():
    """Same coverage rule as the outcome maths, and for the same reason."""
    closes = [(f"2026-06-{i + 1:02d}", 100.0) for i in range(10)]
    assert trajectory(100.0, closes, "2024-01-01", 20) == []


def test_the_mean_stops_when_the_sample_thins():
    """The failure mode of every event-study chart: the average keeps going
    long after it is one lucky ticker."""
    paths = [[100.0, 101.0, 102.0], [100.0, 103.0], [100.0, 105.0]]
    assert mean_path(paths, min_paths=3) == pytest.approx([100.0, 103.0])


def test_the_mean_is_the_average_at_each_day():
    paths = [[100.0, 110.0], [100.0, 90.0]]
    assert mean_path(paths, min_paths=2) == pytest.approx([100.0, 100.0])


# ------------------------------------------------------------- cohorts


def test_a_confirmed_buy_lands_in_the_strong_cohort(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)   # far below fair value
        panels = collect_panels(store, config)
    assert [e.symbol for e in panels[("strong", "1d")].entries] == ["AAA"]
    assert [e.symbol for e in panels[("buy", "1d")].entries] == ["AAA"]


def test_an_unconfirmed_buy_is_only_a_plain_buy(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=None)
        panels = collect_panels(store, config)
    assert panels[("strong", "1d")].entries == []
    assert [e.symbol for e in panels[("buy", "1d")].entries] == ["AAA"]


def test_the_gate_is_recomputed_at_the_price_it_fired_at(config):
    """`_rescore_signals` overwrites every stored price with today's, so the
    saved flag says whether the stock is cheap *now*. The price on the signal's
    own bar is still in rsi_history, and that is what this uses."""
    with Store(config.storage.database) as store:
        # Rises steeply: 200 at the signal, 990 by the end. A 400 fair value
        # clears the daily chart's 30% margin at the first and not the second.
        dates = seed(store, "AAA", start=100.0, drift=10.0, signal_on=10, fair_value=400.0)
        # Simulate the rescore: the stored price becomes the latest close.
        store.update_signal_valuation(
            "AAA", dates[10], 100.0 + 10.0 * 89, 400.0, True, False, True, "1d", "buy",
        )
        panels = collect_panels(store, config)
    assert [e.symbol for e in panels[("strong", "1d")].entries] == ["AAA"]


def test_the_horizon_margin_still_applies_to_the_recomputed_gate(config):
    """200 against a 250 fair value is a 25% discount, and the daily chart
    demands 30%. Recomputing the gate must not quietly drop the margin."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", start=100.0, drift=10.0, signal_on=10, fair_value=250.0)
        panels = collect_panels(store, config)
    assert panels[("strong", "1d")].entries == []
    assert [e.symbol for e in panels[("buy", "1d")].entries] == ["AAA"]


def test_a_sell_never_lands_in_a_buy_cohort(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", direction="sell", fair_value=1.0)
        panels = collect_panels(store, config)
    assert panels[("buy", "1d")].entries == []
    assert panels[("strong", "1d")].entries == []
    assert [e.symbol for e in panels[("sell", "1d")].entries] == ["AAA"]


def test_every_cohort_and_timeframe_has_a_panel(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA")
        panels = collect_panels(store, config)
    assert len(panels) == len(COHORTS) * len(config.horizons)


def test_a_tiny_cohort_draws_no_average(config):
    """Three tickers wearing the authority of a cohort mean is how the
    sell-strong weekly panel produced a +192% 'average'."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        panels = collect_panels(store, config)
    panel = panels[("strong", "1d")]
    assert len(panel.entries) < MIN_FOR_MEAN
    assert panel.mean == []


# ---------------------------------------------------------------- page


def test_the_page_is_named_the_historical_dashboard(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    assert "<h1>Historical Dashboard</h1>" in page
    assert "Has this worked?" not in page


def test_the_page_carries_no_javascript(config):
    """The dashboards never have, and CI asserts it. Cohort selection is
    radios and sibling selectors."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    assert "<script" not in page.lower()
    assert "onclick" not in page.lower()


def test_it_opens_on_strong_buy_and_the_daily_chart(config):
    """What the reader actually asked to see first."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    assert 'id="co-strong" checked' in page
    assert 'id="tf-1d" checked' in page


def test_buy_and_sell_sit_behind_a_disclosure(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    behind = page.split("<details")[1].split("</details>")[0]
    for cohort in COHORTS:
        if cohort.primary:
            assert f'for="co-{cohort.key}"' not in behind
        else:
            assert f'for="co-{cohort.key}"' in behind


def test_each_pairing_has_a_rule_that_shows_exactly_one_panel(config):
    """The CSS trick: two `:checked ~` chains reaching the same container is
    what expresses 'this cohort AND this timeframe'."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    for cohort in COHORTS:
        for horizon in config.horizons:
            rule = (f'#co-{cohort.key}:checked ~ #tf-{horizon.key}:checked ~ '
                    f'.panels .p-{cohort.key}-{horizon.key}')
            assert rule in page
            assert f'class="panel p-{cohort.key}-{horizon.key}"' in page


def test_the_valuation_cohorts_carry_their_caveat_where_it_is_read(config):
    """Not in a footnote: the cohort is selected with hindsight and the reader
    needs to know while looking at it."""
    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=1000.0)
        page = render_historical(store, config)
    strong = page.split('class="panel p-strong-1d"')[1].split("</section>")[0]
    assert "selected with hindsight" in strong
    plain = page.split('class="panel p-buy-1d"')[1].split("</section>")[0]
    assert "selected with hindsight" not in plain


def test_sell_panels_say_which_way_is_winning(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", direction="sell")
        page = render_historical(store, config)
    sell = page.split('class="panel p-sell-1d"')[1].split("</section>")[0]
    assert "falling line is the winning one" in sell


def test_an_empty_database_still_renders(config):
    """The CI smoke check builds every page from nothing."""
    with Store(config.storage.database) as store:
        page = render_historical(store, config)
    assert "Historical Dashboard" in page
    assert "No signals in this cohort yet" in page


def test_the_page_is_written_where_the_nav_points(config, tmp_path):
    with Store(config.storage.database) as store:
        seed(store, "AAA")
        out = build_historical(store, config, tmp_path / "site" / "history.html")
    assert out.name == "history.html"
    assert out.stat().st_size > 0


def test_the_chart_window_matches_what_the_paths_carry(config):
    with Store(config.storage.database) as store:
        seed(store, "AAA", days=CHART_DAYS + 40, signal_on=5)
        panels = collect_panels(store, config)
    [entry] = panels[("buy", "1d")].entries
    assert len(entry.path) == CHART_DAYS + 1, "the signal day plus the window"


# ------------------------------------------- naming the lines, and recency


def _panel_with(entries):
    """A panel holding hand-made entries, for the parts that are pure layout."""
    from screener.historical import Entry, Panel

    p = Panel("strong", "1d")
    p.entries = [
        Entry(symbol=s, up2_date=d, path=path, ret=ret,
              right=(ret > 0) if ret is not None else None)
        for s, d, path, ret in entries
    ]
    return p


def test_a_recommendation_still_inside_its_window_is_listed():
    """The bug this fixes. A +20d return cannot exist until twenty trading days
    have passed, so filtering on it meant the newest row on the page was always
    a month old -- and on the 1h panel, where every drawn line is days old,
    nothing recent was listed at all. The page looked stale while working."""
    from screener.historical import _table

    html = _table(_panel_with([
        ("AAA", "2026-08-19", [100.0, 101.0], None),
        ("BBB", "2026-07-01", [100.0, 105.0, 110.0], 0.10),
    ]))
    assert "AAA" in html, "yesterday's recommendation belongs on the page"
    assert "day 1" in html and "/20" in html, "and says how far through it is"
    assert "BBB" in html and "+10.0%" in html, "settled ones are unchanged"


def test_an_open_entry_shows_the_return_it_has_so_far():
    from screener.historical import _table

    html = _table(_panel_with([("AAA", "2026-08-19", [100.0, 103.5], None)]))
    assert "+3.5%" in html


def test_every_named_line_is_numbered_and_in_the_table():
    """The other half of "which recommendation is this line?" -- the chart
    numbers and the table rows are one key, generated from one list."""
    from screener.historical import _plot, _table

    panel = _panel_with([
        (s, f"2026-0{i + 1}-01", [100.0, 100.0 + i], None)
        for i, s in enumerate("ABCDE")
    ])
    svg, table = _plot(panel), _table(panel)
    assert svg.count('class="pathnum"') == 5
    for n in range(1, 6):
        assert f'>{n}<' in svg
        assert f'<td class="num idx">{n}</td>' in table


def test_the_named_lines_are_one_per_company():
    """Twelve numbered lines should be twelve companies. The same finding as
    the alert cooldown: an intraday pattern completes on almost every run, so
    the twelve newest 1h strong buys were eight companies from one afternoon,
    four of them listed twice."""
    from screener.historical import _named

    panel = _panel_with([
        ("AAA", "2026-08-19T21:00", [100.0, 101.0], None),
        ("AAA", "2026-08-19T20:00", [100.0, 101.0], None),
        ("AAA", "2026-08-19T19:00", [100.0, 101.0], None),
        ("BBB", "2026-08-19T18:00", [100.0, 102.0], None),
    ])
    named = _named(panel)
    assert [e.symbol for e in named] == ["AAA", "BBB"]
    assert named[0].up2_date.endswith("21:00"), "the newest of the repeats"


def test_the_deduplication_does_not_touch_the_statistics(config):
    """Only the key is deduplicated. Every figure on the page still runs over
    the whole sample, or the hit rate would quietly change meaning."""
    from screener.historical import _named, collect_panels

    with Store(config.storage.database) as store:
        seed(store, "AAA", fair_value=500.0)
        seed(store, "BBB", fair_value=500.0)
        panels = collect_panels(store, config)
    panel = panels[("strong", "1d")]
    assert panel.stats["n"] == len([e for e in panel.entries if e.ret is not None])
    assert len(_named(panel)) <= len(panel.entries)


def test_the_numbers_do_not_pile_up_on_each_other():
    """Paths that ran the full window all end at the same x, so their labels
    would stack into an unreadable column."""
    from screener.historical import _label_slots

    placed = _label_slots([100.0] * 6, gap=11.0, top=20.0, bottom=280.0)
    gaps = sorted(b - a for a, b in zip(sorted(placed), sorted(placed)[1:]))
    assert gaps[0] >= 10.99


def test_the_numbers_stay_inside_the_chart():
    from screener.historical import _label_slots

    placed = _label_slots([295.0] * 8, gap=11.0, top=20.0, bottom=280.0)
    assert max(placed) <= 280.0 and min(placed) >= 20.0


def test_a_label_keeps_its_neighbours_order():
    """The key reads top to bottom, so a nudge must never let one number cross
    another."""
    from screener.historical import _label_slots

    ys = [140.0, 142.0, 144.0, 210.0]
    placed = _label_slots(ys, gap=11.0, top=20.0, bottom=280.0)
    assert placed == sorted(placed), "same order in, same order out"


def test_an_unnumbered_path_is_drawn_but_quiet():
    """Beyond the named twelve the paths are there for the shape of the cohort,
    not to be read one by one."""
    from screener.historical import MAX_PATHS, NAMED_PATHS, _plot

    panel = _panel_with([
        (f"S{i:02d}", f"2026-01-{i + 1:02d}", [100.0, 100.0 + i], None)
        for i in range(MAX_PATHS + 4)
    ])
    svg = _plot(panel)
    assert svg.count('class="pathnum"') == NAMED_PATHS
    assert svg.count('class="trace ctx') == MAX_PATHS - NAMED_PATHS


def test_an_open_path_is_not_coloured_as_a_win_or_a_loss():
    """It has no verdict yet and must not borrow the look of one."""
    from screener.historical import _plot

    svg = _plot(_panel_with([("AAA", "2026-08-19", [100.0, 104.0], None)]))
    trace = svg.split('<polyline class="')[1].split('"')[0]
    assert "open" in trace
    assert "win" not in trace and "loss" not in trace
