"""Phil Town's Rule #1 valuation, run backwards.

The method, in one line: guess what a company will earn in ten years, guess what
the market will pay for those earnings, discount that price back at the return
you demand, then halve it and call that a buy.

    future EPS    = EPS x (1 + g)^10
    future price  = future EPS x future P/E
    sticker price = future price / 1.15^10        <- the 15% you require
    MOS price     = sticker / 2                   <- the margin of safety

`1.15^10` is 4.046, so the whole chain collapses to roughly
`MOS ~= EPS x (1+g)^10 x future_PE / 8`. Which exposes the problem the rest of
this module is about: **the answer is dominated by g**. At 10% versus 15% the
sticker moves about 2.4x. This produces a band and a score, never a price to
act on, because a number that swings 2.4x on one assumption should not be
printed to two decimal places and believed.

Run forwards, it refuses almost everything
------------------------------------------
Pick one growth rate, compute one sticker price, compare. Done that way on the
live watchlist it marked 204 of 226 companies red -- which is faithful to Phil,
who expects to find a handful of businesses a year, and useless as a screen.
A verdict that is "no" 90% of the time carries almost no information, and it
cannot rank the 204.

So it is run **backwards**. Rather than asking what a company is worth at a
growth rate we picked, ask what growth rate today's price already demands:

    implied growth = the g for which sticker(g) == price

Solved by bisection, because sticker() rises monotonically in g. That number
needs no verdict to be useful -- "this price requires 15% a year for a decade"
is a complete thought -- and it is then compared against what the company has
actually delivered, expressed as a **range** rather than a point:

    conservative  the lowest of its growth rates      (the pessimistic case)
    base          the median, capped by sales growth  (the central case)

The gap between the base case and what the price demands is the whole verdict,
and it is continuous, so it ranks. On the live watchlist it spreads across all
ten score buckets with a median of 5, and bands at roughly 23/34/43.

Where this departs from the book, and why
-----------------------------------------
Three of Phil's inputs are not in any free feed, so each has a stated
substitute rather than a silent guess:

* **Growth.** Canonically the *equity* (book value per share) growth rate,
  cross-checked against analysts' estimate, taking the lower. No feed serves
  historical book value or a five-year estimate. Substitute: the lower of
  trailing EPS growth, full-year EPS growth, **and trailing sales growth**.
  Sales is in there for a reason -- it is one of Phil's Big Five precisely
  because earnings can be engineered and revenue cannot. Measured on the live
  watchlist, dropping it let one good year become a decade: AT&T scored a
  fictitious 20% a year and came out 361% below sticker; with sales included it
  reads 2.6% and sits 69% *above* it. Travelers, Ahold and US Bancorp all
  behaved the same way.

  The *base* case needs the same protection by another route. Its median of
  AT&T's three rates is 20%, because two of the three are that same one good
  year -- so the base case is additionally capped at **sales growth plus five
  points**. Earnings cannot outgrow sales for a decade; margin expansion is
  finite. Without that cap AT&T scored 9/10 and a green light, with a base case
  it has never come close to. With it, 6/10 and amber.

* **Future P/E.** Canonically the lower of the company's historical average
  P/E and twice the growth rate. No historical average is available, and the
  *current* P/E is a bad stand-in here of all places -- our signals fire when a
  stock is oversold, which is exactly when its multiple is at its trough.
  Substitute: twice the growth rate, held between 8 and 25. The floor matters:
  `2 x 0%` is a P/E of zero, and a company that stopped growing is not worth
  nothing.

* **Normalised EPS.** Phil's input is a *normalised* earnings figure; the feed
  serves trailing twelve months, one-offs and all. There is no way to normalise
  it from here, so instead a large jump is flagged and the reading is barred
  from a green light. LYFT is the live case: trailing EPS growth of **3,166%**
  puts it on a P/E of 2.4 and a sticker six times its market price -- one year
  wearing the costume of a decade. The flag deliberately also catches genuine
  hyper-growth (Alphabet at +112%), because from here the two are
  indistinguishable and "check this by hand" is the right answer to both.

* **The fifth M.** Meaning, Moat and Management are judgements, not fields.
  Only the quantitative half is automated -- see `big_four`.

And one number that is not a substitution but a correction: growth is capped at
**20%**, not the 15% a first pass might reach for. Capping at exactly the
required return makes the model degenerate -- the `(1+g)^10` in the numerator
and the `1.15^10` in the denominator cancel, so every fast grower lands at a
sticker price identical to its market price. All 226 of them, exactly 0.0% away.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

# Phil's own numbers, and not up for negotiation: ten years, a 15% required
# annual return, and half the sticker price as the margin of safety.
YEARS = 10
REQUIRED_RETURN = 0.15
MARGIN_OF_SAFETY = 0.5

# See the module docstring: 15 would sit exactly on the required return and
# collapse the model.
GROWTH_CAP = 20.0

# A no-growth company still earns something; a hyper-growth one will not keep
# its multiple for a decade.
FUTURE_PE_FLOOR, FUTURE_PE_CEILING = 8.0, 25.0

# Trailing EPS growth above this means the sticker rests on an earnings base
# that may be an event rather than a run rate -- a disposal, a tax benefit, a
# first profitable year. Rule #1 wants a normalised figure and the feed has
# none, so such a reading is flagged and barred from green rather than trusted
# or discarded. Set where it catches the obvious cases without pretending we
# can tell a one-off from real hyper-growth: we cannot, and both deserve a
# second look.
EPS_SPIKE = 100.0

# Earnings cannot outgrow sales for a decade -- margin expansion is finite --
# so the base case is capped at sales growth plus this many points. See the
# growth bullet above: without it AT&T's base case reads 20% off one good
# earnings year, and scores green against a rate it has never come close to.
MARGIN_LIFT = 5.0

# The window over which the gap between the base case and the price's demand
# is scored. A company delivering 20 points less than its price requires is as
# bad as the scale goes; 10 points more is as good.
GAP_FLOOR, GAP_CEILING = -20.0, 10.0

# What each of the Big Four has to clear. Phil's own bar, applied to the four
# of the Big Five that a free feed can answer.
BIG_FOUR_THRESHOLD = 10.0

GREEN, AMBER, RED, NOT_APPLICABLE = "green", "amber", "red", "n/a"


@dataclass(frozen=True)
class RuleOne:
    """A Rule #1 reading, or an honest refusal to give one."""

    applicable: bool
    reason: str = ""              # why not, when applicable is False
    growth: float | None = None   # the base case, and what the sticker uses
    conservative_growth: float | None = None   # the pessimistic case
    implied_growth: float | None = None        # what today's price demands
    future_pe: float | None = None
    sticker: float | None = None       # at the base-case growth rate
    sticker_low: float | None = None   # at the conservative rate
    mos: float | None = None
    eps: float | None = None
    price: float | None = None
    big_four: int = 0
    score: int = 0                # 1-10, 0 when not applicable
    band: str = NOT_APPLICABLE
    caution: str = ""             # readable, but do not act on it unexamined

    @property
    def headroom(self) -> float | None:
        """Base-case growth minus what the price demands, in percentage points.

        The whole verdict in one number, and a continuous one, so it ranks
        rather than merely rejecting. Positive means the company already grows
        faster than its price requires.
        """
        if not self.applicable or self.growth is None or self.implied_growth is None:
            return None
        return self.growth - self.implied_growth

    @property
    def demand_summary(self) -> str:
        """The reading as a sentence, which is how it reads on a card."""
        if not self.applicable:
            return f"not applicable — {self.reason}"
        return (f"price demands {self.implied_growth:.1f}%/yr · "
                f"delivered {self.conservative_growth:.1f}–{self.growth:.1f}%")

    @property
    def value_band(self) -> tuple[float, float] | None:
        """What Rule #1 says the company is worth, as a low-high pair of prices.

        `None` when the number would not be a valuation.

        Both growth rates floor at zero, and `future_pe` floors at 8, so a
        company whose earnings are flat or shrinking prices out at
        `EPS x 8 / 1.15^10` -- that is, `EPS x 1.98`, a P/E of 2, whatever the
        company is. 47 of the 226 readable names on the watchlist land on that
        constant exactly. It is arithmetically correct and it is not a value:
        it is the formula demanding 15% a year from a business that is not
        growing, and printing "$52.50" beside META's Morningstar fair value
        would read as a second opinion when it is an artefact of the floor.

        The score stays valid in that case -- it is built on implied growth,
        which handles a flat company perfectly well -- so the card shows the
        score and omits the price.
        """
        if not self.applicable or self.sticker is None or self.sticker_low is None:
            return None
        if not self.growth:
            return None
        return (self.sticker_low, self.sticker)

    @property
    def mos_band(self) -> tuple[float, float] | None:
        """The same band after the margin of safety -- what you'd pay, not what
        it's worth.

        Not on the card any more. It sat beside `value_band` as "Buy under" and
        came off: it is exactly that band halved, so it told the reader nothing
        they could not work out, and two Rule #1 prices next to a Morningstar
        fair value is one price too many. The single `mos` still travels in the
        journal and in the score's tooltip, and this stays for anything that
        wants the banded form back.
        """
        band = self.value_band
        return (mos_price(band[0]), mos_price(band[1])) if band else None

    @property
    def to_sticker(self) -> float | None:
        """How far the price sits below sticker, as a fraction of the price."""
        if not self.applicable or not self.price or self.sticker is None:
            return None
        return (self.sticker - self.price) / self.price

    @property
    def to_mos(self) -> float | None:
        if not self.applicable or not self.price or self.mos is None:
            return None
        return (self.mos - self.price) / self.price


