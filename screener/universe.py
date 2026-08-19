"""Choosing which companies belong on the watchlist.

The screener started as a hand-picked list and grows by proposing additions
from an index, which raises three problems that are not obvious until you look
at the raw data:

* **The same company appears many times.** NVIDIA is `NASDAQ:NVDA`, and also
  `MUN:NVD` in Munich, `EUROTLX:4NVDA` in Milan and `SIX:NVDA.USD` in Zurich.
  Germany's scanner returns 15,079 "stocks", most of which are foreign
  listings. Restricting to each country's primary exchange removes essentially
  all of it.
* **The same company appears twice on one exchange.** Alphabet is GOOGL and
  GOOG; Berkshire is BRK.A and BRK.B. Both are ordinary shares of one
  business, so tracking both is two cards saying the same thing.
* **Not every "stock" is a share.** `NASDAQ:GOOGN` is a depositary line on
  Alphabet's preferred stock. It reports Alphabet's market cap and its own
  price, which is exactly the kind of mismatch that produces a confident wrong
  answer downstream.

TradingView answers all three itself, which is why selection reads fields
rather than guesses: `typespecs` says `['common']` or `['preferred']`, and
`indexes` carries genuine membership -- "S&P 500", "NASDAQ 100", "STOXX Europe
600" -- rather than a market-cap threshold standing in for it.

Nothing here removes a ticker. The watchlist only ever grows: a name already in
config.yaml is excluded from the proposals and otherwise left alone, whatever
the index has done since.
"""

from __future__ import annotations

from dataclasses import dataclass

# Where each country's own companies actually list. Anything else on a national
# scanner is a cross-listing of a company that belongs to another market.
PRIMARY_EXCHANGES = {
    "america": ("NASDAQ", "NYSE", "AMEX"),
    "netherlands": ("EURONEXT",),
    "france": ("EURONEXT",),
    "belgium": ("EURONEXT",),
    "germany": ("XETR",),
    "uk": ("LSE",),
    "sweden": ("OMXSTO",),
    "spain": ("BME",),
    "italy": ("MIL",),
    "switzerland": ("SIX",),
    "hongkong": ("HKEX",),
}

# Morningstar's market identifier for each exchange, for building the quote
# URL. A ticker whose exchange is not here still gets proposed -- the slug is
# just left for a human to fill in, which is better than guessing wrong and
# producing a link to nothing.
MORNINGSTAR_MIC = {
    "NASDAQ": "xnas",
    "NYSE": "xnys",
    "AMEX": "xase",
    "EURONEXT": "xams",
    "XETR": "xetr",
    "LSE": "xlon",
    "OMXSTO": "xsto",
    "BME": "xmce",
    "SIX": "xswx",
    "HKEX": "xhkg",
}

# Price below which a listing earns the dashboard's "Under $10" filter.
PENNY_PRICE = 10.0


@dataclass(frozen=True)
class Candidate:
    """One listing, as the scanner describes it."""

    tv_symbol: str          # "NASDAQ:NVDA"
    symbol: str             # "NVDA"
    exchange: str           # "NASDAQ"
    name: str               # "NVIDIA Corporation"
    price: float | None
    currency: str
    market_cap: float | None
    volume: float | None
    indexes: tuple[str, ...]
    typespecs: tuple[str, ...]
    kind: str               # TradingView's `type`, e.g. "stock"

    @property
    def is_common_stock(self) -> bool:
        return self.kind == "stock" and "common" in self.typespecs

    @property
    def morningstar(self) -> str:
        mic = MORNINGSTAR_MIC.get(self.exchange)
        return f"{mic}/{self.symbol.lower()}" if mic else ""

    def markets(self) -> tuple[str, ...]:
        """The dashboard filter groups this listing belongs to.

        Read off real index membership where there is one, so a NYSE company
        outside the S&P 500 is not labelled as being in it -- which is what
        inferring the tag from the exchange would do.
        """
        tags = []
        if "S&P 500" in self.indexes:
            tags.append("sp500")
        if "NASDAQ 100" in self.indexes or self.exchange == "NASDAQ":
            tags.append("nasdaq")
        if "STOXX Europe 600" in self.indexes or self.exchange in (
            "EURONEXT", "XETR", "LSE", "OMXSTO", "BME", "SIX", "MIL"
        ):
            tags.append("europe")
        if self.exchange == "HKEX":
            tags.append("asia")
        if self.price is not None and self.price < PENNY_PRICE:
            tags.append("penny")
        # Every ticker must carry at least one market or the dashboard filter
        # loses it, and the config test enforces that. Fall back to the
        # listing's region rather than dropping the candidate.
        return tuple(tags) or ("sp500" if self.exchange in ("NYSE", "AMEX") else "nasdaq",)


