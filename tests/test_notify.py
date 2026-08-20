"""Tests for webhook notifications.

The screener records thousands of patterns and every one of them "fires",
because `fire_without_valuation` is set. Announcing all of them is what the
webhook did before this: 5 to 20 messages a run, 251 the day new tickers are
backfilled. So the notification is narrowed to the one thing worth a push --
a newly fired strong buy -- and sent once per pattern. Offline throughout.
"""

from __future__ import annotations

import datetime as dt

import pytest

from screener import notify
from screener.cli import _notify_new_strong_buys
from screener.config import DEFAULT_HORIZONS, load_config
from screener.notified import COOLDOWN, KEEP_FOR, Ledger, key_for
from screener.notify import format_signal, format_strong_buy, issue_title, send_webhook
from screener.storage import RsiPoint, Signal, Store, Valuation

NOW = dt.datetime.now()
HOURLY = next(h for h in DEFAULT_HORIZONS if h.key == "1h")


def _stamp(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="minutes")


@pytest.fixture()
def config(tmp_path):
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
  notifications: "{tmp_path / 'notified.json'}"
dashboard:
  output: "{tmp_path / 't.html'}"
  chart_days: 90
  site_url: https://example.test/screener
""")
    return load_config(path)


@pytest.fixture()
def store(config):
    with Store(config.storage.database) as s:
        # A fresh buy, well below fair value, with the valuation confirming:
        # a rocket that just fired.
        for i in range(30):
            s.upsert_rsi_point(
                RsiPoint("PTON", _stamp(30 - i), 5.45, 33.3, "test", horizon="1h")
            )
        s.record_signal(Signal(
            "PTON", _stamp(6), _stamp(4), _stamp(2.5), 5.45, 7.81,
            True, True, True, "now", horizon="1h", direction="buy",
        ))
        s.upsert_valuation(Valuation("PTON", "2026-08-10", 5.45, 7.81, "2026-08-10", "manual"))
        yield s


# ------------------------------------------------------------ the message


def test_the_message_carries_what_a_phone_needs():
    msg = format_strong_buy("PTON", 0.434, 5.45, 7.81, "USD", HOURLY, 30.0, "https://x.test/1h.html")
    assert "STRONG BUY 🚀 — PTON" in msg
    assert "43% below fair value" in msg
    assert "5.45" in msg and "7.81" in msg
    assert "https://x.test/1h.html" in msg
    assert "10x suggested" in msg


def test_a_non_dollar_price_names_its_currency():
    msg = format_strong_buy("ASML", 0.2, 100.0, 120.0, "EUR", HOURLY, 30.0, "u")
    assert "EUR" in msg


def test_a_sell_is_announced_as_a_sell():
    """This said BUY SIGNAL for everything, which was harmless while only buys
    existed and became a lie once sells were added -- and sells outnumber buys
    on most runs."""
    sig = Signal("ASM", "a", "b", "c", None, None, False, False, True, "now",
                 horizon="4h", direction="sell")
    assert format_signal(sig, "rule").startswith("SELL SIGNAL — ASM")


def test_a_buy_is_still_announced_as_a_buy():
    sig = Signal("ORCL", "a", "b", "c", None, None, False, False, True, "now",
                 horizon="1d", direction="buy")
    assert format_signal(sig, "rule").startswith("BUY SIGNAL — ORCL")


# ------------------------------------------------------------ the transport


def test_no_webhook_configured_is_not_an_error(monkeypatch):
    monkeypatch.delenv("SCREENER_WEBHOOK_URL", raising=False)
    assert send_webhook("anything") is False


def test_a_failing_webhook_does_not_raise(monkeypatch, capsys):
    """A dead webhook must never take the publish step down with it."""
    import requests

    monkeypatch.setenv("SCREENER_WEBHOOK_URL", "https://hooks.test/x")

    def _boom(*a, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(notify.requests, "post", _boom)
    assert send_webhook("hello") is False
    assert "webhook failed" in capsys.readouterr().out


def test_the_payload_suits_slack_and_discord(monkeypatch):
    """Slack reads `text`, Discord reads `content`. Sending both means one URL
    field works for either service with no configuration."""
    sent = {}

    class _Resp:
        def raise_for_status(self): return None

    monkeypatch.setenv("SCREENER_WEBHOOK_URL", "https://hooks.test/x")
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, json, timeout: sent.update(json) or _Resp())
    assert send_webhook("hello") is True
    assert sent == {"text": "hello", "content": "hello"}


# ------------------------------------------------------------ the phone push


def test_no_topic_configured_is_not_an_error(monkeypatch):
    monkeypatch.delenv("SCREENER_NTFY_TOPIC", raising=False)
    assert notify.send_push("t", "m") is False


def test_the_push_goes_to_the_topic(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): return None

    monkeypatch.setenv("SCREENER_NTFY_TOPIC", "sekrit-topic-9f3")
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, data, headers, timeout:
                        captured.update(url=url, data=data, headers=headers) or _Resp())

    assert notify.send_push("PTON strong buy", "the message", "https://x.test/1h.html") is True
    assert captured["url"] == "https://ntfy.sh/sekrit-topic-9f3"
    assert captured["data"] == b"the message"
    assert captured["headers"]["Title"] == "PTON strong buy"
    assert captured["headers"]["Click"] == "https://x.test/1h.html"


def test_the_title_loses_what_a_header_cannot_carry(monkeypatch):
    """`issue_title` opens with a rocket, and HTTP headers are latin-1 on the
    wire -- sending it raw raises inside requests and kills the notification."""
    captured = {}

    class _Resp:
        def raise_for_status(self): return None

    monkeypatch.setenv("SCREENER_NTFY_TOPIC", "t")
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, data, headers, timeout:
                        captured.update(headers=headers) or _Resp())

    notify.send_push(issue_title("PTON", 0.43, HOURLY), "body")
    title = captured["headers"]["Title"]
    assert title == "PTON strong buy on the 1 hour chart - 43% below fair value"
    title.encode("latin-1")  # would raise if anything unsendable survived


def test_a_title_of_nothing_but_emoji_still_says_something(monkeypatch):
    class _Resp:
        def raise_for_status(self): return None

    captured = {}
    monkeypatch.setenv("SCREENER_NTFY_TOPIC", "t")
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, data, headers, timeout:
                        captured.update(headers=headers) or _Resp())
    notify.send_push("🚀🚀", "body")
    assert captured["headers"]["Title"] == "Strong buy"


def test_a_failing_push_does_not_raise(monkeypatch, capsys):
    """A dead phone channel must never take the publish step down with it."""
    import requests

    monkeypatch.setenv("SCREENER_NTFY_TOPIC", "t")

    def _boom(*a, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(notify.requests, "post", _boom)
    assert notify.send_push("t", "m") is False
    assert "push failed" in capsys.readouterr().out


# ---------------------------------------------------- what actually gets sent


def test_a_new_strong_buy_is_announced(config, store, monkeypatch):
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert "STRONG BUY 🚀 — PTON" in posted[0]
    assert "https://example.test/screener/1h.html" in posted[0]


def test_the_same_strong_buy_is_never_announced_twice(config, store, monkeypatch):
    """A rocket sits on the page for as long as its pattern is fresh, and runs
    land every three hours. Without the dedupe the same name arrives four or
    five times."""
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert _notify_new_strong_buys(store, config) == 0
    assert _notify_new_strong_buys(store, config) == 0
    assert len(posted) == 1


def test_nothing_is_sent_when_there_is_no_strong_buy(config, tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    with Store(tmp_path / "empty.db") as empty:
        assert _notify_new_strong_buys(empty, config) == 0
    assert posted == []


def test_a_sell_never_triggers_a_notification(config, tmp_path, monkeypatch):
    """The rocket is a buy verdict; a sell is graded separately."""
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    with Store(tmp_path / "sells.db") as s:
        for i in range(30):
            s.upsert_rsi_point(
                RsiPoint("PTON", _stamp(30 - i), 5.45, 65.0, "test", horizon="1h")
            )
        s.record_signal(Signal(
            "PTON", _stamp(6), _stamp(4), _stamp(2.5), 5.45, 7.81,
            True, True, True, "now", horizon="1h", direction="sell",
        ))
        assert _notify_new_strong_buys(s, config) == 0
    assert posted == []


def test_the_record_survives_a_send_that_failed(config, store, monkeypatch):
    """A webhook that is down must not queue the signal forever. The dashboard
    still shows it; re-sending on every run for days would be worse."""
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: False)
    assert _notify_new_strong_buys(store, config) == 1
    assert Ledger(config.storage.notifications).seen(
        key_for("strong", "1h", "PTON", _stamp(2.5))
    )


def test_the_record_outlives_the_database(config, store, monkeypatch):
    """The bug this file's ledger exists for.

    CI only commits the 50 MB database on the last run of the day, so every
    intraday run checks out the copy from last night's close. When the record
    lived in a table inside it, a strong buy announced at 14:00 was unknown
    again at 14:30 -- and the friend got the same email four or five times over
    an afternoon.

    Simulated here by throwing the database away entirely between runs, which
    is strictly worse than what CI does.
    """
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    assert _notify_new_strong_buys(store, config) == 1

    rows = list(store.all_signals())
    points = [
        RsiPoint("PTON", _stamp(30 - i), 5.45, 33.3, "test", horizon="1h")
        for i in range(30)
    ]
    config.storage.database.unlink()
    with Store(config.storage.database) as reverted:
        for point in points:
            reverted.upsert_rsi_point(point)
        for sig in rows:
            reverted.record_signal(sig)
        reverted.upsert_valuation(
            Valuation("PTON", "2026-08-10", 5.45, 7.81, "2026-08-10", "manual")
        )
        assert _notify_new_strong_buys(reverted, config) == 0
    assert len(posted) == 1


# ------------------------------------------------------------- the cooldown


def _another_pattern(store, hours_ago: float) -> None:
    """A second, genuinely distinct double-cross completing `hours_ago`.

    This is what the half-hourly intraday runs really produce: the bars are
    stamped with the minute the run happened, so a pattern that completes at
    15:29 and one that completes at 15:59 are different patterns by every test
    the per-pattern ledger can apply.
    """
    store.record_signal(Signal(
        "PTON", _stamp(hours_ago + 3), _stamp(hours_ago + 2), _stamp(hours_ago),
        5.45, 7.81, True, True, True, "now", horizon="1h", direction="buy",
    ))


def test_a_new_pattern_hours_later_waits_its_turn(config, store, monkeypatch):
    """The gap the per-pattern ledger left open. Both patterns are real and
    neither has been announced -- but they are the same idea about the same
    name, hours apart."""
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    assert _notify_new_strong_buys(store, config) == 1
    _another_pattern(store, 1.0)
    assert _notify_new_strong_buys(store, config) == 0
    assert len(posted) == 1


def test_the_anet_afternoon_collapses_to_one_alert(config, store, monkeypatch):
    """ANET announced itself eleven times on the 1h chart between 15:29 and
    21:31 on 2026-08-19, and every one of those alerts passed the per-pattern
    check honestly. Thirty alerts that day were about a dozen opportunities."""
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    for minutes_in in (0, 30, 72, 117, 142, 185, 235, 261, 297, 326, 362):
        _another_pattern(store, 6.0 - minutes_in / 60)
        _notify_new_strong_buys(store, config)
    assert len(posted) == 1


def test_the_cooldown_lifts_before_the_next_session(config, store, monkeypatch):
    """Quiet for a session, not quiet forever.

    The window has to clear the overnight gap. DECK, GRAB, HEIA and JOBY each
    came back the next morning 18 to 21 hours after the previous alert; a
    twenty-four hour window would have silenced all four.
    """
    assert COOLDOWN < dt.timedelta(hours=18)
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    Ledger(config.storage.notifications).record(
        key_for("strong", "1h", "PTON", "yesterday"),
        now=NOW - COOLDOWN - dt.timedelta(hours=1),
    )
    assert _notify_new_strong_buys(store, config) == 1
    assert len(posted) == 1


def test_a_held_pattern_does_not_restart_the_clock(config, store, monkeypatch):
    """The trap in the obvious implementation.

    If a suppressed pattern were recorded, every half-hourly run would push the
    window forward by half an hour and the name would never be heard from
    again. Only what was actually sent goes in the ledger.
    """
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: True)
    assert _notify_new_strong_buys(store, config) == 1
    _another_pattern(store, 1.0)
    assert _notify_new_strong_buys(store, config) == 0
    # Only the announced pattern is on file. The held one left no trace, so the
    # 24 hours run from when the friend was actually told.
    assert list(Ledger(config.storage.notifications)._sent) == [
        key_for("strong", "1h", "PTON", _stamp(2.5))
    ]


def test_the_cooldown_says_why_it_stayed_quiet(config, store, monkeypatch, capsys):
    """A silent suppression is indistinguishable from a broken pipeline -- which
    is exactly the fright that started this."""
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: True)
    _notify_new_strong_buys(store, config)
    capsys.readouterr()
    _another_pattern(store, 1.0)
    _notify_new_strong_buys(store, config)
    out = capsys.readouterr().out
    assert "PTON" in out and "holding off" in out


def test_one_quiet_timeframe_does_not_quiet_the_others(config, store, monkeypatch):
    """A daily cross on a name that fired on the hourly chart this morning is a
    different piece of news, and the cooldown is keyed to say so."""
    ledger = Ledger(config.storage.notifications)
    ledger.record(key_for("strong", "4h", "PTON", "recent"), now=NOW)
    posted = []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert len(posted) == 1


# ------------------------------------------------- the GitHub issue transport


def test_no_token_means_no_issue(monkeypatch):
    """The normal case on a laptop: print, don't post."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert notify.send_github_issue("t", "b") is False