def growth_rate(eps_growth_ttm, eps_growth_fy, sales_growth_ttm) -> float | None:
    """The rate to compound earnings at, in percent.

    The lowest of what is on offer, floored at zero and capped -- deliberately
    the most pessimistic reading available, because every error here is
    multiplied ten times over.

    Returns None when nothing is known, which is different from zero: zero is a
    company that stopped growing, None is a company we cannot see.
    """
    known = [g for g in (eps_growth_ttm, eps_growth_fy, sales_growth_ttm) if g is not None]
    if not known:
        return None
    return max(0.0, min(min(known), GROWTH_CAP))


def base_growth(eps_growth_ttm, eps_growth_fy, sales_growth_ttm) -> float | None:
    """The central case: the median rate, held down to what sales can support.

    The median rather than the minimum, because the minimum is the pessimistic
    case and having both is the point -- a range says more than a point does.

    Capped at sales growth plus `MARGIN_LIFT` for the reason in the module
    docstring: two of AT&T's three rates are the same one good earnings year,
    so its median reads 20% and its sales read 2.6%.
    """
    known = [g for g in (eps_growth_ttm, eps_growth_fy, sales_growth_ttm) if g is not None]
    if not known:
        return None
    central = statistics.median(known)
    if sales_growth_ttm is not None:
        central = min(central, sales_growth_ttm + MARGIN_LIFT)
    floor = growth_rate(eps_growth_ttm, eps_growth_fy, sales_growth_ttm) or 0.0
    return max(0.0, min(max(central, floor), GROWTH_CAP))


