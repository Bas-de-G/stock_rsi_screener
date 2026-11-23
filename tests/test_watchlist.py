"""Fair values for an outside ranked CSV, written to a private copy."""

import csv
import datetime as dt
import subprocess

import pytest

from screener import watchlist as wl

HEADER = "Watchlist Rank,Ticker,Stock Price (USD),Daily RSI (14) / 2026-11-20\n"


def write_csv(path, lines, header=HEADER, bom=True):
    path.write_text(("﻿" if bom else "") + header + "".join(f"{l}\n" for l in lines),
                    encoding="utf-8")
    return path


def fake_scan(existing):
    """A TradingView stand-in: returns only the symbols in `existing`."""
    def scan(candidates):
        return {c: existing[c] for c in candidates if c in existing}
    return scan


def test_rows_come_back_in_rank_order_not_file_order(tmp_path):
    src = write_csv(tmp_path / "w.csv", ["3,CCC,1", "1,AAA,1", "2,BBB,1"])
    _, rows = wl.read_rows(src)
    assert [wl.symbol_of(r) for r in rows] == ["AAA", "BBB", "CCC"]


def test_unranked_rows_follow_the_ranked_ones_in_file_order(tmp_path):
    src = write_csv(tmp_path / "w.csv", [",ZZZ,1", "2,BBB,1", ",YYY,1", "1,AAA,1"])
    _, rows = wl.read_rows(src)
    assert [wl.symbol_of(r) for r in rows] == ["AAA", "BBB", "ZZZ", "YYY"]


def test_a_file_without_a_ticker_column_is_refused(tmp_path):
    src = write_csv(tmp_path / "w.csv", ["1,AAA"], header="Rank,Symbol\n")
    with pytest.raises(wl.WatchlistError, match="Ticker"):
        wl.read_rows(src)


def test_listing_prefers_nasdaq_then_nyse_then_amex():
    scan = fake_scan({"NYSE:DUP": 10.0, "NASDAQ:DUP": 11.0, "AMEX:ONLY": 2.0, "NYSE:BRK.B": 500.0})
    found = wl.resolve_listings(["DUP", "ONLY", "BRK.B", "GONE"], scan)
    assert found["DUP"] == wl.Listing("NASDAQ:DUP", "xnas/dup", 11.0)
    assert found["ONLY"].morningstar == "xase/only"
    assert found["BRK.B"].morningstar == "xnys/brk.b"
    assert "GONE" not in found


def test_fill_copies_known_values_with_upside_against_the_files_price(tmp_path):
    src = write_csv(tmp_path / "w.csv", ["1,AAA,50", "2,BBB,10"])
    _, rows = wl.read_rows(src)
    listings = {"AAA": wl.Listing("NASDAQ:AAA", "xnas/aaa", 52.0)}
    filled = wl.fill_from_screener(rows, {"AAA": (75.0, "2026-11-01")}, listings)
    wl.mark_unchecked(rows)
    assert filled == 1
    aaa, bbb = rows
    assert aaa["Fair Value Status"] == wl.COVERED
    assert aaa["Morningstar Fair Value"] == "75"
    assert aaa["Upside to Fair Value"] == "+50.0%"  # 75 / 50, the file's own price
    assert aaa["Fair Value Source"] == "screener"
    assert aaa["Morningstar Listing"] == "xnas/aaa"
    assert bbb["Fair Value Status"] == wl.PENDING


def test_a_fresher_scrape_is_not_overwritten_by_an_older_file_value(tmp_path):
    src = write_csv(tmp_path / "w.csv", ["1,AAA,50"])
    _, rows = wl.read_rows(src)
    wl.record(rows[0], wl.COVERED, fair_value=90.0, checked="2026-11-20", source="scraped")
    wl.fill_from_screener(rows, {"AAA": (75.0, "2026-10-01")}, {})
    assert rows[0]["Morningstar Fair Value"] == "90"


def test_due_skips_settled_answers_until_they_go_stale():
    today = dt.date(2026, 11, 23)
    rows = [
        {"Ticker": "NEW"},
        {"Ticker": "FRESH", "Fair Value Status": wl.COVERED, "Fair Value Checked": "2026-11-20"},
        {"Ticker": "NONE", "Fair Value Status": wl.NO_FAIR_VALUE, "Fair Value Checked": "2026-11-01"},
        {"Ticker": "STALE", "Fair Value Status": wl.COVERED, "Fair Value Checked": "2026-09-01"},
        {"Ticker": "BROKE", "Fair Value Status": wl.FAILED, "Fair Value Checked": ""},
    ]
    assert [r["Ticker"] for r in wl.due(rows, today, 40)] == ["NEW", "STALE", "BROKE"]


