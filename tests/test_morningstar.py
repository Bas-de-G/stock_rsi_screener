"""Tests for Morningstar parsing.

The page text fixture below is modelled on a real IBM quote page, including
the decoys that sit next to the numbers we want: the "Price vs Fair Value"
heading, the 1-Star/5-Star price rows, and the card's previous-close "Price"
row that differs from the live quote at the top.

These run offline — no browser, no network, no session.
"""

from __future__ import annotations

import pytest

from screener.morningstar import (
    MorningstarError,
    ScrapeResult,
    _coerce_number,
    _deep_find_number,
    _extract,
    _fair_value_from_text,
    _looks_like_bot_challenge,
    _looks_logged_out,
    _price_from_text,
    to_valuation,
)

# Mirrors the layout of a signed-in IBM quote page.
IBM_PAGE = """
Stocks
International Business Machines Corp IBM
Stock XNYS Rating as of Jul 24, 2026
$217.24 +3.07 (1.43%)
As of Jul 27, 2026 at 8:25:24 PM Cboe BZX Real-Time Price Open
Previous Close Price $214.19
Day Range $215.39-219.60
52-Week Range $199.19-332.41
Price vs Fair Value
IBM is trading within a range we consider fairly valued.
Fair Value $225.00
Jul 23, 2026
Uncertainty Medium
Price $214.19
Jul 24, 2026
1-Star Price $303.75
5-Star Price $157.50
Economic Moat Narrow
Capital Allocation Exemplary
"""

LOGGED_OUT_PAGE = """
International Business Machines Corp IBM
$217.24 +3.07 (1.43%)
Price vs Fair Value
Sign in to unlock Morningstar's fair value estimate.
Start a free trial
"""


# ------------------------------------------------------------ fair value


def test_reads_fair_value_from_the_card():
    assert _fair_value_from_text(IBM_PAGE) == 225.00


def test_fair_value_skips_the_price_vs_fair_value_heading():
    """The heading contains 'Fair Value' but no number of its own."""
    assert _fair_value_from_text(IBM_PAGE) != 217.24


def test_fair_value_absent_when_signed_out():
    assert _fair_value_from_text(LOGGED_OUT_PAGE) is None


# ------------------------------------------------------------ price


def test_reads_the_live_price_not_the_cards_previous_close():
    """The card says 214.19 (previous close); the current price is 217.24."""
    assert _price_from_text(IBM_PAGE) == 217.24


def test_price_is_not_confused_by_star_price_rows():
    assert _price_from_text(IBM_PAGE) not in (303.75, 157.50)


# ------------------------------------------------------------ signed-out


def test_detects_a_signed_out_page():
    assert _looks_logged_out(LOGGED_OUT_PAGE) is True


def test_signed_in_page_is_not_flagged():
    assert _looks_logged_out(IBM_PAGE) is False


# ------------------------------------------------------------ JSON walking


def test_deep_find_locates_a_nested_fair_value():
    payload = {"data": {"valuation": [{"fairValue": 225.0, "other": 1}]}}
    assert _deep_find_number(payload, ("fairValue",)) == 225.0


def test_deep_find_returns_none_when_absent():
    assert _deep_find_number({"a": {"b": 1}}, ("fairValue",)) is None


def test_deep_find_handles_string_numbers():
    assert _deep_find_number({"fairValue": "1,225.50"}, ("fairValue",)) == 1225.50


def test_deep_find_survives_deeply_nested_lists():
    payload = {"x": [[[{"lastPrice": 217.24}]]]}
    assert _deep_find_number(payload, ("lastPrice",)) == 217.24


@pytest.mark.parametrize(
    "value,expected",
    [
        (225.0, 225.0),
        ("$225.00", 225.0),
        ("1,225.50", 1225.5),
        (0, None),        # a zero price is missing data, not a real quote
        (-5, None),
        (True, None),     # booleans must not read as 1.0
        ("n/a", None),
        (None, None),
    ],
)
def test_coerce_number(value, expected):
    assert _coerce_number(value) == expected


# ------------------------------------------------------------ end to end


def test_extract_prefers_network_json_over_page_text():
    """JSON is strategy 1: when present it wins over the scraped text."""
    captured = [{"fairValue": 230.0, "lastPrice": 218.0}]
    result = _extract("IBM", captured, IBM_PAGE)
    assert (result.fair_value, result.price) == (230.0, 218.0)
    assert result.method == "network-json"


def test_extract_falls_back_to_text_when_no_json():
    result = _extract("IBM", [], IBM_PAGE)
    assert (result.fair_value, result.price) == (225.00, 217.24)
    assert "text" in result.method
    assert result.complete


def test_extract_reports_incomplete_when_signed_out():
    result = _extract("IBM", [], LOGGED_OUT_PAGE)
    assert result.fair_value is None
    assert result.complete is False


