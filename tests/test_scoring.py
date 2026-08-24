"""Tests for the weighted conviction score.

The arithmetic is a weighted mean; everything worth pinning is in what happens
to a factor nobody could read, and in the saturation points, which were
calibrated against the live watchlist rather than picked. Each of those carries
the measurement that set it.

Offline throughout.
"""

from __future__ import annotations

import pytest

from screener.scoring import (
    AMBER,
    DIP_SATURATES_AT,
    GREEN,
    GROWTH_SATURATES_AT,
    MAX_SCORE,
    MIN_SCORE,
    RED,
    SHADOW,
    Factor,
    band_for,
    composite,
    earnings_factor,
    growth_factor,
    pattern_factor,
    rule_one_factor,
    valuation_factor,
)

EVEN = {"a": 1.0, "b": 1.0}


def _f(key, strength):
    return Factor(key, key.upper(), strength)


# ------------------------------------------------------------ the weighing


def test_everything_at_full_strength_scores_ten():
    assert composite([_f("a", 1.0), _f("b", 1.0)], EVEN).score == MAX_SCORE


def test_everything_at_nothing_scores_one():
    """One rather than zero: the scale is a coefficient of quality, and nobody
    reads a 0/10."""
    assert composite([_f("a", 0.0), _f("b", 0.0)], EVEN).score == MIN_SCORE


def test_a_heavier_factor_moves_the_score_further():
    light = composite([_f("a", 1.0), _f("b", 0.0)], {"a": 1.0, "b": 3.0})
    heavy = composite([_f("a", 1.0), _f("b", 0.0)], {"a": 3.0, "b": 1.0})
    assert heavy.score > light.score


def test_reweighting_changes_the_score_without_touching_the_code():
    factors = [_f("a", 1.0), _f("b", 0.0)]
    assert composite(factors, {"a": 9.0, "b": 1.0}).score > \
           composite(factors, {"a": 1.0, "b": 9.0}).score


def test_a_factor_with_no_weight_is_ignored():
    """Adding a factor in code without adding it to the config must leave every
    number on the site where it was."""
    base = composite([_f("a", 1.0)], {"a": 1.0})
    plus = composite([_f("a", 1.0), _f("new", 0.0)], {"a": 1.0})
    assert plus.score == base.score


def test_a_negative_weight_cannot_subtract():
    assert composite([_f("a", 1.0), _f("b", 1.0)], {"a": 1.0, "b": -5.0}).score == MAX_SCORE


# --------------------------------------------------- what unknown does


def test_an_unknown_factor_is_dropped_rather_than_scored_zero():
    """Scoring it zero would make "nobody recorded a fair value" and "this
    stock is dear" produce the same number, and 88 of the 253 names have no
    fair value on file."""
    dropped = composite([_f("a", 1.0), _f("b", None)], EVEN)
    zeroed = composite([_f("a", 1.0), _f("b", 0.0)], EVEN)
    assert dropped.score == MAX_SCORE
    assert zeroed.score < dropped.score


def test_dropping_a_factor_reweights_the_rest():
    both = composite([_f("a", 1.0), _f("b", 1.0)], {"a": 1.0, "b": 3.0})
    one = composite([_f("a", 1.0), _f("b", None)], {"a": 1.0, "b": 3.0})
    assert one.score == both.score, "the survivor carries the whole score"
    assert one.contributions[0].share == pytest.approx(1.0)


def test_coverage_reports_how_much_was_actually_known():
    """The cost of dropping unknowns: a name with one factor out of five gets a
    confident-looking score off a fifth of the evidence. Coverage is what lets
    a reader tell a 9 from a 9."""
    comp = composite([_f("a", 1.0), _f("b", None)], {"a": 1.0, "b": 3.0})
    assert comp.coverage == pytest.approx(0.25)
    assert comp.thin


def test_full_coverage_is_not_thin():
    assert not composite([_f("a", 0.5), _f("b", 0.5)], EVEN).thin


def test_nothing_readable_scores_the_floor_with_no_coverage():
    """Inventing a middling 5 would look like a measurement."""
    comp = composite([_f("a", None), _f("b", None)], EVEN)
    assert comp.score == MIN_SCORE and comp.band == RED
    assert comp.coverage == 0.0


# ------------------------------------------------------- the breakdown


def test_the_contributions_add_up_to_the_score():
    comp = composite([_f("a", 1.0), _f("b", 0.5)], {"a": 3.0, "b": 1.0})
    total = MIN_SCORE + sum(c.points for c in comp.contributions)
    assert total == pytest.approx(comp.score, abs=0.5)


