"""Tests for the Rule #1 valuation.

The arithmetic is four lines; everything hard is in the assumptions. Each
substitution the module makes for an input Phil Town has and we do not is
pinned here with the live case that forced it, because they are the parts a
future change would quietly break.

Offline throughout.
"""

from __future__ import annotations

import pytest

from screener.ruleone import (
    AMBER,
    base_growth,
    EPS_SPIKE,
    FUTURE_PE_CEILING,
    FUTURE_PE_FLOOR,
    GREEN,
    GROWTH_CAP,
    NOT_APPLICABLE,
    RED,
    REQUIRED_RETURN,
    YEARS,
    big_four,
    evaluate,
    from_scanner,
    future_pe,
    growth_rate,
    implied_growth,
    mos_price,
    sticker_price,
)


# --------------------------------------------------------- the arithmetic


def test_the_sticker_is_the_discounted_future_price():
    """EPS 10, growing 10% for ten years, on a P/E of 20, discounted at 15%."""
    future_eps = 10 * 1.10 ** 10          # 25.94
    expected = future_eps * 20 / 1.15 ** 10
    assert sticker_price(10.0, 10.0, 20.0) == pytest.approx(expected)
    assert sticker_price(10.0, 10.0, 20.0) == pytest.approx(128.2, abs=0.5)


def test_the_margin_of_safety_is_half():
    assert mos_price(100.0) == 50.0


def test_the_discount_factor_is_phils_fifteen_percent_over_ten_years():
    assert (1 + REQUIRED_RETURN) ** YEARS == pytest.approx(4.0456, abs=1e-4)


def test_growth_equal_to_the_required_return_collapses_the_model():
    """Why the cap is 20 and not 15. At g = 15% the (1+g)^10 in the numerator
    and the 1.15^10 in the denominator cancel, so the sticker price is just
    EPS x P/E — which for a stock priced on that P/E is its own market price.
    Capping there put all 226 evaluable names at exactly 0.0% from sticker."""
    assert sticker_price(8.0, 15.0, 20.0) == pytest.approx(8.0 * 20)
    assert GROWTH_CAP > REQUIRED_RETURN * 100


# ------------------------------------------------------- the growth rate


def test_the_conservative_case_is_the_lowest_on_offer():
    """Every error here is multiplied ten times over, so take the worst view."""
    assert growth_rate(30.0, 18.0, 12.0) == 12.0


def test_the_base_case_is_the_median():
    """A range says more than a point does, so both cases are kept."""
    assert base_growth(30.0, 18.0, 16.0) == 18.0


def test_the_base_case_cannot_outgrow_sales_for_a_decade():
    """AT&T's median reads 20% because two of its three rates are the same one
    good earnings year, against sales of 2.6%. Margin expansion is finite, so
    the base case is held to sales plus five points — 9/10 and green becomes
    6/10 and amber."""
    assert base_growth(20.0, 20.0, 2.6) == pytest.approx(7.6)


def test_the_base_case_never_falls_below_the_conservative_one():
    assert base_growth(18.0, 2.0, 1.0) >= growth_rate(18.0, 2.0, 1.0)


# ---------------------------------------------------- what the price demands


def test_the_implied_growth_prices_back_to_the_sticker():
    """Rule #1 backwards: the rate for which sticker(g) == price."""
    g = implied_growth(eps=5.0, price=100.0)
    assert sticker_price(5.0, g, future_pe(g)) == pytest.approx(100.0, rel=1e-3)


def test_a_dear_price_demands_more_growth_than_a_cheap_one():
    assert implied_growth(5.0, 500.0) > implied_growth(5.0, 50.0)


def test_a_price_below_any_assumption_returns_the_floor():
    """Cheap however pessimistic you are."""
    assert implied_growth(eps=5.0, price=0.5) == -10.0


def test_a_price_beyond_modelling_returns_the_ceiling():
    """Past this the number has stopped meaning anything."""
    assert implied_growth(eps=0.01, price=10_000.0) == 60.0


def test_sales_growth_can_veto_an_earnings_spike():
    """The substitution that matters most. Without sales in the minimum, AT&T
    scored a fictitious 20% a year off one good earnings year and came out 361%
    below sticker; with it the rate reads 2.6% and the stock sits above sticker
    instead."""
    assert growth_rate(20.0, 20.0, 2.6) == 2.6


