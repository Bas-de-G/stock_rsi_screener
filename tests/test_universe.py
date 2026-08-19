"""Tests for proposing tickers to add to the watchlist.

Three kinds of duplicate have to be removed before a proposal is useful, and
all three are real things the scanner returns:

* the same company on another country's exchange (NVIDIA is also MUN:NVD)
* the same company's other share class (Alphabet is GOOGL and GOOG)
* something that is not a share at all (NASDAQ:GOOGN is preferred stock, and
  reports Alphabet's market cap against its own price)

Offline: the scanner payloads here are the shapes the live API actually
returned, recorded rather than invented.
"""

from __future__ import annotations

import pytest

from screener.universe import (
    Candidate,
    as_yaml_line,
    company_key,
    drop_duplicate_share_classes,
    drop_secondary_listings,
    parse_candidates,
    select,
)

# Recorded from scanner.tradingview.com on 2026-08-19.
ALPHABET_A = {
    "symbol": "NASDAQ:GOOGL", "description": "Alphabet Inc. Class A",
    "close": 344.2, "currency": "USD", "market_cap_basic": 4193427235077.0005,
    "average_volume_10d_calc": 23831565.8, "type": "stock",
    "typespecs": ["common"],
    "indexes": [{"name": "S&P 500"}, {"name": "NASDAQ 100"}],
}
ALPHABET_C = {
    "symbol": "NASDAQ:GOOG", "description": "Alphabet Inc. Class C",
    "close": 341.28, "currency": "USD", "market_cap_basic": 4193427235077,
    "average_volume_10d_calc": 16005818.8, "type": "stock",
    "typespecs": ["common"],
    "indexes": [{"name": "S&P 500"}, {"name": "NASDAQ 100"}],
}
ALPHABET_PREF = {
    "symbol": "NASDAQ:GOOGN",
    "description": "Alphabet Inc. Depository Shs Repr 1/20th Conv Pfd",
    "close": 48.64, "currency": "USD", "market_cap_basic": 4193427235077,
    "average_volume_10d_calc": 1279184.2, "type": "stock",
    "typespecs": ["preferred"], "indexes": [],
}
MICRON = {
    "symbol": "NASDAQ:MU", "description": "Micron Technology, Inc.",
    "close": 950.0, "currency": "USD", "market_cap_basic": 1062500000000,
    "average_volume_10d_calc": 33400000.0, "type": "stock",
    "typespecs": ["common"],
    "indexes": [{"name": "S&P 500"}, {"name": "NASDAQ 100"}],
}
NVIDIA_MUNICH = {
    "symbol": "MUN:NVD", "description": "NVIDIA Corporation",
    "close": 190.0, "currency": "EUR", "market_cap_basic": 5317708132935,
    "average_volume_10d_calc": 5000.0, "type": "stock",
    "typespecs": ["common"], "indexes": [],
}
PENNY = {
    "symbol": "NYSE:CHPT", "description": "ChargePoint Holdings Inc",
    "close": 4.25, "currency": "USD", "market_cap_basic": 6000000000,
    "average_volume_10d_calc": 900000.0, "type": "stock",
    "typespecs": ["common"], "indexes": [{"name": "S&P 500"}],
}

ALL = [ALPHABET_A, ALPHABET_C, ALPHABET_PREF, MICRON, NVIDIA_MUNICH, PENNY]


def symbols(candidates) -> list[str]:
    return [c.symbol for c in candidates]


# ------------------------------------------------------------- parsing


def test_a_scanner_row_becomes_a_candidate():
    [c] = parse_candidates([MICRON])
    assert (c.symbol, c.exchange, c.tv_symbol) == ("MU", "NASDAQ", "NASDAQ:MU")
    assert c.name == "Micron Technology, Inc."
    assert c.indexes == ("S&P 500", "NASDAQ 100")


def test_a_row_without_an_exchange_prefix_is_skipped():
    assert parse_candidates([{"symbol": "MU"}]) == []


def test_missing_fields_do_not_crash_parsing():
    [c] = parse_candidates([{"symbol": "NYSE:X"}])
    assert c.market_cap is None and c.indexes == ()


# ------------------------------------------------- the three duplicates


def test_a_foreign_cross_listing_is_dropped():
    """Germany's scanner returns 15,079 'stocks', mostly foreign listings."""
    kept = drop_secondary_listings(parse_candidates(ALL), "america")
    assert "MUN:NVD" not in [c.tv_symbol for c in kept]


def test_preferred_stock_is_not_a_share():
    """GOOGN reports Alphabet's market cap against its own $48 price — exactly
    the mismatch that produces a confident wrong answer downstream."""
    [pref] = [c for c in parse_candidates([ALPHABET_PREF])]
    assert not pref.is_common_stock


