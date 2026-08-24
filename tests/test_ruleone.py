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
                   sticker=80.0, sticker_low=60.0, mos=40.0, eps=4.0, big_four=3,
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
    card = _card(row, page_config, page_config.horizon("1d"))
    # Beside the score it qualifies, where it can say what it means. As a bare
    # "both agree" badge next to the status pill it named neither party.
    assert "agrees with fair value" in card
    assert card.index('class="ruleone"') < card.index("agrees with fair value")
    assert "both agree" not in card


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


def test_the_card_shows_a_chip_not_a_panel(page_config):
    """A second opinion that ranks the page and decides nothing should not be
    the loudest thing on the card."""
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(7))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    assert '<span class="r1-box r1-amber"' in card
    assert '>7<span class="r1-of">/10</span>' in card, "the scale has to be on the chip"
    assert "Buffett score" in card, "'Rule #1' alone means nothing to a reader"
    for gone in ("r1-head", "r1-demand", "r1-detail", "r1-score", "r1-gap"):
        assert gone not in card, f"{gone} belonged to the panel this replaced"


def test_the_chip_sits_between_fair_value_and_earnings_growth(page_config):
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(7))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    assert card.index('class="valuation') < card.index('class="ruleone"') < card.index('class="earnings')


def test_the_reasoning_moves_to_the_tooltip_rather_than_away(page_config):
    """Cut from the card, not lost: the rate the price demands, the range
    delivered, the sticker and the Big Four are all one hover away."""
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(7))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    tip = card.split('title="')[1].split('"')[0]
    assert "price demands 10.0%" in tip
    assert "delivered 8.0–12.0%" in tip
    assert "Sticker" in tip and "Big Four 3/4" in tip


def test_an_unreadable_company_shows_a_muted_dash(page_config):
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(0, "n/a", applicable=False))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    assert 'class="r1-box r1-na"' in card
    assert "no positive earnings to project" in card


# --------------------------------------------------- the value in money


def test_the_value_is_a_band_not_a_point():
    """Asked for as "a Buffett value in dollars", and deliberately given as a
    range: one sticker is one growth assumption compounded ten times, so it
    carries ten times that assumption's error."""
    from screener.ruleone import evaluate

    r = evaluate(price=100.0, eps_ttm=5.0, eps_growth_ttm=18.0,
                 eps_growth_fy=14.0, sales_growth_ttm=12.0)
    lo, hi = r.value_band
    assert lo < hi
    assert lo == r.sticker_low and hi == r.sticker


def test_the_conservative_end_uses_the_conservative_rate():
    from screener.ruleone import evaluate, future_pe, sticker_price

    r = evaluate(price=100.0, eps_ttm=5.0, eps_growth_ttm=18.0,
                 eps_growth_fy=14.0, sales_growth_ttm=12.0)
    assert r.sticker_low == pytest.approx(
        sticker_price(5.0, r.conservative_growth, future_pe(r.conservative_growth))
    )


def test_a_flat_earner_gets_a_score_but_no_price():
    """The finding that made this a band with a hole in it.

    Both growth rates floor at zero and `future_pe` floors at 8, so a company
    whose earnings are flat prices out at EPS x 8 / 1.15^10 -- EPS x 1.98, a
    P/E of 2, whatever the company is. 47 of the 226 readable names on the
    watchlist land on that constant exactly, META and TSLA among them. It is
    arithmetic, not a valuation.
    """
    from screener.ruleone import evaluate

    r = evaluate(price=550.0, eps_ttm=26.55, eps_growth_ttm=-4.0,
                 eps_growth_fy=-2.0, sales_growth_ttm=-1.0)
    assert r.applicable, "a profitable company is still readable"
    assert r.growth == 0.0
    constant = FUTURE_PE_FLOOR / (1 + REQUIRED_RETURN) ** YEARS   # 1.977
    assert r.sticker == pytest.approx(26.55 * constant), "EPS times a constant"
    assert constant == pytest.approx(1.977, abs=1e-3), "a P/E of 2"
    assert r.value_band is None, "and so it must not be quoted as a value"
    assert r.score > 0, "the score is built on implied growth and still stands"


def test_the_margin_of_safety_band_is_the_value_band_halved():
    from screener.ruleone import evaluate

    r = evaluate(price=100.0, eps_ttm=5.0, eps_growth_ttm=18.0,
                 eps_growth_fy=14.0, sales_growth_ttm=12.0)
    assert r.mos_band == (r.value_band[0] / 2, r.value_band[1] / 2)


def test_an_unreadable_company_has_no_band_either():
    from screener.ruleone import evaluate

    r = evaluate(price=20.0, eps_ttm=-2.12)   # Intel, the live case
    assert not r.applicable
    assert r.value_band is None and r.mos_band is None


def test_the_card_shows_the_value_beside_the_score(page_config):
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(7))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    # Two named figures on one line, each label ahead of the number it names.
    assert "Buffett score:" in card and "Buffett value:" in card
    assert "80.00" in card
    line = card.split('<p class="ruleone">')[1].split("</p>")[0]
    assert line.index("Buffett score:") < line.index('class="r1-box')
    assert line.index('class="r1-box') < line.index('class="r1-worth"')
    # The visible half only -- the score's tooltip quotes the sticker too, so
    # searching the whole line for the figure finds the wrong one.
    shown = line.split('class="r1-worth"')[1]
    assert shown.index("Buffett value:") < shown.index("80.00")