def test_a_token_without_a_repository_is_not_enough(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert notify.send_github_issue("t", "b") is False


def test_the_issue_goes_to_the_right_repository(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self): return None

    def _post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Bas-de-G/stock_rsi_screener")
    monkeypatch.setattr(notify.requests, "post", _post)

    assert notify.send_github_issue("🚀 PLNT strong buy", "body text") is True
    assert captured["url"] == "https://api.github.com/repos/Bas-de-G/stock_rsi_screener/issues"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"] == {"title": "🚀 PLNT strong buy", "body": "body text"}


def test_a_failing_issue_does_not_raise(monkeypatch, capsys):
    """A GitHub hiccup must not take the publish step down with it."""
    import requests

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")

    def _boom(*a, **kw):
        raise requests.RequestException("503")

    monkeypatch.setattr(notify.requests, "post", _boom)
    assert notify.send_github_issue("t", "b") is False
    assert "issue failed" in capsys.readouterr().out


def test_a_second_run_finds_its_own_issue_and_stays_quiet(monkeypatch):
    """The belt to the ledger's braces: GitHub is asked whether it already has
    this exact signal. It is the authoritative record -- the issue *is* the
    thing being deduplicated -- so this holds even if the ledger is lost."""
    posted = []

    class _Resp:
        def __init__(self, payload=None): self.payload = payload or []
        def raise_for_status(self): return None
        def json(self): return self.payload

    filed = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(notify.requests, "get", lambda *a, **kw: _Resp(list(filed)))
    monkeypatch.setattr(
        notify.requests, "post",
        lambda url, headers, json, timeout: filed.append(json) or posted.append(json) or _Resp(),
    )

    key = "strong/1h/PTON/2026-08-13T14:00"
    assert notify.send_github_issue("🚀 PTON strong buy — 43% below", "body", key) is True
    assert notify.send_github_issue("🚀 PTON strong buy — 41% below", "body", key) is False
    assert len(posted) == 1


def test_the_duplicate_check_ignores_the_moving_discount(monkeypatch):
    """Matching on the title would not work: it quotes a percentage that moves
    with the price, so the same signal an hour later reads differently."""
    marker = notify.issue_marker("strong/1h/PTON/2026-08-13T14:00")
    assert marker not in "🚀 PTON strong buy on the 1 hour chart — 43% below fair value"
    assert marker in f"a message\n\n{marker}"


def test_an_unrelated_issue_does_not_suppress_a_signal(monkeypatch):
    """Someone filing a bug report must not silence the screener."""
    class _Resp:
        def __init__(self, payload=None): self.payload = payload or []
        def raise_for_status(self): return None
        def json(self): return self.payload

    posted = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(notify.requests, "get", lambda *a, **kw: _Resp(
        [{"body": "the 1h page looks wrong"}, {"body": None}]
    ))
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, headers, json, timeout: posted.append(json) or _Resp())
    assert notify.send_github_issue("t", "b", "strong/1h/PTON/x") is True
    assert len(posted) == 1