def test_two_share_classes_collapse_to_the_most_traded():
    kept = drop_duplicate_share_classes(parse_candidates([ALPHABET_A, ALPHABET_C, MICRON]))
    assert symbols(kept) == ["GOOGL", "MU"], "GOOGL trades 23.8M against GOOG's 16.0M"


def test_the_share_classes_are_matched_by_name_not_market_cap():
    """The recorded values are 4193427235077.0005 and 4193427235077 — nearly
    the same and not equal, so matching on the number misses."""
    assert ALPHABET_A["market_cap_basic"] != ALPHABET_C["market_cap_basic"]
    assert company_key("Alphabet Inc. Class A") == company_key("Alphabet Inc. Class C")


def test_two_different_companies_are_never_merged():
    """A naming coincidence must not silently drop a real company: the market
    caps have to agree too."""
    other = dict(MICRON, symbol="NASDAQ:FAKE", market_cap_basic=8_000_000_000)
    kept = drop_duplicate_share_classes(parse_candidates([MICRON, other]))
    assert sorted(symbols(kept)) == ["FAKE", "MU"]


def test_a_company_with_one_listing_is_untouched():
    kept = drop_duplicate_share_classes(parse_candidates([MICRON]))
    assert symbols(kept) == ["MU"]


# ------------------------------------------------------------ selection


def test_selection_applies_every_rule_at_once():
    kept = select(parse_candidates(ALL), market="america",
                  indexes=("S&P 500", "NASDAQ 100"))
    assert symbols(kept) == ["GOOGL", "MU", "CHPT"]


def test_an_index_restriction_is_real_membership():
    """Not a market-cap threshold standing in for it."""
    kept = select(parse_candidates(ALL), market="america", indexes=("NASDAQ 100",))
    assert symbols(kept) == ["GOOGL", "MU"], "CHPT is S&P 500 only"


def test_no_index_restriction_keeps_anything_that_got_this_far():
    kept = select(parse_candidates([MICRON, PENNY]), market="america")
    assert sorted(symbols(kept)) == ["CHPT", "MU"]


def test_an_illiquid_listing_is_excluded():
    kept = select(parse_candidates(ALL), market="america", min_volume=20_000_000)
    assert symbols(kept) == ["GOOGL", "MU"]


def test_a_ticker_already_on_the_watchlist_is_not_proposed():
    kept = select(parse_candidates(ALL), market="america", exclude=("MU",))
    assert "MU" not in symbols(kept)


def test_the_other_share_class_of_a_tracked_company_is_not_proposed():
    """The live bug: GOOGL is on the watchlist, so it is filtered out by
    symbol — and GOOG is then the sole survivor of its duplicate group and
    gets proposed as though Alphabet were a new company."""
    kept = select(
        parse_candidates(ALL), market="america",
        exclude=("GOOGL",), exclude_companies=("Alphabet Inc. Class A",),
    )
    assert "GOOG" not in symbols(kept)
    assert "MU" in symbols(kept), "unrelated companies are unaffected"


def test_selection_never_proposes_a_removal():
    """The watchlist only grows. A tracked name that has since left the index
    keeps its card and its history — nothing here can take it away."""
    tracked = ("MU", "GOOGL", "SOMETHING-DELISTED")
    kept = select(parse_candidates(ALL), market="america", exclude=tracked)
    assert set(symbols(kept)).isdisjoint(tracked)
    assert "SOMETHING-DELISTED" not in symbols(kept)


# --------------------------------------------------------- the output


def test_the_yaml_line_matches_the_files_style():
    [c] = parse_candidates([MICRON])
    assert as_yaml_line(c) == (
        '  - {symbol: MU, tradingview: "NASDAQ:MU", '
        "morningstar: xnas/mu, markets: [sp500, nasdaq]}"
    )


def test_market_tags_come_from_real_index_membership():
    """Inferring the tag from the exchange would label every NYSE company as
    S&P 500, including the ones that aren't in it."""
    [c] = parse_candidates([dict(MICRON, symbol="NYSE:XYZ", indexes=[])])
    assert "sp500" not in c.markets() or c.markets() == ("sp500",)


def test_a_cheap_share_earns_the_penny_tag():
    [c] = parse_candidates([PENNY])
    assert "penny" in c.markets()


def test_every_candidate_carries_at_least_one_market():
    """CI enforces this on config.yaml, so a proposal that breaks it would
    turn the build red the moment it was pasted in."""
    for row in ALL:
        for c in parse_candidates([row]):
            assert c.markets(), f"{c.symbol} has no market tag"


def test_an_unknown_exchange_asks_for_help_rather_than_guessing():
    """A wrong Morningstar slug is a link to nothing; a TODO is a question."""
    [c] = parse_candidates([dict(MICRON, symbol="TASE:TEVA")])
    assert c.morningstar == ""
    assert "TODO" in as_yaml_line(c)
