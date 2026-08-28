"""Tests for suspending signals around an earnings release.

RSI cannot tell an ordinary correction from positioning ahead of results, and
the price gaps on the release whatever the chart said. So a signal inside the
window is shown and badged but not acted on: no rocket, no deal of the day, no
push to anyone's phone.

Offline throughout — the dates are fixtures, not fetches.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screener.earnings import (
    AFTER,
    BEFORE,
    CLEAR,
    DEFAULT_BLACKOUT_DAYS,
    earnings_window,
    sessions_until,
    to_date,
)

# A Wednesday, so a week can be stepped through without a weekend surprise.
WED = dt.date(2026, 8, 19)


def weekdays(start: dt.date, count: int) -> list[dt.date]:
    """`count` trading days ending on `start`, weekends skipped."""
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


HISTORY = weekdays(WED, 40)


# ------------------------------------------------------- counting sessions


def test_today_is_zero_sessions_away():
    assert sessions_until(WED, WED) == 0


def test_tomorrow_is_one_session_away():
    assert sessions_until(WED + dt.timedelta(days=1), WED) == 1


def test_a_weekend_does_not_count_as_two_sessions():
    """Friday to Monday is one session, not three days."""
    friday = dt.date(2026, 8, 21)
    monday = dt.date(2026, 8, 24)
    assert sessions_until(monday, friday) == 1


def test_a_date_already_past_is_zero():
    assert sessions_until(WED - dt.timedelta(days=5), WED) == 0


def test_a_week_ahead_is_five_sessions():
    assert sessions_until(WED + dt.timedelta(days=7), WED) == 5


# --------------------------------------------------------- the window


def test_a_release_far_off_does_not_suspend():
    win = earnings_window(WED + dt.timedelta(days=60), None, HISTORY, today=WED)
    assert win.state == CLEAR
    assert not win.suspended


def test_a_release_three_sessions_out_suspends():
    """The rule as specified: no new signal from 2-3 trading days before.

    Monday, not Saturday: Wednesday plus three *days* is a weekend, and only
    two sessions away. Which is the whole reason this counts sessions.
    """
    monday = dt.date(2026, 8, 24)
    win = earnings_window(monday, None, HISTORY, today=WED)
    assert win.state == BEFORE
    assert win.sessions == 3
    assert win.suspended


def test_a_weekend_release_is_measured_in_sessions_not_days():
    saturday = WED + dt.timedelta(days=3)
    assert earnings_window(saturday, None, HISTORY, today=WED).sessions == 2


def test_the_session_before_suspends():
    win = earnings_window(WED + dt.timedelta(days=1), None, HISTORY, today=WED)
    assert (win.state, win.sessions) == (BEFORE, 1)


def test_reporting_day_itself_suspends():
    win = earnings_window(WED, None, HISTORY, today=WED)
    assert (win.state, win.sessions) == (BEFORE, 0)


def test_four_sessions_out_is_still_clear():
    """The boundary has to be somewhere, and this is it."""
    tuesday = dt.date(2026, 8, 25)          # Thu, Fri, Mon, Tue
    assert sessions_until(tuesday, WED) == 4
    assert earnings_window(tuesday, None, HISTORY, today=WED).state == CLEAR


def test_the_window_width_is_tunable():
    far = WED + dt.timedelta(days=7)
    assert earnings_window(far, None, HISTORY, today=WED).state == CLEAR
    assert earnings_window(far, None, HISTORY, today=WED, blackout_days=5).state == BEFORE


# ------------------------------------------------------- after the release


def test_a_release_with_no_session_since_is_still_suspended():
    """"Until the first trading session after results" -- the gap has not
    happened yet, so neither has the information."""
    just_reported = HISTORY[-1]
    win = earnings_window(None, just_reported, HISTORY, today=WED)
    assert win.state == AFTER
    assert win.suspended


def test_one_session_after_the_release_clears_it():
    reported = HISTORY[-2]  # a session has closed since
    assert earnings_window(None, reported, HISTORY, today=WED).state == CLEAR


def test_a_next_date_that_has_slipped_into_the_past_is_treated_as_the_release():
    """The feed lags: `next` sometimes still points at a release that has
    already happened. That is the aftermath, not an imminent event.

    History stops on Monday here — a run that hasn't recorded today's bar yet —
    and the release was Tuesday, so no session has closed since.
    """
    lagging = weekdays(dt.date(2026, 8, 17), 30)      # ends Monday the 17th
    tuesday = dt.date(2026, 8, 18)
    assert earnings_window(tuesday, None, lagging, today=WED).state == AFTER


# ------------------------------------------------------- missing data


def test_no_dates_at_all_never_suspends():
    """Refusing to signal on every stock whose calendar we cannot see would
    silently disable the screener."""
    assert earnings_window(None, None, HISTORY, today=WED).state == CLEAR


def test_no_history_does_not_crash():
    win = earnings_window(WED + dt.timedelta(days=30), WED - dt.timedelta(days=1), [], today=WED)
    assert win.state == AFTER  # nothing has traded since, by definition


# ------------------------------------------------------- reading the feed


def test_epoch_seconds_decode_to_a_date():
    """What TradingView actually serves: 2026-10-29 12:00 UTC for AAPL."""
    assert to_date(1793275200) == dt.date(2026, 10, 29)


def test_an_iso_string_decodes_too():
    """What the database stores after a run."""
    assert to_date("2026-10-29") == dt.date(2026, 10, 29)


def test_a_full_timestamp_decodes_to_its_date():
    assert to_date("2026-10-29T20:00") == dt.date(2026, 10, 29)


def test_a_missing_date_is_none_not_an_error():
    assert to_date(None) is None
    assert to_date("") is None


def test_nonsense_is_none_rather_than_an_exception():
    """A feed change must not take the dashboard build down."""
    assert to_date("not a date") is None


def test_the_default_window_is_three_sessions():
    assert DEFAULT_BLACKOUT_DAYS == 3


# ------------------------------------------- what it means on the dashboard


@pytest.fixture()
def config(tmp_path):
    from screener.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(f"""