def test_a_factor_that_scored_nothing_contributes_nothing():
    comp = composite([_f("a", 1.0), _f("b", 0.0)], EVEN)
    assert next(c for c in comp.contributions if c.key == "b").points == 0.0


def test_an_unknown_factor_keeps_its_place_in_the_breakdown():
    """So the card can say which factor is missing, rather than silently
    listing one fewer."""
    comp = composite([_f("a", 1.0), _f("b", None)], EVEN)
    assert [c.key for c in comp.contributions] == ["a", "b"]
    assert [c.key for c in comp.known_factors] == ["a"]


# ------------------------------------------------------------ the bands


def test_the_bands_run_the_right_way_round():
    assert band_for(9.0) == GREEN
    assert band_for(5.0) == AMBER
    assert band_for(2.0) == RED


# ------------------------------------------------------ reading the factors


def test_the_dip_is_measured_at_the_trough_not_today():
    """A double cross completes above the threshold by construction, so
    scoring today's RSI gave every signal on the page a zero on the one factor
    that describes the signal itself."""
    assert pattern_factor(24.0, 30.0, at_dip=True).strength > 0
    assert pattern_factor(45.0, 30.0).strength == 0.0


def test_the_dip_saturates_where_the_data_says():
    """Across the 273 recorded daily buy patterns the dip below 30 runs a
    median of 2.3 points and a 90th percentile of 7.2. Saturating at ten put
    97% under full marks and the factor ranked almost nothing."""
    assert DIP_SATURATES_AT == 6.0
    assert pattern_factor(30.0 - DIP_SATURATES_AT, 30.0, at_dip=True).strength == 1.0
    assert pattern_factor(30.0 - 2.3, 30.0, at_dip=True).strength == pytest.approx(0.383, abs=0.01)


def test_meeting_the_margin_is_half_marks_and_doubling_it_is_full():
    """The factor rescales to whatever each horizon already demands, instead of
    carrying a second constant that has to be kept in step with the first."""
    assert valuation_factor(100.0, 130.0, margin=0.30).strength == pytest.approx(0.5)
    assert valuation_factor(100.0, 160.0, margin=0.30).strength == pytest.approx(1.0)
    # And the hourly chart, which wants 10%, puts the same two marks at 10/20.
    assert valuation_factor(100.0, 110.0, margin=0.10).strength == pytest.approx(0.5)
    assert valuation_factor(100.0, 120.0, margin=0.10).strength == pytest.approx(1.0)


def test_a_stock_at_fair_value_scores_nothing_on_valuation():
    assert valuation_factor(100.0, 100.0, margin=0.30).strength == 0.0
    assert valuation_factor(100.0, 80.0, margin=0.30).strength == 0.0


def test_an_unchecked_fair_value_is_unknown_not_zero():
    assert valuation_factor(100.0, None).strength is None
    assert valuation_factor(None, 130.0).strength is None


def test_growth_saturates_above_merely_growing():
    """Ten percent -- the Rule #1 Big Four threshold -- was cleared outright by
    60% of the watchlist and stopped separating it. The median here is +18%."""
    assert GROWTH_SATURATES_AT == 30.0
    assert growth_factor(30.0).strength == 1.0
    assert growth_factor(18.0).strength == pytest.approx(0.6)
    assert growth_factor(-5.0).strength == 0.0


def test_no_growth_figure_is_unknown():
    assert growth_factor(None).strength is None


def test_rule_one_comes_in_as_its_own_ten():
    class R:
        applicable, score, implied_growth = True, 10, 12.0

    assert rule_one_factor(R()).strength == 1.0

    class Low(R):
        score = 1

    assert rule_one_factor(Low()).strength == 0.0


def test_an_inapplicable_rule_one_is_unknown_and_says_why():
    class R:
        applicable, reason = False, "no positive earnings to project"

    factor = rule_one_factor(R())
    assert factor.strength is None
    assert "earnings" in factor.detail


def test_no_rule_one_reading_at_all_is_unknown():
    assert rule_one_factor(None).strength is None


def test_earnings_timing_is_binary():
    """The risk being priced is a gap on the open, and a gap does not get
    smaller because the release is three days out instead of one. What is
    tunable is how much that matters, and that lives in the weight."""
    class Clear:
        suspended, sessions = False, None

    class Near:
        suspended, sessions = True, 2

    assert earnings_factor(Clear()).strength == 1.0
    assert earnings_factor(Near()).strength == 0.0
    assert "2 sessions" in earnings_factor(Near()).detail


def test_no_calendar_entry_is_unknown():
    assert earnings_factor(None).strength is None


# ---------------------------------------------------------- shadow mode


def test_the_score_decides_nothing_yet():
    """Swapping a measured rule for an unmeasured one is the mistake this whole
    roadmap exists to avoid."""
    assert SHADOW is True