def test_an_unreachable_api_still_sends(monkeypatch, capsys):
    """When the duplicate check cannot run, err towards telling him. A repeat
    email is an annoyance; a missed strong buy defeats the whole point."""
    import requests as _requests

    class _Resp:
        def raise_for_status(self): return None

    posted = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(notify.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(
        _requests.RequestException("503")
    ))
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, headers, json, timeout: posted.append(json) or _Resp())
    assert notify.send_github_issue("t", "b", "strong/1h/PTON/x") is True
    assert len(posted) == 1
    assert "filing anyway" in capsys.readouterr().out


def test_the_marker_rides_along_in_the_body(monkeypatch):
    """Hidden in an HTML comment, so it shows in neither the issue nor the
    email -- but the next run can read it back."""
    captured = {}

    class _Resp:
        def __init__(self, payload=None): self.payload = payload or []
        def raise_for_status(self): return None
        def json(self): return self.payload

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(notify.requests, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, headers, json, timeout: captured.update(json) or _Resp())
    notify.send_github_issue("t", "the message", "strong/1h/PTON/x")
    assert captured["body"].startswith("the message")
    assert "<!-- screener-signal: strong/1h/PTON/x -->" in captured["body"]


def test_a_closed_issue_still_counts_as_filed(monkeypatch):
    """He reads the email and closes the issue; that is not an invitation to
    send it again. The query asks for state=all for exactly this reason."""
    captured = {}

    class _Resp:
        def raise_for_status(self): return None
        def json(self): return [{"body": notify.issue_marker("strong/1h/PTON/x")}]

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setattr(notify.requests, "get",
                        lambda url, headers, params, timeout: captured.update(params) or _Resp())
    assert notify.send_github_issue("t", "b", "strong/1h/PTON/x") is False
    assert captured["state"] == "all"