tickers:
  - {{symbol: PTON, tradingview: "NASDAQ:PTON", morningstar: xnas/pton, markets: [nasdaq]}}
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
notify:
  push_horizons: [1h, 4h, 1d, 1w]
dashboard:
  output: "{tmp_path / 't.html'}"
  chart_days: 90
  site_url: https://example.test/s
""")
    return load_config(path)


def _sessions_ahead(sessions: int) -> dt.date:
    """The date `sessions` trading days from today, skipping weekends.

    Not `today + timedelta(days=n)`: that is only the same thing from Monday to
    Thursday. Seeded from a Friday it lands on the weekend, which `sessions_until`
    correctly counts as zero sessions away -- so a test seeding "one day out" and
    asserting the card reads "Earnings tomorrow" failed every Friday and Saturday.
    """
    day = dt.date.today()
    for _ in range(sessions):
        day += dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
    return day


def _seed(store, reports_in_days: int | None):
    """A fresh, strong, deeply discounted buy — and results that many sessions away."""
    from screener.storage import RsiPoint, Signal, Valuation

    now = dt.datetime.now()
    stamps = [(now - dt.timedelta(hours=30 - i)).isoformat(timespec="minutes") for i in range(30)]
    for horizon in ("1h", "1d"):
        for stamp in stamps:
            label = stamp if horizon == "1h" else stamp[:10]
            store.upsert_rsi_point(RsiPoint("PTON", label, 5.45, 33.3, "test", horizon=horizon))
    store.record_signal(Signal(
        "PTON", stamps[0], stamps[10], stamps[-2], 5.45, 7.81,
        True, True, True, "now", horizon="1h", direction="buy",
    ))
    store.upsert_valuation(Valuation("PTON", "2026-08-10", 5.45, 7.81, "2026-08-10", "manual"))
    if reports_in_days is not None:
        release = _sessions_ahead(reports_in_days)
        store.upsert_earnings("PTON", release.isoformat(), None, None)


def test_a_signal_clear_of_earnings_is_a_strong_buy(config):
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=60)
        row = _collect(store, config, config.horizon("1h"))[0]
    assert row.state == "strong"
    assert row.deal_discount is not None
    assert not row.suspended


def test_the_same_signal_inside_the_window_is_suspended(config):
    """The regression this whole module exists for: identical setup, results
    two days out, and it stops being actionable."""
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=1)
        row = _collect(store, config, config.horizon("1h"))[0]
    assert row.suspended
    assert row.state == "suspended"
    assert row.deal_discount is None, "a suspended pick cannot lead the page"
    assert "buy signal suspended" in row.earnings_note


def test_the_card_says_why(config):
    from screener.dashboard import _collect, _card
    from screener.storage import Store

    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=1)
        row = _collect(store, config, config.horizon("1h"))[0]
    card = _card(row, config, config.horizon("1h"))
    assert "earnings-warning" in card
    assert "Earnings tomorrow" in card
    assert "Suspended" in card


def test_a_suspended_signal_does_not_reach_a_phone(config, monkeypatch):
    """The one contradiction the warning could not survive: the page calls it
    un-actionable and the phone rings anyway."""
    from screener.cli import _notify_new_strong_buys
    from screener.storage import Store

    sent = []
    monkeypatch.setattr("screener.cli.send_push", lambda t, m, u: sent.append(t) or True)
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: sent.append(m) or True)
    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=1)
        assert _notify_new_strong_buys(store, config) == 0
    assert sent == []


def test_the_same_signal_clear_of_earnings_does_reach_a_phone(config, monkeypatch):
    """And the other half, so the test above is about earnings and not about
    the fixture quietly failing to produce a strong buy at all."""
    from screener.cli import _notify_new_strong_buys
    from screener.storage import Store

    sent = []
    monkeypatch.setattr("screener.cli.send_push", lambda t, m, u: sent.append(t) or True)
    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=60)
        assert _notify_new_strong_buys(store, config) == 1
    assert len(sent) == 1


def test_a_sell_is_suspended_too(config):
    """Symmetrical: an overbought stock gapping down on results is the same
    risk from the other side."""
    from screener.dashboard import _collect
    from screener.storage import Store
    from screener.storage import RsiPoint, Signal

    now = dt.datetime.now()
    stamps = [(now - dt.timedelta(hours=30 - i)).isoformat(timespec="minutes") for i in range(30)]
    with Store(config.storage.database) as store:
        for stamp in stamps:
            # Below 70: a sell signal is live while RSI is on the *low* side of
            # the overbought line, which is where the second downward cross
            # leaves it.
            store.upsert_rsi_point(RsiPoint("PTON", stamp, 5.45, 65.0, "test", horizon="1h"))
            store.upsert_rsi_point(RsiPoint("PTON", stamp[:10], 5.45, 65.0, "test", horizon="1d"))
        store.record_signal(Signal(
            "PTON", stamps[0], stamps[10], stamps[-2], 5.45, 7.81,
            True, True, True, "now", horizon="1h", direction="sell",
        ))
        release = dt.date.today() + dt.timedelta(days=1)
        store.upsert_earnings("PTON", release.isoformat(), None, None)
        row = _collect(store, config, config.horizon("1h"))[0]

    assert row.suspended
    assert "sell signal suspended" in row.earnings_note


def test_a_ticker_with_no_earnings_date_is_never_suspended(config):
    """Most of the watchlist has a date; the ones that don't must still work."""
    from screener.dashboard import _collect
    from screener.storage import Store

    with Store(config.storage.database) as store:
        _seed(store, reports_in_days=None)
        row = _collect(store, config, config.horizon("1h"))[0]
    assert not row.suspended
    assert row.state == "strong"
