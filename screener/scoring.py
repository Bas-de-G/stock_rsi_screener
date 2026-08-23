"""One conviction score out of many factors, with the weights in config.

Everything the screener knows about a signal is already on the card: how deep
the RSI dip went, how far the price sits below fair value, whether earnings are
growing, what Rule #1 makes of it, whether results are imminent. What the card
cannot do is say which of those matters more. `is_strong` answers that with a
rule -- fair value required, everything else a veto -- which is legible and
unweighted and cannot be tuned by measurement.

This is the weighted version. Each factor reports a *strength* between 0 and 1,
the weights come from `config.yaml`, and the result is a 1-10 coefficient with
the per-factor contributions kept alongside it so the number can be taken apart
on the page rather than believed.

It decides nothing yet. See `SHADOW` below.

Two decisions worth stating, because both could reasonably have gone the other
way
-------------------------------------------------------------------------
**An unknown factor is dropped and the rest reweighted, not scored zero.**
Scoring it zero would mean "nobody has recorded a fair value for this" and
"this stock is dear" produce the same number, and 88 of the 253 names have no
fair value on file. Dropping it keeps the score a statement about what is
known.

That trade has a cost, and it is the reason `coverage` exists: a name with one
known factor out of five gets a fully confident-looking score off a fifth of
the evidence. So every result carries the fraction of the total weight that was
actually known, and the page shows it. A 9/10 on 20% coverage is a different
claim from a 9/10 on 100%, and the reader is entitled to tell them apart.

**The earnings blackout is a factor, not a gate.** It could have been a
multiplier that zeroes the score outright, which is what the dashboard does to
the rocket. Keeping it in the weighted set means its influence is a number in
the config like every other, so "how much should imminent results really
matter?" becomes answerable from the journal instead of asserted here. The
suspension still applies separately and still hides the signal -- this is only
about the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The composite is computed, journalled and displayed, but `is_strong` keeps
# deciding which signals earn a rocket and which ring a phone.
#
# Swapping a measured rule for an unmeasured one is the mistake this whole
# roadmap exists to avoid: the asymmetric rule has a track record on the
# Historical Dashboard, and this has none until it has run forward for a while.
# `recommendations.csv` stamps the score and the weights into every row from
# today, so the comparison can be made on evidence rather than taste.
SHADOW = True

# Where the 1-10 coefficient lands. A weighted strength of 0 is a 1 rather than
# a 0 because the scale is a coefficient of quality, not a percentage, and
# "1 out of 10" is the floor everyone expects.
MIN_SCORE, MAX_SCORE = 1, 10

# Bands for the dot beside the number. Deliberately not thirds: a 7 is a good
# signal and should not have to reach 7.5 to look like one.
GREEN_AT, AMBER_AT = 7.0, 4.5

GREEN, AMBER, RED = "green", "amber", "red"

# Below this share of the total weight, the score is reported but marked thin.
# Half is the point at which most of what the model wants to know is missing.
THIN_COVERAGE = 0.5


@dataclass(frozen=True)
class Factor:
    """One input, reduced to a strength between 0 and 1.

    `strength` is None when the factor could not be read at all -- which is a
    third state, distinct from a strength of 0. Unknown drops out of the
    average; zero drags it down.
    """

    key: str
    label: str
    strength: float | None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.strength is not None


@dataclass(frozen=True)
class Contribution:
    """What one factor did to the final number, for the breakdown on the card."""

    key: str
    label: str
    strength: float | None
    weight: float
    share: float          # fraction of the score this factor accounts for
    points: float         # of the 9 points above the floor
    detail: str = ""


@dataclass(frozen=True)
class Composite:
    score: int
    band: str
    coverage: float                      # share of total weight that was known
    contributions: list[Contribution] = field(default_factory=list)

    @property
    def thin(self) -> bool:
        """Whether too little was known for the number to carry its usual
        weight. Not a failure -- a caveat the reader is owed."""
        return self.coverage < THIN_COVERAGE

    @property
    def known_factors(self) -> list[Contribution]:
        return [c for c in self.contributions if c.strength is not None]


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def band_for(score: float) -> str:
    if score >= GREEN_AT:
        return GREEN
    return AMBER if score >= AMBER_AT else RED


def composite(factors, weights: dict) -> Composite:
    """Weigh the factors and return a 1-10 coefficient with its breakdown.

    Weights are looked up by factor key and default to zero, so adding a factor
    to the code without adding it to the config leaves the score unchanged
    rather than silently shifting every number on the site.
    """
    total_weight = sum(max(0.0, weights.get(f.key, 0.0)) for f in factors)
    known = [f for f in factors if f.known and weights.get(f.key, 0.0) > 0]
    live_weight = sum(weights[f.key] for f in known)

    if not live_weight:
        # Nothing readable carries any weight. A floor score with zero coverage
        # is the honest answer; inventing a middling 5 would look like a
        # measurement.
        return Composite(
            score=MIN_SCORE, band=RED, coverage=0.0,
            contributions=[
                Contribution(f.key, f.label, f.strength,
                             max(0.0, weights.get(f.key, 0.0)), 0.0, 0.0, f.detail)
                for f in factors
            ],
        )

    strength = sum(weights[f.key] * f.strength for f in known) / live_weight
    span = MAX_SCORE - MIN_SCORE
    raw = MIN_SCORE + span * clamp(strength)

    contributions = []
    for f in factors:
        weight = max(0.0, weights.get(f.key, 0.0))
        if f.known and weight:
            share = weight / live_weight
            points = span * share * clamp(f.strength)
        else:
            share = points = 0.0
        contributions.append(
            Contribution(f.key, f.label, f.strength, weight, share, points, f.detail)
        )

    return Composite(
        score=int(round(raw)),
        band=band_for(raw),
        coverage=live_weight / total_weight if total_weight else 0.0,
        contributions=contributions,
    )


# ------------------------------------------------------ reading the factors
#
# Each of these turns one thing the screener already knows into a strength
# between 0 and 1. They are separate from `composite` on purpose: the weighing
# is arithmetic and the reading is judgement, and only the second one needs
# arguing about.

# How far below the threshold an RSI dip has to go to score full marks.
#
# Calibrated, not picked: across the 273 recorded daily buy patterns the dip
# below 30 runs a median of 2.3 points, a 75th percentile of 4.7 and a 90th of
# 7.2. Saturating at ten points -- the first guess -- put 97% of patterns under
# full marks and squeezed the whole factor into the bottom third of its range,
# where it ranked almost nothing. Six sits between the 75th and 90th
# percentiles: a genuinely hard sell-off, reached by about one pattern in six.
DIP_SATURATES_AT = 6.0

# Used only where a horizon sets no margin of its own. Normally the margin
# scales this factor -- see `valuation_factor`.
DISCOUNT_SATURATES_AT = 0.40

# EPS growth scoring full marks. Ten percent -- the Rule #1 Big Four threshold
# -- turned out to be a low bar for this watchlist: 60% of the names cleared it
# outright and the factor stopped separating them. The median here is +18% and
# the 75th percentile +42%, so 30% is "growing fast" rather than merely
# "growing", which is what a top mark should mean.
GROWTH_SATURATES_AT = 30.0


def pattern_factor(rsi: float | None, threshold: float, at_dip: bool = False) -> Factor:
    """How deep the dip went, relative to the line it had to cross.

    Every signal here has already completed the same double cross, so the shape
    carries no information between them -- depth is the part that varies.

    `rsi` is the trough *between* the two crosses where a pattern exists, and
    today's reading otherwise. Scoring today's RSI in both cases was the first
    version and it was wrong: by the time a double cross completes, RSI is back
    above the line by construction, so every signal on the page scored zero on
    the one factor that describes the signal itself. The dip is a fact about
    the setup; the current reading is a fact about the stock.
    """
    if rsi is None:
        return Factor("pattern", "RSI depth", None, "no reading")
    depth = threshold - rsi
    where = "at the dip" if at_dip else "now"
    return Factor(
        "pattern", "RSI depth", clamp(depth / DIP_SATURATES_AT),
        f"RSI {rsi:.1f} {where}, {depth:+.1f} against the {threshold:g} line",
    )


def valuation_factor(price, fair_value, margin: float = 0.0) -> Factor:
    """How far under fair value the price sits, scaled by the horizon's margin.

    Meeting the margin is half marks; doubling it is full. So on the daily
    chart, which wants 30% of headroom, a stock 30% under fair value scores
    0.5 and one 60% under scores 1.0 -- and on the hourly chart, which wants
    10%, the same two marks fall at 10% and 20%. The factor rescales itself to
    whatever each horizon already demands instead of carrying a second constant
    that has to be kept in step with the first.

    The first version subtracted the margin and scaled the remainder, which
    scored 87% of the watchlist at exactly zero: the median name sits 2% under
    fair value against a 30% margin. A factor that is zero for seven names in
    eight cannot rank them, which is the one job it has here.
    """
    if not price or not fair_value:
        return Factor("valuation", "Fair value", None, "not checked yet")
    discount = (fair_value - price) / price
    strength = (clamp(0.5 * discount / margin) if margin > 0
                else clamp(discount / DISCOUNT_SATURATES_AT))
    return Factor(
        "valuation", "Fair value", strength,
        f"{discount * 100:+.0f}% to fair value, {margin * 100:.0f}% wanted",
    )


def growth_factor(growth: float | None) -> Factor:
    if growth is None:
        return Factor("growth", "EPS growth", None, "no figure yet")
    return Factor(
        "growth", "EPS growth", clamp(growth / GROWTH_SATURATES_AT),
        f"{growth:+.1f}% year on year",
    )


def rule_one_factor(reading) -> Factor:
    """Rule #1's own 1-10, rescaled onto the same 0-1 as everything else.

    Deliberately the score and not the sticker gap: the score is already the
    banded, error-tolerant form of that gap, and feeding the raw price
    difference in would import all the sensitivity the band exists to contain.
    """
    if reading is None or not getattr(reading, "applicable", False):
        why = getattr(reading, "reason", "") or "no reading"
        return Factor("ruleone", "Buffett score", None, why)
    return Factor(
        "ruleone", "Buffett score", clamp((reading.score - 1) / 9),
        f"{reading.score}/10, price demands {reading.implied_growth:.1f}%/yr",
    )


def earnings_factor(window) -> Factor:
    """Clear of results, or too close to them.

    Binary rather than graded: the risk being priced is a gap on the open, and
    a gap does not get smaller because the release is three days out instead of
    one. What is tunable is how much that matters, and that lives in the
    weight.
    """
    if window is None:
        return Factor("earnings", "Earnings timing", None, "no calendar entry")
    if not window.suspended:
        return Factor("earnings", "Earnings timing", 1.0, "no release near")
    when = (f"results in {window.sessions} session{'' if window.sessions == 1 else 's'}"
            if window.sessions is not None else "results just out")
    return Factor("earnings", "Earnings timing", 0.0, when)