def implied_growth(eps: float, price: float, lo: float = -10.0,
                   hi: float = 60.0, steps: int = 120) -> float:
    """The growth rate today's price already demands, in percent.

    Rule #1 backwards: rather than pricing the company at a rate we picked,
    find the rate `g` for which `sticker(g)` equals what the market is asking.
    "This price requires 15% a year for a decade" is a complete thought with no
    verdict attached, and it is what makes the reading rank rather than reject.

    Bisection, because `sticker_price` rises monotonically in g (both the
    compounding and the multiple do). The bounds are returned as-is when the
    answer lies outside them: below -10% the company is cheap on any
    assumption, above 60% the number has stopped meaning anything.
    """
    def sticker(g: float) -> float:
        return sticker_price(eps, g, future_pe(max(g, 0.0)))

    if sticker(hi) < price:
        return hi
    if sticker(lo) > price:
        return lo
    for _ in range(steps):
        mid = (lo + hi) / 2
        if sticker(mid) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def future_pe(growth: float) -> float:
    """Twice the growth rate, kept inside a defensible range."""
    return max(FUTURE_PE_FLOOR, min(2 * growth, FUTURE_PE_CEILING))


def sticker_price(eps: float, growth: float, pe: float,
                  years: int = YEARS, required: float = REQUIRED_RETURN) -> float:
    """What the company is worth today if it is to return `required` a year."""
    return eps * (1 + growth / 100) ** years * pe / (1 + required) ** years