def test_growth_is_capped():
    assert growth_rate(90.0, 80.0, 70.0) == GROWTH_CAP


def test_growth_is_floored_at_zero():
    """A shrinking company is valued as a flat one, not a negative one."""
    assert growth_rate(-30.0, -12.0, -5.0) == 0.0


def test_no_growth_data_is_none_not_zero():
    """Zero is a company that stopped growing; None is one we cannot see."""
    assert growth_rate(None, None, None) is None
    assert growth_rate(None, 8.0, None) == 8.0


# -------------------------------------------------------- the future P/E


def test_the_future_pe_is_twice_the_growth_rate():
    assert future_pe(10.0) == 20.0


def test_a_no_growth_company_is_not_worth_nothing():
    """`2 x 0%` is a P/E of zero, which makes the sticker zero and every
    low-growth company infinitely overvalued."""
    assert future_pe(0.0) == FUTURE_PE_FLOOR


def test_the_future_pe_has_a_ceiling():
    assert future_pe(GROWTH_CAP) == FUTURE_PE_CEILING


# --------------------------------------------------------- the Big Four


def test_the_big_four_counts_what_clears_ten_percent():
    assert big_four(roic=15.0, eps_growth=12.0, sales_growth=11.0, fcf_growth=30.0) == 4
    assert big_four(roic=15.0, eps_growth=2.0, sales_growth=1.0, fcf_growth=-4.0) == 1


def test_an_unknown_metric_counts_as_a_failure():
    """This is the quality half of a method whose premise is that most
    companies do not qualify. An absent test is not a pass."""
    assert big_four(roic=None, eps_growth=None, sales_growth=None, fcf_growth=None) == 0


def test_it_is_four_and_not_five():
    """The fifth is equity growth, and no free feed serves the history for it.
    Counting an absent test as a pass would flatter every company equally."""
    assert big_four(50.0, 50.0, 50.0, 50.0) == 4


# ------------------------------------------------ refusing to give a number


def test_a_loss_making_company_gets_no_sticker_price():
    """Intel is on the watchlist at -2.12 a share. Compounding a negative for
    ten years produces a confident number about nothing."""
    reading = evaluate(price=92.84, eps_ttm=-2.12, eps_growth_ttm=55.5, sales_growth_ttm=7.5)
    assert not reading.applicable
    assert reading.band == NOT_APPLICABLE
    assert reading.reason == "no positive earnings to project"


def test_a_company_with_no_earnings_figure_is_refused():
    assert evaluate(price=10.0, eps_ttm=None).reason == "no earnings figure"


def test_a_company_with_no_growth_history_is_refused():
    reading = evaluate(price=10.0, eps_ttm=1.0)
    assert not reading.applicable
    assert reading.reason == "no growth history"


def test_no_price_is_refused():
    assert not evaluate(price=None, eps_ttm=1.0, eps_growth_ttm=10.0).applicable


# --------------------------------------------------- the one-off earnings


def test_an_earnings_spike_is_flagged():
    """LYFT's trailing EPS growth reads 3,166%, which puts it on a P/E of 2.4
    and a sticker six times its price. Rule #1 wants a normalised figure and
    the feed has none."""
    reading = evaluate(price=17.43, eps_ttm=7.15, eps_growth_ttm=3166.4,
                       eps_growth_fy=40.0, sales_growth_ttm=10.8,
                       fcf_growth_ttm=25.0, roic=20.0)
    assert reading.caution
    assert "run rate" in reading.caution


def test_a_flagged_reading_cannot_be_green():
    """A green light says 'act'. Nothing built on an unexamined one-off earns
    that, however cheap the arithmetic makes it look."""
    reading = evaluate(price=17.43, eps_ttm=7.15, eps_growth_ttm=3166.4,
                       eps_growth_fy=40.0, sales_growth_ttm=10.8,
                       fcf_growth_ttm=25.0, roic=20.0)
    assert reading.price <= reading.mos, "cheap enough that it would be green"
    assert reading.big_four >= 3
    assert reading.band == AMBER