# ------------------------------------------------------------------ the ledger


def test_the_ledger_survives_a_restart(tmp_path):
    path = tmp_path / "notified.json"
    Ledger(path).record(key_for("strong", "1h", "PTON", "2026-08-13T14:00"))
    assert Ledger(path).seen("strong/1h/PTON/2026-08-13T14:00")
    assert not Ledger(path).seen("strong/1h/PTON/2026-08-13T15:00")


def test_the_ledger_is_readable_json(tmp_path):
    """It gets committed and shows up in diffs, so it has to be legible to a
    person wondering why a notification did or didn't go out."""
    import json

    path = tmp_path / "notified.json"
    Ledger(path).record("strong/1h/PTON/2026-08-13T14:00")
    written = json.loads(path.read_text())
    assert list(written["sent"]) == ["strong/1h/PTON/2026-08-13T14:00"]


def test_recording_twice_keeps_the_first_time(tmp_path):
    path = tmp_path / "notified.json"
    ledger = Ledger(path)
    first = dt.datetime(2026, 8, 13, 14, 0)
    ledger.record("k", now=first)
    ledger.record("k", now=first + dt.timedelta(hours=3))
    assert path.read_text().count('"k"') == 1
    assert first.isoformat(timespec="seconds") in path.read_text()


def test_old_entries_are_pruned(tmp_path):
    """Otherwise the file grows a line per notification forever."""
    path = tmp_path / "notified.json"
    ledger = Ledger(path)
    ledger.record("ancient", now=NOW - KEEP_FOR - dt.timedelta(days=1))
    ledger.record("recent", now=NOW)
    reloaded = Ledger(path)
    assert reloaded.seen("recent")
    assert not reloaded.seen("ancient")