def mos_price(sticker: float) -> float:
    return sticker * MARGIN_OF_SAFETY


def big_four(roic, eps_growth, sales_growth, fcf_growth) -> int:
    """How many of the quantitative Big Five clear 10%.

    Four, not five: the fifth is equity (book value per share) growth, and no
    free feed serves the history to compute it. Counting an absent test as a
    pass would flatter every company equally, and inventing a fifth from a
    number that does not measure it would be worse.

    An unknown counts as a fail rather than a pass. This is the quality half of
    a method whose entire premise is that most companies do not qualify.
    """
    return sum(
        1 for value in (roic, eps_growth, sales_growth, fcf_growth)
        if value is not None and value >= BIG_FOUR_THRESHOLD
    )


def _score(headroom: float, quality: int) -> tuple[int, str]:
    """A 1-10 ranking and a traffic light, from the growth gap.

    `headroom` is base-case growth minus what the price demands, in percentage
    points. Scoring that rather than a price-versus-sticker comparison is what
    stopped this being a rejection machine: the forward version marked 204 of
    226 companies red, which cannot rank the 204. This spreads across all ten
    buckets with a median of 5.

    Quality then shifts it by up to a point and a half either way, so a
    business clearing all of the Big Four is not read the same as one clearing
    none of them at the same price. It moves the score; it does not set it.
    """
    span = GAP_CEILING - GAP_FLOOR
    value = 1 + 9 * max(0.0, min((headroom - GAP_FLOOR) / span, 1.0))
    score = int(max(1, min(10, round(value + (quality - 2) * 0.75))))
    band = GREEN if score >= 8 else AMBER if score >= 5 else RED
    return score, band


def evaluate(
    price,
    eps_ttm,
    eps_growth_ttm=None,
    eps_growth_fy=None,
    sales_growth_ttm=None,
    fcf_growth_ttm=None,
    roic=None,
) -> RuleOne:
    """Read a company the way Rule #1 does, or say why it cannot be read.

    Refusing is a real answer here. Rule #1 projects earnings forward for a
    decade, which is meaningless for a company that has none: Intel is on this
    watchlist at -2.12 a share, so it gets "not applicable" rather than a
    confident number compounded from a negative.
    """
    if price is None or price <= 0:
        return RuleOne(False, "no price")
    if eps_ttm is None:
        return RuleOne(False, "no earnings figure")
    if eps_ttm <= 0:
        return RuleOne(False, "no positive earnings to project")

    conservative = growth_rate(eps_growth_ttm, eps_growth_fy, sales_growth_ttm)
    if conservative is None:
        return RuleOne(False, "no growth history")
    growth = base_growth(eps_growth_ttm, eps_growth_fy, sales_growth_ttm)

    pe = future_pe(growth)
    sticker = sticker_price(eps_ttm, growth, pe)
    mos = mos_price(sticker)
    # The pessimistic end of the same calculation. Quoting one sticker implies a
    # precision the method does not have -- the two rates are both defensible
    # readings of the same filings, and across the watchlist they straddle a
    # median 1.4x and a 90th-percentile 5.6x. A band says that out loud.
    sticker_floor = sticker_price(
        eps_ttm, conservative, future_pe(max(conservative, 0.0))
    )
    demanded = implied_growth(eps_ttm, price)
    quality = big_four(roic, eps_growth_ttm, sales_growth_ttm, fcf_growth_ttm)
    score, band = _score(growth - demanded, quality)

    caution = ""
    if eps_growth_ttm is not None and eps_growth_ttm > EPS_SPIKE:
        caution = (
            f"trailing earnings jumped {eps_growth_ttm:,.0f}% — check that is a run "
            f"rate and not a one-off before trusting the sticker, which is built "
            f"entirely on it"
        )
        # The whole sticker is built on this earnings base. A green light says
        # "act", and nothing built on an unexamined one-off earns that.
        if band == GREEN:
            band = AMBER

    return RuleOne(
        applicable=True, growth=growth, conservative_growth=conservative,
        implied_growth=demanded, future_pe=pe, sticker=sticker, mos=mos,
        sticker_low=min(sticker_floor, sticker), eps=eps_ttm,
        price=price, big_four=quality, score=score, band=band, caution=caution,
    )