def test_ordinary_growth_is_not_flagged():
    reading = evaluate(price=50.0, eps_ttm=4.0, eps_growth_ttm=14.0,
                       eps_growth_fy=12.0, sales_growth_ttm=11.0)
    assert reading.caution == ""
    assert EPS_SPIKE > 14.0


# ---------------------------------------------------------- the verdict


def _quality(price, **kw):
    """A company good enough to pass the Big Four, priced as asked."""
    return evaluate(price=price, eps_ttm=5.0, eps_growth_ttm=15.0,
                    eps_growth_fy=14.0, sales_growth_ttm=13.0,
                    fcf_growth_ttm=20.0, roic=25.0, **kw)


def test_a_price_demanding_less_than_the_company_delivers_scores_well():
    """The verdict, in one comparison. Cheap enough that the growth already on
    record clears what the price is asking for."""
    reading = _quality(price=20.0)
    assert reading.implied_growth < reading.growth
    assert reading.headroom > 0
    assert reading.score >= 8
    assert reading.band == GREEN


def test_a_price_demanding_far_more_than_it_delivers_scores_badly():
    reading = _quality(price=2000.0)
    assert reading.implied_growth > reading.growth
    assert reading.headroom < 0
    assert reading.band == RED


def test_the_score_moves_continuously_with_the_price():
    """Why this replaced the sticker comparison: that marked 204 of 226
    companies red and could not rank the 204. This has to be a ranking."""
    scores = [_quality(price=p).score for p in (10, 40, 80, 150, 400, 1200)]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) >= 4, "a rank, not two buckets"


def test_quality_shifts_the_score_without_setting_it():
    """A business clearing all of the Big Four is not read the same as one
    clearing none at the same price — but the price still dominates."""
    priced = dict(price=120.0, eps_ttm=5.0, eps_growth_ttm=15.0,
                  eps_growth_fy=14.0, sales_growth_ttm=13.0)
    good = evaluate(**priced, fcf_growth_ttm=20.0, roic=25.0)
    poor = evaluate(**priced, fcf_growth_ttm=-5.0, roic=2.0)
    assert good.score > poor.score
    assert good.score - poor.score <= 4, "quality moves it, it does not decide it"


def test_a_cheap_price_alone_is_not_a_top_score():
    """Phil's condition is a wonderful business at an attractive price. Cheap
    on its own is how value traps are bought."""
    poor = evaluate(price=1.0, eps_ttm=5.0, eps_growth_ttm=1.0,
                    eps_growth_fy=1.0, sales_growth_ttm=1.0,
                    fcf_growth_ttm=-10.0, roic=2.0)
    rich = _quality(price=1.0)
    assert poor.score < rich.score


def test_the_headroom_is_the_base_case_minus_what_the_price_demands():
    reading = _quality(price=100.0)
    assert reading.headroom == pytest.approx(reading.growth - reading.implied_growth)


def test_the_gap_to_sticker_is_relative_to_the_price():
    reading = evaluate(price=100.0, eps_ttm=5.0, eps_growth_ttm=15.0,
                       eps_growth_fy=15.0, sales_growth_ttm=15.0)
    assert reading.to_sticker == pytest.approx((reading.sticker - 100.0) / 100.0)


# ------------------------------------------------------ reading the feed


def test_a_scanner_row_evaluates_directly():
    row = {
        "close": 46.51,
        "earnings_per_share_diluted_ttm": 3.9,
        "earnings_per_share_diluted_yoy_growth_ttm": 25.0,
        "earnings_per_share_diluted_yoy_growth_fy": 22.0,
        "total_revenue_yoy_growth_ttm": 19.3,
        "free_cash_flow_yoy_growth_ttm": 15.0,
        "return_on_invested_capital": 18.2,
    }
    reading = from_scanner(row)
    assert reading.applicable
    assert reading.conservative_growth == 19.3, "the pessimistic case is the lowest rate"
    assert reading.growth == 20.0, "the base case is the median, capped"
    assert reading.big_four == 4


def test_a_scanner_row_missing_everything_is_refused():
    assert not from_scanner({}).applicable


