"""Tests for how `scrape` handles results that come back half-filled.

Regression: `cmd_scrape`'s reporting callback assumed that whenever
`scrape_many` reported no *error*, the result was usable. It isn't always.
`_scrape_on_page` returns an incomplete `ScrapeResult` — without raising —
when a page loads fine, isn't a bot challenge and doesn't look signed out, but
extraction still finds nothing usable. That happened for every non-USD ticker
while the price regex hard-required a "$".

The result was a fair value written to the YAML with no price to compare it
against, immediately followed by `(price / fair_value)` dividing by None, which
`except MorningstarError` doesn't catch. Since targets are visited in order and
the euro ticker sorted last, the earlier tickers were already saved — so the
symptom was "everything scraped except that one," with a traceback.

Playwright is mocked; these stay offline like the rest of the suite.
"""

from __future__ import annotations

import argparse

import pytest

from screener import morningstar as ms_module
from screener.cli import cmd_scrape
from screener.config import load_config
from screener.fairvalues import load_fair_values
from screener.morningstar import ScrapeResult
from screener.storage import RsiPoint, Signal, Store


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
tickers:
  - {{symbol: IBM,  tradingview: "NYSE:IBM",      morningstar: xnys/ibm}}
  - {{symbol: RAND, tradingview: "EURONEXT:RAND", morningstar: xams/rand, yahoo: RAND.AS, currency: EUR}}
rsi: {{period: 14, threshold: 30, interval: "1D"}}
signal:
  window_days: 14
  window_unit: calendar
  valuation_rule: price_below_fair_value
  fire_without_valuation: true
storage:
  database: {tmp_path / "t.db"}
  csv_dir: {tmp_path}
  fair_values: {tmp_path / "fair_values.yaml"}
morningstar:
  state_file: {tmp_path / "auth.json"}
dashboard:
  output: {tmp_path / "t.html"}
  chart_days: 90
"""
    )
    return load_config(path)


@pytest.fixture()
def seeded(config):
    """Both tickers with history and a fired, in-window signal, so the default
    target selection picks up both."""
    import datetime as dt

    base = dt.date(2026, 6, 1)
    with Store(config.storage.database) as store:
        for sym in ("IBM", "RAND"):
            for i in range(30):
                d = (base + dt.timedelta(days=i)).isoformat()
                store.upsert_rsi_point(RsiPoint(sym, d, 100.0, 45.0, "live:tradingview"))
            store.record_signal(Signal(
                sym, "2026-06-10", "2026-06-12", "2026-06-15",
                None, None, False, False, True, "now",
            ))
    return config


def args(**kw):
    base = dict(all=False, symbols=None, dry_run=False, push=False, date=None, note=None)
    base.update(kw)
    return argparse.Namespace(**base)


def fake_scrape(results):
    """Stand in for `scrape_many`, yielding (ticker, result, error) triples.

    Patched on `screener.morningstar`, not `screener.cli` -- cmd_scrape does
    its import inside the function body, so the name is resolved from the
    source module at call time.
    """
    def _fake(tickers, ms_config, pause_range=(3.0, 8.0), on_result=None,
              reference_prices=None):
        out = []
        for t in tickers:
            entry = (t, results.get(t.symbol), None)
            out.append(entry)
            if on_result is not None:
                on_result(*entry)
        return out
    return _fake


def test_an_incomplete_result_is_reported_as_a_failure_not_recorded(
    monkeypatch, seeded, capsys
):
    """The exact RAND shape: fair value parsed, price didn't."""
    monkeypatch.setattr(ms_module, "scrape_many", fake_scrape({
        "IBM":  ScrapeResult("IBM", price=217.24, fair_value=225.0, method="text"),
        "RAND": ScrapeResult("RAND", price=None, fair_value=44.0),
    }))
    code = cmd_scrape(seeded, args())

    out = capsys.readouterr().out
    values = load_fair_values(seeded.storage.fair_values)
    assert "IBM" in values, "the good ticker must still be recorded"
    assert "RAND" not in values, "a price-less result must not be written"
    assert "nothing usable" in out
    assert "1 recorded, 1 failed" in out
    assert code == 0  # something succeeded, so not a hard failure


def test_a_missing_fair_value_is_also_rejected(monkeypatch, seeded, capsys):
    monkeypatch.setattr(ms_module, "scrape_many", fake_scrape({
        "IBM":  ScrapeResult("IBM", price=217.24, fair_value=225.0, method="text"),
        "RAND": ScrapeResult("RAND", price=37.64, fair_value=None),
    }))
    cmd_scrape(seeded, args())
    assert "RAND" not in load_fair_values(seeded.storage.fair_values)


def test_an_incomplete_result_does_not_crash_the_run(monkeypatch, seeded):
    """Before the guard this raised TypeError on (None / fair_value), which
    `except MorningstarError` does not catch — so the CLI died with a
    traceback partway through the batch."""
    monkeypatch.setattr(ms_module, "scrape_many", fake_scrape({
        "IBM":  ScrapeResult("IBM", price=None, fair_value=None),
        "RAND": ScrapeResult("RAND", price=None, fair_value=44.0),
    }))
    cmd_scrape(seeded, args())  # must not raise


def test_all_incomplete_exits_non_zero(monkeypatch, seeded):
    monkeypatch.setattr(ms_module, "scrape_many", fake_scrape({
        "IBM":  ScrapeResult("IBM", price=None, fair_value=None),
        "RAND": ScrapeResult("RAND", price=None, fair_value=44.0),
    }))
    assert cmd_scrape(seeded, args()) == 1


def test_complete_results_are_recorded_for_both_currencies(monkeypatch, seeded):
    monkeypatch.setattr(ms_module, "scrape_many", fake_scrape({
        "IBM":  ScrapeResult("IBM", price=217.24, fair_value=225.0, method="text"),
        "RAND": ScrapeResult("RAND", price=37.64, fair_value=44.0, method="text"),
    }))
    assert cmd_scrape(seeded, args()) == 0
    values = load_fair_values(seeded.storage.fair_values)
    assert values["IBM"].fair_value == 225.0
    assert values["RAND"].fair_value == 44.0
    assert values["RAND"].source == "scraped"