def test_only_one_price_is_quoted(page_config):
    """The margin-of-safety price sat beside this as "Buy under" and came out.

    It is exactly the band halved, so it told the reader nothing they could not
    work out, and two Rule #1 prices on a card that already carries a
    Morningstar fair value is one price too many. It survives in the score's
    tooltip.
    """
    from screener.dashboard import _card, _collect
    from screener.storage import Store

    with Store(page_config.storage.database) as store:
        _seed_signal(store, "AAA")
        store.upsert_rule_one("AAA", _reading(7))
        row = [r for r in _collect(store, page_config, page_config.horizon("1d"))
               if r.symbol == "AAA"][0]
    card = _card(row, page_config, page_config.horizon("1d"))

    assert "Buy under" not in card
    assert "30.00–40.00" not in card, "the halved band is gone from the card"
    assert "60.00" not in card, "and so is the band's low end"
    tip = card.split('title="')[1].split('"')[0]
    assert "margin of safety" in tip, "still one hover away"


def test_a_collapsed_band_is_printed_once(page_config):
    """GOOGL's two rates both cap at 20%, and "761.63–761.63" is noise."""
    from screener.dashboard import _rule_one_value
    from screener.ruleone import RuleOne

    r = RuleOne(applicable=True, score=10, band="green", growth=20.0,
                conservative_growth=20.0, implied_growth=11.6, price=344.82,
                sticker=761.63, sticker_low=761.63, mos=380.81, eps=19.9)
    html = _rule_one_value(r, "USD")
    assert "761.63" in html
    assert "–" not in html, "one number, never a range, on the card"


def test_a_flat_earner_says_why_there_is_no_price(page_config):
    """Printing nothing would read as a missing number rather than a refused
    one, on a fifth of the watchlist."""
    from screener.dashboard import _rule_one_value
    from screener.ruleone import RuleOne

    r = RuleOne(applicable=True, score=3, band="red", growth=0.0,
                conservative_growth=0.0, implied_growth=12.9, price=549.90,
                sticker=52.50, sticker_low=52.50, mos=26.25, eps=26.55)
    # Nothing at all: the score beside it still stands, and an explanation
    # nobody asked for is exactly the clutter this card was losing.
    assert _rule_one_value(r, "USD") == ""


def test_a_wildly_disagreeing_band_says_so(page_config):
    """Rolls-Royce spans 0.95 to 16.26 against a 1,502 price. The number is
    honest and the spread is the actual finding."""
    from screener.dashboard import _rule_one_value
    from screener.ruleone import RuleOne

    r = RuleOne(applicable=True, score=2, band="red", growth=19.0,
                conservative_growth=0.0, implied_growth=40.0, price=1502.20,
                sticker=16.26, sticker_low=0.95, mos=8.13, eps=0.48)
    from screener.dashboard import _spread_note

    # Not a badge on the card any more -- a sentence where someone asking about
    # the price will find it.
    assert "r1v-wide" not in _rule_one_value(r, "GBP")
    note = _spread_note(r)
    assert "0.95" in note and "17x spread" in note
    assert "trust the score" in note


def test_a_tight_band_is_not_flagged(page_config):
    from screener.dashboard import _rule_one_value
    from screener.ruleone import RuleOne

    r = RuleOne(applicable=True, score=10, band="green", growth=20.0,
                conservative_growth=15.8, implied_growth=12.9, price=258.63,
                sticker=475.70, sticker_low=332.17, mos=237.85, eps=6.5)
    from screener.dashboard import _spread_note

    assert _spread_note(r) == "", "a 1.4x band needs no caveat"


def test_a_non_dollar_value_names_its_currency(page_config):
    from screener.dashboard import _rule_one_value
    from screener.ruleone import RuleOne

    r = RuleOne(applicable=True, score=5, band="amber", growth=14.7,
                conservative_growth=9.8, implied_growth=22.7, price=1506.0,
                sticker=766.30, sticker_low=391.04, mos=383.15, eps=12.0)
    assert "EUR" in _rule_one_value(r, "EUR")
    assert "USD" not in _rule_one_value(r, "USD"), "the majority currency stays unsaid"


def test_the_band_survives_a_round_trip(tmp_path):
    from screener.ruleone import evaluate
    from screener.storage import Store

    r = evaluate(price=100.0, eps_ttm=5.0, eps_growth_ttm=18.0,
                 eps_growth_fy=14.0, sales_growth_ttm=12.0)
    with Store(tmp_path / "t.db") as store:
        store.upsert_rule_one("AAA", r)
        back = store.rule_one_readings()["AAA"]
    assert back.value_band == pytest.approx(r.value_band)
    assert back.eps == pytest.approx(5.0)


def test_a_database_without_the_band_columns_gains_them(tmp_path):
    """Rule #1 shipped before the value did, so an existing rule_one table has
    neither column and must not need a rebuild."""
    import sqlite3

    from screener.storage import Store

    path = tmp_path / "old.db"
    with Store(path) as store:
        store.upsert_rule_one("AAA", _reading(7))
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE rule_one DROP COLUMN sticker_low")
    con.execute("ALTER TABLE rule_one DROP COLUMN eps")
    con.commit()
    con.close()

    with Store(path) as store:          # migrates on open
        back = store.rule_one_readings()["AAA"]
    assert back.sticker == 80.0, "the reading survives"
    assert back.sticker_low is None, "the new column is simply empty"
    assert back.value_band is None, "and no band is invented for it"