def test_the_ledger_finds_the_last_time_a_name_was_heard(tmp_path):
    """Across patterns, which is the whole point -- `seen` deliberately cannot
    answer this."""
    path = tmp_path / "notified.json"
    ledger = Ledger(path)
    ledger.record(key_for("strong", "1h", "PTON", "morning"), now=NOW - dt.timedelta(hours=5))
    ledger.record(key_for("strong", "1h", "PTON", "midday"), now=NOW - dt.timedelta(hours=2))
    ledger.record(key_for("strong", "1h", "ANET", "midday"), now=NOW)
    last = Ledger(path).last_sent("strong", "1h", "PTON")
    assert last == (NOW - dt.timedelta(hours=2)).replace(microsecond=0)


def test_a_name_never_heard_has_no_last_time(tmp_path):
    ledger = Ledger(tmp_path / "notified.json")
    assert ledger.last_sent("strong", "1h", "PTON") is None


def test_a_prefix_does_not_count_as_the_symbol(tmp_path):
    """PT and PTON share three characters; nothing about PTON should mute PT."""
    path = tmp_path / "notified.json"
    Ledger(path).record(key_for("strong", "1h", "PTON", "midday"), now=NOW)
    assert Ledger(path).last_sent("strong", "1h", "PT") is None


def test_an_unreadable_stamp_does_not_mute_a_name(tmp_path):
    """A record that cannot be placed in time cannot justify silence. One extra
    alert is the safe failure here; a missed one is not."""
    path = tmp_path / "notified.json"
    path.write_text('{"sent": {"strong/1h/PTON/midday": "sometime tuesday"}}\n')
    assert Ledger(path).last_sent("strong", "1h", "PTON") is None