def parse_candidates(rows) -> list[Candidate]:
    """Turn `discover_market` records into candidates, skipping unusable ones."""
    out = []
    for row in rows:
        tv_symbol = row.get("symbol") or ""
        if ":" not in tv_symbol:
            continue
        exchange, symbol = tv_symbol.split(":", 1)
        indexes = tuple(
            entry.get("name", "") for entry in (row.get("indexes") or [])
            if isinstance(entry, dict)
        )
        out.append(Candidate(
            tv_symbol=tv_symbol,
            symbol=symbol,
            exchange=exchange,
            name=row.get("description") or row.get("name") or symbol,
            price=row.get("close"),
            currency=row.get("currency") or "USD",
            market_cap=row.get("market_cap_basic"),
            volume=row.get("average_volume_10d_calc"),
            indexes=indexes,
            typespecs=tuple(row.get("typespecs") or ()),
            kind=row.get("type") or "",
        ))
    return out


def drop_secondary_listings(candidates, market: str) -> list[Candidate]:
    """Keep only the exchanges where a market's own companies actually list."""
    allowed = PRIMARY_EXCHANGES.get(market)
    if not allowed:
        return list(candidates)
    return [c for c in candidates if c.exchange in allowed]


def company_key(name: str) -> str:
    """The business behind a listing, as far as its description reveals.

    "Alphabet Inc. Class A" and "Alphabet Inc. Class C" both reduce to
    `alphabetinc`, which is what makes them recognisable as one company.

    Matching on market capitalisation instead is the obvious idea and does not
    work. It is *nearly* identical between share classes but not exactly --
    TradingView reports Alphabet as 4193427235077.0005 for GOOGL and
    4193427235077 for GOOG -- so equality misses, and rounding enough to catch
    it starts colliding unrelated companies: at six significant figures, five
    hundred candidates spread over three decades of market cap have a few
    percent chance of a false match, and a false match silently drops a real
    company from the proposals.
    """
    import re

    for marker in (" Class ", " Cl ", " Series ", " Ser "):
        if marker in name:
            name = name.split(marker)[0]
            break
    return re.sub(r"[^a-z0-9]", "", name.lower())


def drop_duplicate_share_classes(candidates) -> list[Candidate]:
    """One line per company: the most traded of its share classes.

    Alphabet is GOOGL and GOOG, Berkshire is BRK.A and BRK.B. Both are ordinary
    shares in one business, so tracking both is two cards saying the same
    thing. The one to keep is whichever people actually trade.

    Market capitalisation is required to agree within a percent as well, so a
    coincidence of naming cannot merge two genuinely different companies.
    """
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(company_key(candidate.name), []).append(candidate)

    kept: list[Candidate] = []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        biggest = max(group, key=lambda c: c.market_cap or 0.0)
        same_company = [c for c in group if _caps_agree(c, biggest)]
        kept.append(max(same_company, key=lambda c: (c.volume or 0.0, c.symbol)))
        kept.extend(c for c in group if c not in same_company)

    # Restore the caller's order, which is by size and therefore meaningful.
    order = {c.tv_symbol: i for i, c in enumerate(candidates)}
    kept.sort(key=lambda c: order[c.tv_symbol])
    return kept


def _caps_agree(one: Candidate, other: Candidate, tolerance: float = 0.01) -> bool:
    """Whether two listings report close enough to the same company size."""
    if one.market_cap is None or other.market_cap is None:
        return one.market_cap == other.market_cap
    biggest = max(abs(one.market_cap), abs(other.market_cap))
    if biggest == 0:
        return True
    return abs(one.market_cap - other.market_cap) / biggest <= tolerance


def select(
    candidates,
    market: str,
    indexes=(),
    min_volume: float = 0.0,
    exclude=(),
    exclude_companies=(),
) -> list[Candidate]:
    """Everything worth proposing, in order of size.

    `indexes` restricts to real membership of at least one named index; empty
    means "any listing that got this far". `exclude` is the watchlist as it
    already stands -- those are dropped from the proposals and never otherwise
    touched, because this only ever adds.

    `exclude_companies` is the *name* of each business already tracked, which
    is what catches the other share class of one you have. Alphabet is the live
    case: GOOGL is on the watchlist and so is filtered out by symbol, and
    without this GOOG is then the sole survivor of its own duplicate group and
    gets proposed as though it were a different company.
    """
    excluded = {s.upper() for s in exclude}
    tracked = {company_key(n) for n in exclude_companies if n}
    wanted = set(indexes)

    kept = drop_secondary_listings(candidates, market)
    kept = [c for c in kept if c.is_common_stock]
    if wanted:
        kept = [c for c in kept if wanted & set(c.indexes)]
    if min_volume:
        kept = [c for c in kept if (c.volume or 0.0) >= min_volume]
    kept = drop_duplicate_share_classes(kept)
    return [
        c for c in kept
        if c.symbol.upper() not in excluded and company_key(c.name) not in tracked
    ]


def as_yaml_line(candidate: Candidate) -> str:
    """One config.yaml ticker entry, matching the file's existing style."""
    markets = ", ".join(candidate.markets())
    morningstar = candidate.morningstar or "TODO"
    return (
        f"  - {{symbol: {candidate.symbol}, tradingview: \"{candidate.tv_symbol}\", "
        f"morningstar: {morningstar}, markets: [{markets}]}}"
    )