# The scanner columns `evaluate` needs, so the batch request can carry them.
FIELDS = (
    "earnings_per_share_diluted_ttm",
    "earnings_per_share_diluted_yoy_growth_ttm",
    "earnings_per_share_diluted_yoy_growth_fy",
    "total_revenue_yoy_growth_ttm",
    "free_cash_flow_yoy_growth_ttm",
    "return_on_invested_capital",
    # The three below exist only to put EPS in the same currency as the price.
    "price_earnings_ttm",
    "currency",
    "fundamental_currency_code",
)


def eps_in_quote_currency(row: dict) -> float | None:
    """The TTM EPS expressed in the currency the stock is quoted in.

    TradingView reports fundamentals in the company's reporting currency --
    USD for almost everything -- while `close` is in the listing currency.
    Dividing one by the other silently produced nonsense for every non-USD
    listing: Rentokil showed a sticker of 0.71 against a price of 345.70,
    because 0.186 is dollars of earnings and 345.70 is pence of price.

    No FX feed is needed. `price_earnings_ttm` is price over EPS with both
    already in the listing currency, so `close / pe` *is* the local EPS. The
    rates that falls out of are exactly right and agree across names: 75.43
    for GBX, 0.876 for EUR (identical for SAP, Orange and Deutsche Telekom),
    9.912 for NOK, 1.0 for USD.

    Returns None when the conversion cannot be made, so `evaluate` refuses
    rather than quoting a sticker that is wrong by a factor of a hundred.
    """
    eps = row.get("earnings_per_share_diluted_ttm")
    if eps is None or eps <= 0:
        # A loss is a loss in any currency, and converting it needs a P/E that
        # does not exist for a loss-maker. Passed through unchanged so the
        # caller still reports "no positive earnings" rather than "no figure".
        return eps

    quote, reporting = row.get("currency"), row.get("fundamental_currency_code")
    if not quote or not reporting or quote == reporting:
        return eps  # same currency: the reported figure already lines up

    close, pe = row.get("close"), row.get("price_earnings_ttm")
    if not close or not pe:
        return None  # no P/E to derive the rate from (loss-makers land here)
    return close / pe


def from_scanner(row: dict, price: float | None = None) -> RuleOne:
    """Evaluate straight from a TradingView scan row."""
    return evaluate(
        price=price if price is not None else row.get("close"),
        eps_ttm=eps_in_quote_currency(row),
        eps_growth_ttm=row.get("earnings_per_share_diluted_yoy_growth_ttm"),
        eps_growth_fy=row.get("earnings_per_share_diluted_yoy_growth_fy"),
        sales_growth_ttm=row.get("total_revenue_yoy_growth_ttm"),
        fcf_growth_ttm=row.get("free_cash_flow_yoy_growth_ttm"),
        roic=row.get("return_on_invested_capital"),
    )