def test_the_score_does_not_touch_the_rocket(tmp_path):
    from screener.signals import is_strong

    # The composite has no way into the verdict: `is_strong` takes grading
    # factors and nothing else.
    assert is_strong((True, True), (True, True)) is True
    assert is_strong((False, False), (True, True)) is False


# ------------------------------------------------------------- on the card


def _block(**kw):
    from screener.dashboard import _conviction_block
    factors = [
        Factor("valuation", "Fair value", kw.get("valuation", 1.0), "d"),
        Factor("ruleone", "Buffett score", kw.get("ruleone", 1.0), "d"),
        Factor("growth", "EPS growth", kw.get("growth", 1.0), "d"),
        Factor("earnings", "Earnings timing", kw.get("earnings", 1.0), "d"),
        Factor("pattern", "RSI depth", kw.get("pattern", 1.0), "d"),
    ]
    weights = {"valuation": 3.0, "ruleone": 2.0, "growth": 1.5,
               "earnings": 1.0, "pattern": 1.0}
    return _conviction_block(composite(factors, weights))


def test_the_card_shows_the_score_out_of_ten():
    html = _block()
    assert 'class="cv-box cv-green"' in html
    assert '>10<span class="cv-of">/10</span>' in html
    assert "Conviction" in html
    # Score, label and bar on one line: the card had grown to five lines of
    # scoring metadata before this.
    assert html.count("<p") == 1 and html.count("</p>") == 1


def test_the_bar_has_one_segment_per_contributing_factor():
    """A single filled bar would show only the total, which is the least
    interesting part of a weighted score."""
    html = _block()
    for key in ("valuation", "ruleone", "growth", "earnings", "pattern"):
        assert f"seg-{key}" in html


def test_a_factor_that_scored_nothing_draws_no_segment():
    assert "seg-growth" not in _block(growth=0.0)


def test_the_segments_are_sized_by_what_each_factor_contributed():
    """The heaviest factor at full strength must draw the widest segment."""
    import re

    html = _block()
    widths = dict(re.findall(r'class="seg seg-(\w+)" style="flex:([\d.]+)"', html))
    assert float(widths["valuation"]) > float(widths["ruleone"]) > float(widths["growth"])
    assert float(widths["earnings"]) == float(widths["pattern"])


def test_a_missing_factor_is_declared_rather_than_just_absent():
    """An unread factor and one that scored zero both draw nothing, so the
    chip is what tells them apart."""
    html = _block(ruleone=None)
    # Named in the tooltip rather than badged on the card: one factor of five
    # missing is not a warning, it is Tuesday.
    tip = html.split('title="')[1].split('"')[0]
    assert "Not counted: Buffett score" in tip
    assert "cv-thin" not in html


def test_a_full_house_says_nothing_about_coverage():
    assert "known" not in _block()


def test_thin_evidence_is_marked_differently():
    html = _block(valuation=None, ruleone=None, growth=None)
    assert "cv-thin" in html


def test_no_score_renders_nothing():
    from screener.dashboard import _conviction_block

    assert _conviction_block(None) == ""


def test_the_block_carries_no_javascript():
    """CI asserts the whole page has none; this is the piece most tempted."""
    assert "<script" not in _block() and "onclick" not in _block()


def test_the_page_counts_its_high_conviction_names(tmp_path):
    """The score belongs at site level too, not only on a card at a time."""
    from screener.config import load_config
    from screener.dashboard import _collect, render
    from screener.storage import RsiPoint, Store

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: AAA, tradingview: "NASDAQ:AAA", morningstar: xnas/aaa, markets: [nasdaq]}}
rsi: {{period: 14, threshold: 30, overbought: 70, interval: "1D"}}
signal: {{window_days: 14, window_unit: calendar,
          valuation_rule: price_below_fair_value, fire_without_valuation: true}}
storage:
  database: "{tmp_path / 't.db'}"
  csv_dir: "{tmp_path}"
  fair_values: "{tmp_path / 'fv.yaml'}"
  notifications: "{tmp_path / 'n.json'}"
  recommendations: "{tmp_path / 'r.csv'}"
dashboard: {{output: "{tmp_path / 't.html'}", chart_days: 90}}
""")
    config = load_config(path)
    with Store(config.storage.database) as store:
        store.upsert_rsi_point(
            RsiPoint("AAA", "2026-08-19", 10.0, 25.0, "live:tradingview", horizon="1d")
        )
        rows = _collect(store, config, config.horizon("1d"))
    page = render(rows, config, config.horizon("1d"))
    assert "Conviction ≥7" in page
    assert "<script" not in page
