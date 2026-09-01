"""The crypto universe: which assets are worth watching, by market cap.

Only the *universe* comes from CoinGecko. Prices and RSI keep coming from
TradingView's scanner, the same batch request the equities ride on -- crypto
pairs resolve there already, so the signal half of this needed no new data
source at all. What CoinGecko is genuinely better at is saying which assets
exist and how big they are: TradingView's crypto scan returns one row per
exchange pair, eight separate Bitcoin lines across COINJAR, BITFINEX, BYBIT,
OKX and the rest, which is the cross-listing problem again. CoinGecko gives one
canonical row per asset.

Three kinds of asset are excluded, and the reason is the same each time: RSI
measures how far a price has moved from its own recent range, so an asset whose
price is *designed* not to move has no signal to give.

  - **Stablecoins.** USDT oscillates in the fourth decimal place. A 14-period
    RSI on that is a reading of rounding noise, and it will cross 30 and 70
    constantly. Six of the top twenty-five are stablecoins.
  - **Wrapped tokens.** WBTC is Bitcoin. Watching both is watching one asset
    twice and calling the agreement confirmation.
  - **Liquid-staking derivatives.** stETH tracks ETH the same way.

Membership of all three comes from CoinGecko's own category endpoints rather
than a hand-kept list or a "price is near a dollar" heuristic. A heuristic
would misfile any real asset that happens to trade near $1, and the hand-kept
list would be out of date by the next cycle.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .tradingview import MarketDataError

_API = "https://api.coingecko.com/api/v3"
_HEADERS = {"User-Agent": "Mozilla/5.0 (screener; +https://github.com)",
            "Accept": "application/json"}
_TIMEOUT = 30

# Assets whose price is pegged or derived from another asset's. See the module
# docstring: each is excluded because it has no independent price to read.
EXCLUDED_CATEGORIES = ("stablecoins", "wrapped-tokens", "liquid-staking-tokens")

# CoinGecko's free tier is rate-limited per minute and answers 429 rather than
# queueing. Three seconds was not enough -- a six-request proposal tripped the
# limit and the whole run died on the fourth call. Eight is slow enough to
# finish, and a proposal is a thing you run by hand a few times a year.
_PAUSE = 8.0


@dataclass(frozen=True)
class Asset:
    """One cryptocurrency, as CoinGecko ranks it."""

    rank: int
    coingecko_id: str
    symbol: str            # upper-case, e.g. BTC
    name: str
    market_cap: float
    price: float
    ath: float | None
    ath_change_pct: float | None   # negative: how far below the all-time high

    @property
    def tradingview(self) -> str:
        """The Binance USDT pair, which is where the depth is.

        Not a rule that holds for every asset -- some are quoted only against
        USD, or not on Binance at all -- so a proposal is only ever emitted
        after the symbol has been checked against the live scanner.
        """
        return f"BINANCE:{self.symbol}USDT"

    @property
    def yahoo(self) -> str:
        """Yahoo's crypto convention, used for backfill: BTC-USD.

        A guess, and one that has to be checked rather than trusted. Where the
        bare symbol collides with something else Yahoo already lists, it
        disambiguates with a numeric suffix -- Uniswap is `UNI7083-USD` and Sui
        is `SUI20947-USD`, while plain `UNI-USD` and `SUI-USD` return a
        malformed chart rather than an error. Same trap as Deutsche Telekom
        resolving to DTE Energy: the wrong answer arrives looking like a right
        one, so a proposal is only emitted after this symbol has fetched real
        bars.
        """
        return f"{self.symbol}-USD"


def _get(path: str, params: dict) -> list:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_API}/{path}?{query}"
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise MarketDataError(
                "CoinGecko rate-limited the request (429). The free tier allows "
                "a handful of calls a minute; wait a minute and re-run."
            ) from exc
        raise MarketDataError(f"CoinGecko returned {exc.code} for {path}") from exc
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        raise MarketDataError(f"CoinGecko request failed: {exc}") from exc


def excluded_ids(categories=EXCLUDED_CATEGORIES, per_page: int = 100) -> set[str]:
    """CoinGecko ids to leave out, read from CoinGecko's own categories.

    **Ids, not ticker symbols.** Symbols are not unique and the collision is
    not hypothetical: a token called "Mezo Wrapped BTC" carries the symbol
    `BTC`, so excluding by symbol removed *Bitcoin itself* -- rank 1 -- from a
    crypto watchlist, leaving a list that looked entirely reasonable until you
    noticed the most important asset was not on it. Ids are canonical:
    `bitcoin` and `mezo-wrapped-btc` cannot be confused.
    """
    out: set[str] = set()
    for index, category in enumerate(categories):
        if index:
            time.sleep(_PAUSE)
        rows = _get("coins/markets", {
            "vs_currency": "usd", "category": category,
            "per_page": per_page, "page": 1,
        })
        out.update(str(row.get("id", "")) for row in rows if row.get("id"))
    return out


def top_assets(limit: int = 50, exclude: set[str] | None = None) -> list[Asset]:
    """The largest assets by market cap, pegged and derived ones removed.

    `exclude` holds CoinGecko ids, not symbols -- see `excluded_ids`.

    `limit` counts assets *kept*, so asking for 20 gives 20 tradable ones
    rather than 14 plus six stablecoins.
    """
    if exclude is None:
        exclude = excluded_ids()
        time.sleep(_PAUSE)

    kept: list[Asset] = []
    page = 1
    # Over-fetch: roughly a quarter of the top of the table is excluded, and
    # asking for exactly `limit` would quietly return fewer.
    while len(kept) < limit and page <= 4:
        rows = _get("coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 100, "page": page, "sparkline": "false",
        })
        if not rows:
            break
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or str(row.get("id", "")) in exclude:
                continue
            if row.get("market_cap_rank") is None or not row.get("current_price"):
                continue
            kept.append(Asset(
                rank=int(row["market_cap_rank"]),
                coingecko_id=str(row.get("id", "")),
                symbol=symbol,
                name=str(row.get("name", symbol)),
                market_cap=float(row.get("market_cap") or 0.0),
                price=float(row["current_price"]),
                ath=float(row["ath"]) if row.get("ath") else None,
                ath_change_pct=(
                    float(row["ath_change_percentage"])
                    if row.get("ath_change_percentage") is not None else None
                ),
            ))
            if len(kept) >= limit:
                break
        page += 1
        if len(kept) < limit:
            time.sleep(_PAUSE)
    return kept


def config_line(asset: Asset) -> str:
    """The `config.yaml` line for one asset.

    No `morningstar:` field, which is the whole of "option 1" in one detail:
    there is no fair value for a cryptocurrency, the ticker is marked as
    unvalued, and `is_strong` refuses to award a rocket without a valuation. A
    crypto pattern is reported as a plain buy signal and never as a strong one.
    """
    return (
        f"  - {{symbol: {asset.symbol}, tradingview: \"{asset.tradingview}\", "
        f"yahoo: {asset.yahoo}, markets: [crypto]}}"
        f"   # #{asset.rank} {asset.name}"
    )
