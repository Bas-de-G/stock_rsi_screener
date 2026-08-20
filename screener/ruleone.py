"""Phil Town's Rule #1 valuation, as far as free data allows.

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

# What each of the Big Four has to clear. Phil's own bar, applied to the four
# of the Big Five that a free feed can answer.
BIG_FOUR_THRESHOLD = 10.0

GREEN, AMBER, RED, NOT_APPLICABLE = "green", "amber", "red", "n/a"


@dataclass(frozen=True)
class RuleOne:
    """A Rule #1 reading, or an honest refusal to give one."""

    applicable: bool
    reason: str = ""              # why not, when applicable is False
    growth: float | None = None   # the rate actually used, in percent
    future_pe: float | None = None
    sticker: float | None = None
    mos: float | None = None
    price: float | None = None
    big_four: int = 0
    score: int = 0                # 1-10, 0 when not applicable
    band: str = NOT_APPLICABLE
    caution: str = ""             # readable, but do not act on it unexamined

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


def _score(price: float, sticker: float, mos: float, quality: int) -> tuple[int, str]:
    """A 1-10 ranking and a traffic light.

    Value is worth more than quality here -- six points against four -- because
    a wonderful business at a terrible price is what Rule #1 exists to refuse.

    The band is stricter than the score and matches Phil's actual condition: a
    green light means the price is at or below the margin-of-safety price *and*
    the business passes most of the Big Four. Everything at or below sticker is
    amber, which reads as "worth the work of checking by hand".
    """
    if price <= mos:
        value = 6.0
    elif price <= sticker:
        # Somewhere between the two prices: 3 at sticker, 6 at MOS.
        span = sticker - mos
        value = 3.0 + 3.0 * ((sticker - price) / span) if span else 3.0
    else:
        # Above sticker. Fades to zero as the price runs away from it.
        value = max(0.0, 3.0 * (sticker / price) - 1.0) if price else 0.0

    score = int(round(max(1.0, min(10.0, value + quality))))
    if price <= mos and quality >= 3:
        band = GREEN
    elif price <= sticker:
        band = AMBER
    else:
        band = RED
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

    growth = growth_rate(eps_growth_ttm, eps_growth_fy, sales_growth_ttm)
    if growth is None:
        return RuleOne(False, "no growth history")

    pe = future_pe(growth)
    sticker = sticker_price(eps_ttm, growth, pe)
    mos = mos_price(sticker)
    quality = big_four(roic, eps_growth_ttm, sales_growth_ttm, fcf_growth_ttm)
    score, band = _score(price, sticker, mos, quality)

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
        applicable=True, growth=growth, future_pe=pe, sticker=sticker, mos=mos,
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
)


def from_scanner(row: dict, price: float | None = None) -> RuleOne:
    """Evaluate straight from a TradingView scan row."""
    return evaluate(
        price=price if price is not None else row.get("close"),
        eps_ttm=row.get("earnings_per_share_diluted_ttm"),
        eps_growth_ttm=row.get("earnings_per_share_diluted_yoy_growth_ttm"),
        eps_growth_fy=row.get("earnings_per_share_diluted_yoy_growth_fy"),
        sales_growth_ttm=row.get("total_revenue_yoy_growth_ttm"),
        fcf_growth_ttm=row.get("free_cash_flow_yoy_growth_ttm"),
        roic=row.get("return_on_invested_capital"),
    )
