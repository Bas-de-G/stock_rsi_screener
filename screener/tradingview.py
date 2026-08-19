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
# The same data, for many symbols at once. One POST carries the whole
# watchlist across every horizon: 637 symbols x 4 intervals answered in 0.75s
# against 612 sequential GETs for the same thing.
_SCAN_URL = "https://scanner.tradingview.com/global/scan"
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (screener; +https://github.com)"}
_TIMEOUT = 20

# How many symbols to put in one scan request. 637 has been measured working in
# a single call; this leaves room under whatever the real ceiling is, and the
# cost of an extra chunk is one round trip.
SCAN_CHUNK = 500


class MarketDataError(RuntimeError):
    """Raised when TradingView or Yahoo doesn't return usable data."""


class NoHistoryYet(MarketDataError):
    """The symbol resolved, but this interval has no RSI to give yet.

    A 14-period weekly RSI needs fifteen weekly bars, so a stock that listed
    two months ago has a perfectly good 1h and 1d RSI and a null 1W one. That
    is a fact about the listing's age, not a fetch failure, and it resolves
    itself as the bars accumulate.

    A subclass of MarketDataError so every existing `except MarketDataError`
    still catches it; callers that want to treat "too young" differently from
    "the request broke" catch this first.
    """


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    close: float
    rsi: float
    # YoY EPS growth, as a percentage (32.6 means +32.6%), and which window it
    # came from. Both None when TradingView has neither figure yet — happens
    # for a stock too new to have a trailing year, e.g. SanDisk right after
    # its 2025 spin-off.
    earnings_growth: float | None = None
    earnings_growth_period: str | None = None  # "ttm" | "fy" | None


# TradingView serves RSI as fixed-period fields, not a parameterised one:
# "RSI" is the 14-period value, "RSI7" the 7-period. Any other period would
# have to be computed locally, which would no longer be TradingView's number.
_RSI_FIELD_BY_PERIOD = {14: "RSI", 7: "RSI7"}
SUPPORTED_LIVE_PERIODS = tuple(sorted(_RSI_FIELD_BY_PERIOD))

# Same free, unauthenticated scanner endpoint as RSI — no Morningstar login
# needed for this factor. TTM (trailing twelve months) is preferred as the
# more current figure; FY (fiscal year) is the fallback for a stock too
# recently listed to have a full trailing year yet.
_EPS_GROWTH_TTM_FIELD = "earnings_per_share_diluted_yoy_growth_ttm"
_EPS_GROWTH_FY_FIELD = "earnings_per_share_diluted_yoy_growth_fy"

# When the company next reports, and when it last did -- epoch seconds, and
# present for US, European and Hong Kong listings alike. Fundamentals fields
# again, so they ride along in the same batch request as RSI at no extra cost.
# The timestamp carries the time of day too, which is what distinguishes a
# release before the open (06:45Z) from one after the close (20:00Z).
EARNINGS_NEXT_FIELD = "earnings_release_next_date"
EARNINGS_LAST_FIELD = "earnings_release_date"
EARNINGS_FIELDS = (EARNINGS_NEXT_FIELD, EARNINGS_LAST_FIELD)


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

    Also picks up YoY EPS growth in the same request — it's a fundamentals
    field, not a price-interval one, so it rides along at no extra cost.

    tv_symbol looks like "NASDAQ:NVDA" (exchange:ticker), matching what's in
    config.yaml and what appears in the TradingView URL bar.
    """
    rsi_field = rsi_field_name(period, interval)
    close_field = _close_field_name(interval)
    params = {
        "symbol": tv_symbol,
        "fields": f"{rsi_field},{close_field},{_EPS_GROWTH_TTM_FIELD},{_EPS_GROWTH_FY_FIELD}",
        "no_status": "true",
    }
    url = f"{_SCANNER_URL}?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"TradingView request failed for {tv_symbol}: {exc}") from exc

    return decode_quote(tv_symbol, data, period=period, interval=interval)


def quote_fields(intervals, period: int = 14) -> list[str]:
    """The scanner columns needed to build a `LiveQuote` for each interval.

    Deduplicated but order-preserving: the daily bar's RSI field is bare `RSI`
    whichever way it is asked for, so passing ("1D", "D") must not ask for the
    same column twice.
    """
    fields: list[str] = []
    for interval in intervals:
        for name in (rsi_field_name(period, interval), _close_field_name(interval)):
            if name not in fields:
                fields.append(name)
    # Fundamentals, not price-interval fields, so one copy covers every horizon.
    fields += [_EPS_GROWTH_TTM_FIELD, _EPS_GROWTH_FY_FIELD]
    return fields


def decode_quote(tv_symbol: str, data: dict, period: int = 14, interval: str = "1D") -> LiveQuote:
    """Turn one symbol's scanner fields into a `LiveQuote` for one interval.

    Shared by the single-symbol and batch paths so there is exactly one place
    that decides what a null means -- which matters, because that distinction
    (too young vs broken) is what keeps one recent listing from failing a whole
    scheduled run.
    """
    rsi_field = rsi_field_name(period, interval)
    close_field = _close_field_name(interval)

    if data.get(rsi_field) is None or data.get(close_field) is None:
        # A price with no RSI means the symbol is real and simply too young for
        # this interval's lookback -- distinguished from a broken response so
        # one recently listed ticker can't fail an otherwise healthy run.
        if data.get(close_field) is not None:
            raise NoHistoryYet(
                f"{tv_symbol} has no {rsi_field} yet — not enough {interval} bars "
                f"for a {period}-period RSI (close is {data[close_field]})"
            )
        raise MarketDataError(
            f"TradingView returned no {rsi_field}/{close_field} for {tv_symbol}: {data}"
        )

    growth, growth_period = _pick_earnings_growth(data)
    return LiveQuote(
        symbol=tv_symbol,
        close=float(data[close_field]),
        rsi=float(data[rsi_field]),
        earnings_growth=growth,
        earnings_growth_period=growth_period,
    )


def fetch_live_batch(
    tv_symbols, intervals, period: int = 14, extra_fields=()
) -> dict[str, dict]:
    """Fetch every symbol's readings for every interval, in as few requests as possible.

    Returns `{tv_symbol: {field: value}}` -- the raw scanner row, not a
    `LiveQuote`, because one row serves all four horizons and later work wants
    other columns out of the same request (earnings dates, fundamentals). Pass
    a row to `decode_quote` to read one horizon out of it.

    A symbol the scanner has no row for is simply absent from the result. That
    is deliberate: the caller reports it per ticker, the same way a null field
    is reported, so one delisted or mistyped symbol cannot fail the others.

    Raises `MarketDataError` only when a whole request fails -- that is a real
    outage, and the run should go red for it.
    """
    symbols = list(dict.fromkeys(tv_symbols))  # de-duplicated, order preserved
    columns = quote_fields(intervals, period) + [f for f in extra_fields]
    out: dict[str, dict] = {}

    for start in range(0, len(symbols), SCAN_CHUNK):
        chunk = symbols[start:start + SCAN_CHUNK]
        try:
            resp = requests.post(
                _SCAN_URL,
                json={"symbols": {"tickers": chunk}, "columns": columns},
                headers={**_HEADERS, "Content-Type": "application/json"},
                timeout=_TIMEOUT * 3,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise MarketDataError(
                f"TradingView scan failed for {len(chunk)} symbols: {exc}"
            ) from exc

        for row in payload.get("data", []) or []:
            try:
                out[row["s"]] = dict(zip(columns, row["d"]))
            except (KeyError, TypeError) as exc:
                raise MarketDataError(
                    f"Unexpected TradingView scan row: {row!r} ({exc})"
                ) from exc
    return out


def _pick_earnings_growth(data: dict) -> tuple[float | None, str | None]:
    """TTM preferred; FY is the fallback for a stock too new to have one."""
    ttm = data.get(_EPS_GROWTH_TTM_FIELD)
    if ttm is not None:
        return float(ttm), "ttm"
    fy = data.get(_EPS_GROWTH_FY_FIELD)
    if fy is not None:
        return float(fy), "fy"
    return None, None


# What the screener needs to judge a candidate for the watchlist. `typespecs`
# and `indexes` are the two that do the real work: the first separates a
# company's ordinary shares from its preferred and depositary lines, the second
# is actual index membership rather than a market-cap guess at it.
DISCOVERY_COLUMNS = (
    "name", "description", "close", "currency", "market_cap_basic",
    "average_volume_10d_calc", "type", "typespecs", "indexes", "sector",
)


def discover_market(
    market: str,
    min_market_cap: float = 0.0,
    min_volume: float = 0.0,
    limit: int = 1000,
) -> list[dict]:
    """List the tradeable stocks on one market, largest first.

    `market` is the scanner's own regional path -- "america", "netherlands",
    "germany" and so on. Returns one dict per listing, keyed by
    DISCOVERY_COLUMNS plus "symbol" (the exchange-prefixed TradingView name).

    Size and liquidity are filtered server-side because they cut the response
    hard: the American market alone has thousands of listings, and only a few
    hundred are things anyone would trade.
    """
    filters = [{"left": "type", "operation": "equal", "right": "stock"}]
    if min_market_cap:
        filters.append(
            {"left": "market_cap_basic", "operation": "egreater", "right": min_market_cap}
        )
    if min_volume:
        filters.append(
            {"left": "average_volume_10d_calc", "operation": "egreater", "right": min_volume}
        )
    try:
        resp = requests.post(
            f"https://scanner.tradingview.com/{market}/scan",
            json={
                "filter": filters,
                "columns": list(DISCOVERY_COLUMNS),
                "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
                "range": [0, limit],
            },
            headers={**_HEADERS, "Content-Type": "application/json"},
            timeout=_TIMEOUT * 3,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MarketDataError(f"TradingView discovery failed for {market}: {exc}") from exc

    out = []
    for row in payload.get("data", []) or []:
        record = dict(zip(DISCOVERY_COLUMNS, row.get("d", [])))
        record["symbol"] = row.get("s", "")
        out.append(record)
    return out


def fetch_daily_closes(
    yahoo_symbol: str, range_: str = "1y", interval: str = "1d"
) -> list[tuple[str, float]]:
    """Fetch (ISO timestamp, close) pairs for backfilling RSI history.

    yahoo_symbol is the bare ticker (e.g. "NVDA", "IBM") — Yahoo doesn't use
    TradingView's exchange prefix.

    `interval` is Yahoo's own code: 60m, 4h, 1d, 1wk. Intraday intervals return
    a timestamp per bar rather than a date, so the label carries the time too —
    otherwise every bar in a day would collapse onto one key.
    """
    url = _YAHOO_CHART_URL.format(symbol=yahoo_symbol)
    params = {"range": range_, "interval": interval}
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

    intraday = interval not in ("1d", "1wk", "1mo", "5d", "3mo")
    out: list[tuple[str, float]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        out.append((_epoch_to_label(ts, intraday), float(close)))
    return out


def _epoch_to_label(epoch_seconds: int, intraday: bool = False) -> str:
    """ISO date, plus the time when bars are finer than daily.

    Both forms sort lexicographically in the same order they occur in, which is
    what `rsi_series`'s ORDER BY relies on to return a series in time order.
    """
    # Timezone-aware rather than utcfromtimestamp, which is deprecated in 3.12.
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(epoch_seconds, _dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M") if intraday else moment.date().isoformat()


# Kept under the old name too: it is part of what the tests import.
def _epoch_to_date(epoch_seconds: int) -> str:
    return _epoch_to_label(epoch_seconds, intraday=False)
