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


def test_the_growth_rate_is_the_lowest_on_offer():
    """Every error here is multiplied ten times over, so take the worst view."""
    assert growth_rate(30.0, 18.0, 12.0) == 12.0


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


def _quality(price, sticker_target=None, **kw):
    """A company good enough to pass the Big Four, priced as asked."""
    return evaluate(price=price, eps_ttm=5.0, eps_growth_ttm=15.0,
                    eps_growth_fy=14.0, sales_growth_ttm=13.0,
                    fcf_growth_ttm=20.0, roic=25.0, **kw)


def test_a_quality_company_below_the_mos_price_is_green():
    at_mos = _quality(price=100.0)
    reading = _quality(price=at_mos.mos * 0.9)
    assert reading.band == GREEN
    assert reading.score >= 8


def test_the_same_company_above_sticker_is_red():
    at_mos = _quality(price=100.0)
    reading = _quality(price=at_mos.sticker * 2)
    assert reading.band == RED


def test_between_the_two_prices_is_amber():
    base = _quality(price=100.0)
    reading = _quality(price=(base.sticker + base.mos) / 2)
    assert reading.band == AMBER


def test_a_cheap_price_alone_is_not_a_green_light():
    """Phil's condition is a wonderful business at an attractive price. Cheap
    on its own is how value traps are bought."""
    poor = evaluate(price=1.0, eps_ttm=5.0, eps_growth_ttm=1.0,
                    eps_growth_fy=1.0, sales_growth_ttm=1.0,
                    fcf_growth_ttm=-10.0, roic=2.0)
    assert poor.price <= poor.mos
    assert poor.big_four < 3
    assert poor.band == AMBER, "cheap, but not a business Rule #1 would own"


def test_the_score_stays_inside_one_to_ten():
    for price in (0.01, 1.0, 50.0, 1e6):
        reading = _quality(price=price)
        assert 1 <= reading.score <= 10


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
    assert reading.growth == 19.3
    assert reading.big_four == 4


def test_a_scanner_row_missing_everything_is_refused():
    assert not from_scanner({}).applicable