def test_extract_picks_up_the_qualitative_fields():
    result = _extract("IBM", [], IBM_PAGE)
    assert result.moat == "Narrow"
    assert result.uncertainty == "Medium"
    assert result.fair_value_date == "Jul 23, 2026"


def test_to_valuation_rejects_incomplete_data():
    """A half-read page must raise, never store a fair value of zero or None."""
    partial = ScrapeResult(symbol="IBM", price=217.24, fair_value=None)
    with pytest.raises(MorningstarError, match="Incomplete"):
        to_valuation(partial, "2026-07-27")


def test_to_valuation_builds_a_storable_row():
    result = _extract("IBM", [], IBM_PAGE)
    valuation = to_valuation(result, "2026-07-27")
    assert valuation.symbol == "IBM"
    assert valuation.date == "2026-07-27"
    assert valuation.price == 217.24
    assert valuation.fair_value == 225.00


# ------------------------------------------------- unit mismatch guard


from screener.morningstar import UnitMismatchError, check_units


def result(price, fair_value, symbol="RR"):
    return ScrapeResult(symbol=symbol, price=price, fair_value=fair_value)


def test_matching_units_pass():
    check_units(result(217.24, 225.00, "IBM"))  # does not raise


def test_pence_against_pounds_is_rejected():
    """The Rolls-Royce hazard: GBX 1413.60 price vs GBP 14.50 fair value.

    A 97x 'discount' would sail through the valuation gate and mark a wildly
    overvalued stock as a screaming buy.
    """
    with pytest.raises(UnitMismatchError, match="unit mismatch"):
        check_units(result(1413.60, 14.50))


def test_pounds_against_pence_is_rejected():
    """Same mismatch the other way round."""
    with pytest.raises(UnitMismatchError):
        check_units(result(14.50, 1413.60))


def test_a_genuinely_wide_but_plausible_gap_is_allowed():
    """A stock at half its fair value is a real thing, not a unit bug."""
    check_units(result(50.0, 100.0))
    check_units(result(100.0, 50.0))


def test_boundaries_are_inclusive():
    check_units(result(10.0, 100.0))   # ratio 0.1
    check_units(result(100.0, 10.0))   # ratio 10.0


def test_just_outside_the_boundary_is_rejected():
    with pytest.raises(UnitMismatchError):
        check_units(result(100.0, 9.0))


def test_incomplete_results_are_left_to_the_caller():
    """Nothing to compare — `to_valuation` is what rejects these."""
    check_units(result(None, 225.0))
    check_units(result(217.0, None))
    check_units(result(None, None))


def test_the_error_names_the_symbol_and_the_ratio():
    with pytest.raises(UnitMismatchError) as exc:
        check_units(result(1413.60, 14.50, "RR"))
    assert "RR" in str(exc.value)
    assert "97" in str(exc.value)


# ------------------------------------------------- bot-challenge detection
#
# This is a regression test against a real failure: a headless `check-auth`
# run against AAPL got served an AWS WAF CAPTCHA instead of the quote page,
# with a perfectly valid session cookie. The text below is what
# `page.inner_text("body")` returns for that page -- the *rendered* text, so
# it excludes the `display: none` error banners and everything living only in
# <script src> URLs (which is why "awswaf.com" itself isn't a marker: it never
# appears in rendered text, only in a script tag's src attribute).

WAF_CHALLENGE_PAGE = """
Let's confirm you are human
Complete the security check before continuing. This step verifies that you are not a bot, which helps to protect your account and prevent spam.
Begin
English
"""


def test_the_actual_captured_challenge_page_is_detected():
    assert _looks_like_bot_challenge(WAF_CHALLENGE_PAGE)


def test_a_normal_quote_page_is_not_flagged_as_a_challenge():
    assert not _looks_like_bot_challenge(IBM_PAGE)


def test_a_signed_out_page_is_not_confused_with_a_bot_challenge():
    """Two different failure modes, two different fixes -- re-login vs.
    retry with a visible browser -- so they must not collide."""
    assert not _looks_like_bot_challenge(LOGGED_OUT_PAGE)
    assert _looks_logged_out(LOGGED_OUT_PAGE)
    assert not _looks_logged_out(WAF_CHALLENGE_PAGE)


# ------------------------------------------------- non-dollar currencies
#
# Regression: `_price_from_text`'s primary regex hard-required a literal "$",
# and `_MONEY`'s optional "$" wouldn't step over a "€" either -- so price came
# back None for every Amsterdam and London ticker while fair value parsed
# fine. That half-filled result then flowed into the scrape as if it were
# usable. 19 of the 54 configured tickers are non-USD.


