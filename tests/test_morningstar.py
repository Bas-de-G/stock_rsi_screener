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
