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
    assert store.already_notified("strong", "1h", "PTON", _stamp(2.5))


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


def test_the_title_carries_the_symbol_and_the_gap():
    assert issue_title("PLNT", 0.49, HOURLY) == (
        "🚀 PLNT strong buy on the 1 hour chart — 49% below fair value"
    )


def test_the_title_survives_an_unknown_gap():
    """A rocket earned on earnings growth alone has no discount to quote."""
    assert issue_title("AD", None, HOURLY) == "🚀 AD strong buy on the 1 hour chart"


def test_both_transports_fire_for_one_signal(config, store, monkeypatch):
    """They are independent: CI has the token and may have the webhook too."""
    posted, issues = [], []
    monkeypatch.setattr("screener.cli.send_webhook", lambda m: posted.append(m) or True)
    monkeypatch.setattr("screener.cli.send_github_issue",
                        lambda t, b: issues.append((t, b)) or True)
    assert _notify_new_strong_buys(store, config) == 1
    assert len(posted) == 1 and len(issues) == 1
    assert issues[0][0].startswith("🚀 PTON strong buy")
    assert "STRONG BUY 🚀 — PTON" in issues[0][1]