# ------------------------------------------- how the page uses the reading
#
# Rule #1 ranks and annotates; it never gates. Nothing here may add or remove
# a signal, because the factor is still unmeasured — and the only honest way
# to measure it is forward, from the journal, since its inputs are current
# fundamentals with no history to backtest against.


@pytest.fixture()
def page_config(tmp_path):
    from screener.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: AAA, tradingview: "NASDAQ:AAA", morningstar: xnas/aaa, markets: [nasdaq]}}
  - {{symbol: BBB, tradingview: "NASDAQ:BBB", morningstar: xnas/bbb, markets: [nasdaq]}}
  - {{symbol: CCC, tradingview: "NASDAQ:CCC", morningstar: xnas/ccc, markets: [nasdaq]}}
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


def _seed_signal(store, symbol, fair_value=1000.0, confirms=True):
    """A fired buy on the daily chart, with the valuation gate set explicitly.

    `confirms` is passed rather than derived so a test can produce the case
    that matters here: a fired pattern whose Morningstar gate *failed*.
    """
    import datetime as dt

    from screener.storage import RsiPoint, Signal, Valuation

    now = dt.datetime.now()
    stamps = [(now - dt.timedelta(days=40 - i)).date().isoformat() for i in range(40)]
    for stamp in stamps:
        store.upsert_rsi_point(RsiPoint(symbol, stamp, 50.0, 33.0, "test", horizon="1d"))
    store.record_signal(Signal(
        symbol, stamps[-6], stamps[-4], stamps[-2], 50.0, fair_value,
        True, confirms, True, "now", horizon="1d", direction="buy",
    ))
    store.upsert_valuation(
        Valuation(symbol, "2026-08-10", 50.0, fair_value, "2026-08-10", "manual")
    )


def _reading(score, band="amber", applicable=True):
    from screener.ruleone import RuleOne

    return RuleOne(applicable=applicable, score=score, band=band, growth=12.0,
                   conservative_growth=8.0, implied_growth=10.0, price=50.0,
                   sticker=80.0, mos=40.0, big_four=3,
                   reason="" if applicable else "no positive earnings to project")


def test_rule_one_ranks_within_a_category_without_changing_it(page_config):
    """The rocket category holds Rule #1 scores from 2 to 10 and treats them
    identically. This sorts inside it; membership is untouched."""
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        for symbol in ("AAA", "BBB", "CCC"):
            _seed_signal(store, symbol)
        store.upsert_rule_one("AAA", _reading(2, "red"))
        store.upsert_rule_one("BBB", _reading(9, "green"))
        store.upsert_rule_one("CCC", _reading(6))
        rows = _collect(store, page_config, page_config.horizon("1d"))

    assert [r.symbol for r in rows] == ["BBB", "CCC", "AAA"]
    assert all(r.state == "strong" for r in rows), "every one is still a rocket"


def test_an_unreadable_company_ranks_mid_table_not_last(page_config):
    """Rule #1 having no opinion is not the same as a bad opinion."""
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        for symbol in ("AAA", "BBB", "CCC"):
            _seed_signal(store, symbol)
        store.upsert_rule_one("AAA", _reading(9, "green"))
        store.upsert_rule_one("BBB", _reading(1, "red"))
        store.upsert_rule_one("CCC", _reading(0, "n/a", applicable=False))
        rows = _collect(store, page_config, page_config.horizon("1d"))

    assert [r.symbol for r in rows] == ["AAA", "CCC", "BBB"]


def test_agreement_between_the_two_valuations_is_marked(page_config):
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(9, "green"))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]

    assert row.both_valuations_agree
    assert "both agree" in _card(row, page_config, page_config.horizon("1d"))


def test_a_rule_one_red_strong_buy_is_not_marked_but_stays_a_rocket(page_config):
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(2, "red"))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]

    assert not row.both_valuations_agree
    assert row.strong, "ranking must never gate"
    assert row.state == "strong"


def test_only_a_strong_buy_can_carry_the_agreement_mark(page_config):
    """A Rule #1 green with no Morningstar confirmation is one opinion, not
    two agreeing."""
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA", fair_value=1.0, confirms=False)
        store.upsert_rule_one("AAA", _reading(9, "green"))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]

    assert not row.both_valuations_agree