def euro_page(price="37.64", fair="44.00", cur="€"):
    return f"""
Randstad NV RAND
Stock XAMS Rating as of Aug 3, 2026
{cur}{price} +0.42 (1.13%)
Previous Close Price {cur}37.22
Price vs Fair Value
RAND is trading within a range we consider fairly valued.
Fair Value {cur}{fair}
Jul 30, 2026
Uncertainty Medium
Price {cur}37.22
1-Star Price {cur}59.40
5-Star Price {cur}30.80
Economic Moat Narrow
"""


@pytest.mark.parametrize("cur", ["$", "€", "£"])
def test_price_parses_for_every_supported_currency(cur):
    assert _price_from_text(euro_page(cur=cur)) == 37.64


@pytest.mark.parametrize("cur", ["$", "€", "£"])
def test_fair_value_parses_for_every_supported_currency(cur):
    assert _fair_value_from_text(euro_page(cur=cur)) == 44.00


@pytest.mark.parametrize("cur", ["$", "€", "£"])
def test_extract_is_complete_for_every_supported_currency(cur):
    r = _extract("RAND", [], euro_page(cur=cur))
    assert r.complete, f"{cur} page produced price={r.price} fair_value={r.fair_value}"
    assert r.fair_value_date == "Jul 30, 2026"


def test_the_euro_page_still_avoids_the_star_price_decoys():
    """1-Star/5-Star Price sit right below Fair Value on the card and would be
    easy to grab by accident."""
    r = _extract("RAND", [], euro_page())
    assert r.fair_value == 44.00
    assert r.fair_value not in (59.40, 30.80)


# --------------------------------- fair value disambiguation by known price
#
# Two real failures motivated this. On AMD the page's own price parsed as 1.62
# (AMD trades near $200), and on UNH the fair value parsed as 16.00 against a
# $409 price. Both were caught by the unit guard and discarded -- correct, but
# it meant neither ticker could ever be scraped.
#
# The screener already holds an authoritative close from TradingView, so it
# can hand that to the extractor: it stands in for a price the page won't
# yield, and it rules out fair-value candidates of the wrong magnitude.

from screener.morningstar import _fair_value_candidates

MULTI_CANDIDATE_PAGE = """
UnitedHealth Group Inc UNH
$409.62 +2.10 (0.51%)
Price vs Fair Value
Fair Value $16.00
Fair Value $530.00
1-Star Price $700.00
5-Star Price $300.00
"""


def test_all_fair_value_candidates_are_gathered():
    assert _fair_value_candidates(MULTI_CANDIDATE_PAGE) == [16.0, 530.0]


def test_the_vs_heading_is_still_skipped():
    assert 409.62 not in _fair_value_candidates(MULTI_CANDIDATE_PAGE)


def test_a_price_over_fair_value_ratio_label_is_skipped():
    """`Price/Fair Value` is a Morningstar metric in its own right; its value
    is a ratio like 0.85, not a valuation."""
    page = "Price/Fair Value 0.85\nFair Value $225.00\n"
    assert _fair_value_candidates(page) == [225.0]


def test_a_known_price_picks_the_plausible_candidate():
    """The UNH case: $16 against a $409 price is a parse error, not a view."""
    assert _fair_value_from_text(MULTI_CANDIDATE_PAGE, 409.62) == 530.0


def test_without_a_known_price_the_first_candidate_wins():
    """Unchanged fallback behaviour when the caller has no reference."""
    assert _fair_value_from_text(MULTI_CANDIDATE_PAGE) == 16.0


def test_no_plausible_candidate_returns_none_rather_than_a_wrong_one():
    page = "Fair Value $16.00\n"
    assert _fair_value_from_text(page, 4000.0) is None


def test_a_single_sane_candidate_is_unaffected_by_the_reference():
    assert _fair_value_from_text(IBM_PAGE, 217.24) == 225.00
    assert _fair_value_from_text(IBM_PAGE) == 225.00


def test_the_reference_price_substitutes_for_an_unparseable_page_price():
    """The AMD case: the page's own price came out as 1.62. The close we
    already hold is authoritative, so the scrape still succeeds."""
    page = "AMD\nFair Value $210.00\nsome layout we can't read a price from\n"
    result = _extract("AMD", [], page, reference_price=198.40)
    assert result.price == 198.40
    assert result.fair_value == 210.00
    assert result.complete
    assert "ref-price" in result.method


def test_the_reference_price_wins_over_the_pages_own():
    """Not a fallback -- an override. `_price_from_text` takes the first
    currency-prefixed figure on the page, which is often the wrong one; the
    close from TradingView is authoritative and is what the gate uses."""
    result = _extract("IBM", [], IBM_PAGE, reference_price=999.0)
    assert result.price == 999.0


def test_a_reference_price_keeps_the_unit_guard_meaningful():
    """Guard still fires when the fair value really is the wrong currency."""
    page = "RR\nFair Value 14.50\n"
    result = _extract("RR", [], page, reference_price=1413.60)
    # No candidate is within 10x of 1413.60, so nothing is claimed at all.
    assert result.fair_value is None
