"""Tests for config loading and validation.

A typo in config.yaml must fail loudly at load time — a screener that silently
runs the wrong valuation rule is worse than one that refuses to start.
"""

from __future__ import annotations

import pytest

from screener.config import load_config
from screener.tradingview import rsi_field_name

VALID = """
tickers:
  - symbol: nvda
    tradingview: NASDAQ:NVDA
    morningstar: xnas/nvda
rsi:
  period: 14
  threshold: 30
  interval: "1D"
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: fair_value_below_price
storage:
  database: data/test.db
  csv_dir: data
"""


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_loads_valid_config(tmp_path):
    config = load_config(write(tmp_path, VALID))
    assert config.tickers[0].symbol == "NVDA"  # normalised to upper case
    assert config.rsi.threshold == 30.0
    assert config.signal.window_unit == "calendar"


def test_morningstar_url_is_built_from_the_slug(tmp_path):
    config = load_config(write(tmp_path, VALID))
    assert config.ticker("nvda").morningstar_url == (
        "https://www.morningstar.com/stocks/xnas/nvda/quote"
    )


def test_unknown_ticker_lookup_raises(tmp_path):
    config = load_config(write(tmp_path, VALID))
    with pytest.raises(KeyError):
        config.ticker("TSLA")


def test_rejects_unknown_valuation_rule(tmp_path):
    bad = VALID.replace("valuation_rule: fair_value_below_price", "valuation_rule: cheap")
    with pytest.raises(ValueError, match="valuation_rule"):
        load_config(write(tmp_path, bad))


def test_rejects_unknown_window_unit(tmp_path):
    bad = VALID.replace("window_unit: calendar", "window_unit: fortnights")
    with pytest.raises(ValueError, match="window_unit"):
        load_config(write(tmp_path, bad))


def test_rejects_rsi_period_tradingview_cannot_serve(tmp_path):
    bad = VALID.replace("period: 14", "period: 9")
    with pytest.raises(ValueError, match="rsi.period"):
        load_config(write(tmp_path, bad))


def test_accepts_the_other_supported_period(tmp_path):
    ok = VALID.replace("period: 14", "period: 7")
    assert load_config(write(tmp_path, ok)).rsi.period == 7


def test_rejects_ticker_missing_a_field(tmp_path):
    bad = VALID.replace("    morningstar: xnas/nvda\n", "")
    with pytest.raises(ValueError, match="morningstar"):
        load_config(write(tmp_path, bad))


def test_rejects_empty_ticker_list(tmp_path):
    bad = "tickers: []\n"
    with pytest.raises(ValueError, match="No tickers"):
        load_config(write(tmp_path, bad))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_rule_descriptions_name_the_direction():
    from screener.config import SignalConfig

    above = SignalConfig(valuation_rule="fair_value_below_price").describe_rule()
    below = SignalConfig(valuation_rule="price_below_fair_value").describe_rule()
    assert "ABOVE" in above and "fair value < price" in above
    assert "BELOW" in below and "price < fair value" in below


# ------------------------------------------------------ TradingView fields


@pytest.mark.parametrize(
    "period,interval,expected",
    [
        (14, "1D", "RSI"),      # daily is the scanner default, no suffix
        (7, "1D", "RSI7"),
        (14, "1W", "RSI|1W"),
        (7, "60", "RSI7|60"),
    ],
)
def test_rsi_field_names(period, interval, expected):
    assert rsi_field_name(period, interval) == expected


def test_unsupported_period_field_raises():
    from screener.tradingview import MarketDataError

    with pytest.raises(MarketDataError, match="only serves"):
        rsi_field_name(9, "1D")


def test_morningstar_headless_defaults_to_false(tmp_path):
    """Headless has been observed to trip Morningstar's bot-protection;
    false is the safe default, matching the interactive login flow."""
    config = load_config(write(tmp_path, VALID))
    assert config.morningstar.headless is False


def test_morningstar_headless_can_be_enabled_explicitly(tmp_path):
    config = load_config(write(tmp_path, VALID + "\nmorningstar:\n  headless: true\n"))
    assert config.morningstar.headless is True