def test_a_rerun_keeps_earlier_answers_even_after_a_reranked_export(tmp_path):
    first = write_csv(tmp_path / "w.csv", ["1,AAA,10", "2,BBB,10"])
    columns, rows = wl.read_rows(first)
    wl.record(rows[1], wl.NO_FAIR_VALUE, checked="2026-11-23")
    out = tmp_path / "out.csv"
    wl.write_rows(out, columns, rows)

    rerank = write_csv(tmp_path / "w2.csv", ["1,BBB,10", "2,CCC,10", "3,AAA,10"])
    _, fresh = wl.read_rows(rerank)
    _, previous = wl.read_rows(out)
    wl.merge_previous(fresh, previous)
    assert fresh[0]["Fair Value Status"] == wl.NO_FAIR_VALUE
    assert not fresh[1].get("Fair Value Status")


def test_output_keeps_every_input_column_then_adds_the_fair_value_ones(tmp_path):
    src = write_csv(tmp_path / "w.csv", ["1,AAA,10"])
    columns, rows = wl.read_rows(src)
    wl.mark_unchecked(rows)
    out = tmp_path / "deep" / "out.csv"
    wl.write_rows(out, columns, rows)
    header = next(csv.reader(out.open(encoding="utf-8")))
    assert header == [*columns, *wl.FV_COLUMNS]
    assert columns[0] == "Watchlist Rank"  # the BOM did not stick to the first name
    assert not out.with_suffix(".csv.tmp").exists()


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("private/\n")
    return tmp_path


def test_writing_into_the_repo_unignored_is_refused(repo):
    with pytest.raises(wl.WatchlistError, match="not gitignored"):
        wl.assert_private(repo / "fred.csv", repo)


def test_writing_under_private_or_outside_the_repo_is_allowed(repo, tmp_path_factory):
    wl.assert_private(repo / "private" / "fred.csv", repo)
    wl.assert_private(tmp_path_factory.mktemp("elsewhere") / "fred.csv", repo)


def test_the_real_repo_ignores_private():
    from pathlib import Path
    root = Path(wl.__file__).resolve().parent.parent
    wl.assert_private(root / "private" / "anything.csv", root)
    with pytest.raises(wl.WatchlistError):
        wl.assert_private(root / "fred.csv", root)


def test_the_command_fills_scrapes_in_rank_order_and_resumes(tmp_path, monkeypatch):
    """End to end with fakes for TradingView and Morningstar."""
    import argparse
    from screener import cli, morningstar, tradingview
    from screener.config import load_config
    from screener.morningstar import ScrapeResult

    config = load_config()
    known = cli._us_fair_values(config)
    held = next(iter(known))  # a ticker the screener already values
    src = write_csv(tmp_path / "fred.csv",
                    [f"1,{held},10", "2,AAA,10", "3,BBB,20", "4,CCC,5", "5,DDD,5", "6,GONE,1"])
    listed = {f"NASDAQ:{s}": 10.0 for s in (held, "AAA", "BBB", "CCC", "DDD")}
    monkeypatch.setattr(tradingview, "existing_closes", fake_scan(listed))

    visits = []

    def fake_scrape(tickers, ms_config, on_result=None, reference_prices=None, **_):
        assert ms_config.debug_on_failure is False
        for t in tickers:
            visits.append(t.symbol)
            assert t.morningstar == f"xnas/{t.symbol.lower()}"
            if t.symbol == "AAA":
                on_result(t, ScrapeResult(t.symbol, 10.0, 30.0), None)
            elif t.symbol == "BBB":
                on_result(t, ScrapeResult(t.symbol, 10.0, None), None)
            else:
                on_result(t, None, "timed out")
        return []

    monkeypatch.setattr(morningstar, "scrape_many", fake_scrape)
    out = tmp_path / "out.csv"
    args = argparse.Namespace(csv=str(src), out=str(out), limit=3, max_age=40, dry_run=False)
    assert cli.cmd_watchlist(config, args) == 0
    assert visits == ["AAA", "BBB", "CCC"]  # rank order; the held one is never visited

    rows = {wl.symbol_of(r): r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert rows[held]["Fair Value Source"] == "screener"
    assert rows["AAA"]["Fair Value Status"] == wl.COVERED
    assert rows["AAA"]["Upside to Fair Value"] == "+200.0%"
    assert rows["BBB"]["Fair Value Status"] == wl.NO_FAIR_VALUE
    assert rows["CCC"]["Fair Value Status"] == wl.FAILED
    assert rows["DDD"]["Fair Value Status"] == wl.PENDING
    assert rows["GONE"]["Fair Value Status"] == wl.UNLISTED

    # Second run: the failure is retried, the settled answers are not.
    visits.clear()
    assert cli.cmd_watchlist(config, args) == 0
    assert visits == ["CCC", "DDD"]


def test_values_the_screener_holds_are_never_revisited_even_when_old():
    rows = [{"Ticker": "OLD", "Fair Value Status": wl.COVERED,
             "Fair Value Checked": "2025-01-01", "Fair Value Source": "screener"}]
    assert wl.due(rows, dt.date(2026, 11, 23), 40) == []