def test_a_corrupt_ledger_does_not_stop_the_run(tmp_path, capsys):
    """A truncated write must cost at most one duplicate email, never the
    dashboard build."""
    path = tmp_path / "notified.json"
    path.write_text("{not json")
    ledger = Ledger(path)
    assert not ledger.seen("anything")
    assert "unreadable" in capsys.readouterr().out
    ledger.record("k")
    assert Ledger(path).seen("k")


def test_the_title_carries_the_symbol_and_the_gap():
    assert issue_title("PLNT", 0.49, HOURLY) == (
        "🚀 PLNT strong buy on the 1 hour chart — 49% below fair value"
    )


def test_the_title_survives_an_unknown_gap():
    """A rocket earned on earnings growth alone has no discount to quote."""
    assert issue_title("AD", None, HOURLY) == "🚀 AD strong buy on the 1 hour chart"


def test_every_transport_fires_for_one_signal(config, store, monkeypatch):
    """They are independent: CI has the token and may have the topic and the
    webhook too. One signal, one message down each channel it has."""
    posted, issues, pushes = [], [], []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    monkeypatch.setattr("screener.cli.send_github_issue",
                        lambda t, b, k: issues.append((t, b, k)) or True)
    monkeypatch.setattr("screener.cli.send_push",
                        lambda t, m, u: pushes.append((t, m, u)) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert len(posted) == 1 and len(issues) == 1 and len(pushes) == 1
    assert issues[0][0].startswith("🚀 PTON strong buy")
    assert "STRONG BUY 🚀 — PTON" in issues[0][1]
    # The same key the ledger uses, so both defences dedupe on one identity.
    assert issues[0][2] == key_for("strong", "1h", "PTON", _stamp(2.5))
    # The push carries the same title and taps through to the right page.
    assert pushes[0][0] == issues[0][0]
    assert pushes[0][2] == "https://example.test/screener/1h.html"


def test_the_phone_inherits_the_no_duplicate_policy(config, store, monkeypatch):
    """The dedupe sits above the transports, so adding one did not need its
    own copy of it -- a repeat run pushes nothing."""
    pushes = []
    monkeypatch.setattr("screener.cli.send_push",
                        lambda t, m, u: pushes.append(t) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert _notify_new_strong_buys(store, config) == 0
    assert len(pushes) == 1
