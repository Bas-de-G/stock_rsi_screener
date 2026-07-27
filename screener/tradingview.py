"""RSI from TradingView, and historical closes from Yahoo Finance for backfill.

Both endpoints are public JSON APIs — no login, no API key, no scraping of
rendered HTML. This is deliberate: the alternative (driving a browser to
https://www.tradingview.com/symbols/.../technicals/ and parsing the page)
is far more fragile and would need a logged-in session for no benefit, since
TradingView's own RSI figure is served unauthenticated from this endpoint.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import requests

_SCANNER_URL = "https://scanner.tradingview.com/symbol"
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (screener; +https://github.com)"}
_TIMEOUT = 20


class MarketDataError(RuntimeError):
    """Raised when TradingView or Yahoo doesn't return usable data."""


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    close: float
    rsi: float


# TradingView serves RSI as fixed-period fields, not a parameterised one:
# "RSI" is the 14-period value, "RSI7" the 7-period. Any other period would
# have to be computed locally, which would no longer be TradingView's number.
_RSI_FIELD_BY_PERIOD = {14: "RSI", 7: "RSI7"}
SUPPORTED_LIVE_PERIODS = tuple(sorted(_RSI_FIELD_BY_PERIOD))


def rsi_field_name(period: int, interval: str) -> str:
    """Build the scanner field name, e.g. RSI, RSI7, RSI|1W, RSI7|60."""
    try:
        base = _RSI_FIELD_BY_PERIOD[period]
    except KeyError:
        raise MarketDataError(
            f"TradingView only serves RSI periods {SUPPORTED_LIVE_PERIODS}, not {period}."
        ) from None
    # The daily bar is the scanner's default and takes no suffix.
    return base if interval in ("1D", "", "D") else f"{base}|{interval}"


def _close_field_name(interval: str) -> str:
    return "close" if interval in ("1D", "", "D") else f"close|{interval}"


def fetch_live_rsi(tv_symbol: str, period: int = 14, interval: str = "1D") -> LiveQuote:
    """Fetch the current close + RSI that TradingView itself shows for `tv_symbol`.

    tv_symbol looks like "NASDAQ:NVDA" (exchange:ticker), matching what's in
    config.yaml and what appears in the TradingView URL bar.
    """
    rsi_field = rsi_field_name(period, interval)
    close_field = _close_field_name(interval)
    params = {
        "symbol": tv_symbol,
        "fields": f"{rsi_field},{close_field}",
        "no_status": "true",
    }
    url = f"{_SCANNER_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"TradingView request failed for {tv_symbol}: {exc}") from exc

    if data.get(rsi_field) is None or data.get(close_field) is None:
        raise MarketDataError(
            f"TradingView returned no {rsi_field}/{close_field} for {tv_symbol}: {data}"
        )

    return LiveQuote(
        symbol=tv_symbol,
        close=float(data[close_field]),
        rsi=float(data[rsi_field]),
    )


def fetch_daily_closes(yahoo_symbol: str, range_: str = "1y") -> list[tuple[str, float]]:
    """Fetch (ISO date, close) pairs for backfilling RSI history.

    yahoo_symbol is the bare ticker (e.g. "NVDA", "IBM") — Yahoo doesn't use
    TradingView's exchange prefix.
    """
    url = _YAHOO_CHART_URL.format(symbol=yahoo_symbol)
    params = {"range": range_, "interval": "1d"}
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"Yahoo history request failed for {yahoo_symbol}: {exc}") from exc

    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MarketDataError(f"Unexpected Yahoo response shape for {yahoo_symbol}: {payload}") from exc

    out: list[tuple[str, float]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date = _epoch_to_date(ts)
        out.append((date, float(close)))
    return out


def _epoch_to_date(epoch_seconds: int) -> str:
    # Timezone-aware rather than utcfromtimestamp, which is deprecated in 3.12.
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch_seconds, _dt.timezone.utc).date().isoformat()
