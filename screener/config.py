"""Configuration loading.

Credentials never live here — they come from the environment (see .env.example).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .drawdown import DEFAULT_ATH_FLOOR, RECENT_WINDOW_BARS

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

# How many bars of its own timeframe a pattern may be old and still count as
# "just fired". Two is deliberately tight: this badge answers "act now", while
# `window_days` answers the looser "is this still a valid setup".
FRESH_BARS = 2

# ...but never a shorter span than the page takes to rebuild.
#
# Two bars of the 1h chart is two hours, and the screener publishes every three
# hours at the median -- up to six on a busy weekday, because GitHub delivers
# only some of the scheduled runs. A window narrower than that cadence is a
# promise the page cannot keep: a pattern completing just after one run is
# already expired by the next, so it is never once shown as fresh. PTON did
# exactly this on 2026-08-10, completing at 17:30 and first appearing on the
# 20:12 page at two hours forty-two, silently past the line.
#
# You cannot claim finer resolution than you publish at, so the window is
# floored at one comfortable publish cycle. This binds on the 1h chart alone;
# every other horizon's two bars already exceed it.
FRESH_FLOOR_HOURS = 6.0

VALUATION_RULES = ("fair_value_below_price", "price_below_fair_value")
WINDOW_UNITS = ("calendar", "trading")

# Market groups the dashboard can filter by. A ticker can belong to several --
# AAPL is both an S&P 500 constituent and Nasdaq-listed -- so `markets` on a
# Ticker is a list, not a single value.
MARKETS = ("sp500", "nasdaq", "europe", "asia", "penny", "crypto")
MARKET_LABELS = {
    "sp500": "S&P 500",
    "nasdaq": "NASDAQ",
    "europe": "Europe",
    "asia": "Asia",
    "penny": "Under $10",
    "crypto": "Crypto",
}


@dataclass(frozen=True)
class Horizon:
    """One RSI timeframe, and everything that scales with it.

    The three tunables all move together with the holding period, which is the
    whole point of having them per-horizon rather than global:

    * `window_days` -- how long the two upward crosses may be apart. Fixed at
      14 calendar days this only ever made sense for the daily chart: 14 days
      is two weekly bars, so a 1w pattern could never form at all.
    * `margin` -- how far below fair value the price must sit before the
      valuation gate confirms. A longer hold wants more headroom.
    * `leverage` -- the multiplier surfaced on the dashboard for this horizon.
      A fixed number attached to the timeframe, not a calculation.
    """

    key: str
    label: str
    tv_interval: str      # TradingView scanner suffix: 60, 240, 1D, 1W
    yahoo_interval: str   # Yahoo chart interval: 60m, 4h, 1d, 1wk
    yahoo_range: str      # how much history to request when backfilling
    window_days: int
    margin: float
    leverage: int
    intraday: bool = False
    # How long one bar of this timeframe lasts. Used to decide whether a
    # pattern *just* completed, which is a different question from whether it
    # is still live: `window_days` keeps a 1d signal actionable for a
    # fortnight, so a pattern that closed yesterday and one that closed
    # thirteen days ago look identical without this.
    bar_hours: float = 24.0

    @property
    def margin_pct(self) -> str:
        return f"{self.margin * 100:g}%"

    @property
    def fresh_hours(self) -> float:
        """The freshness span in hours: two bars, or one publish cycle if
        that is longer. See FRESH_FLOOR_HOURS."""
        return max(self.bar_hours * FRESH_BARS, FRESH_FLOOR_HOURS)

    @property
    def fresh_within(self) -> dt.timedelta:
        """How recently the second cross must have landed to count as fresh."""
        return dt.timedelta(hours=self.fresh_hours)

    @property
    def fresh_label(self) -> str:
        """Human phrasing for that span — '6 hours', '8 hours', '2 weeks'."""
        hours = self.fresh_hours
        if hours < 24:
            return f"{hours:g} hours"
        days = hours / 24
        if days < 14:
            return f"{days:g} day" + ("s" if days != 1 else "")
        return f"{days / 7:g} weeks"


# Verified against both services before being wired in: TradingView serves
# RSI|60, RSI|240, RSI and RSI|1W, and Yahoo serves all four intervals.
#
# The ranges are sized to what actually gets used, not to what Yahoo will hand
# over. Only `dashboard.chart_days` (90) bars are ever plotted, plus 15 to seed
# Wilder's RSI and one lead-in bar for cross detection -- about 105. Each range
# below lands at 2.4-2.8x that, which is comfortable headroom for a thinly
# traded ticker without hoarding.
#
# This matters more than it looks: the database is committed to git on every
# scheduled run. Asking Yahoo for its full 730-day intraday depth gave 5,000
# hourly bars a ticker -- a 54 MB database to display 2.8 MB worth, growing the
# repository by gigabytes a month. These ranges make it ~7 MB.
DEFAULT_HORIZONS: tuple[Horizon, ...] = (
    Horizon("1h", "1 hour",  "60",  "60m", "2mo", window_days=2,  margin=0.10, leverage=10, intraday=True, bar_hours=1),
    Horizon("4h", "4 hours", "240", "4h",  "6mo", window_days=5,  margin=0.20, leverage=5,  intraday=True, bar_hours=4),
    Horizon("1d", "1 day",   "1D",  "1d",  "1y",  window_days=14, margin=0.30, leverage=2,  bar_hours=24),
    Horizon("1w", "1 week",  "1W",  "1wk", "5y",  window_days=90, margin=0.50, leverage=1,  bar_hours=168),
)
DEFAULT_HORIZON = "1d"


@dataclass(frozen=True)
class Ticker:
    symbol: str
    tradingview: str
    # Empty for anything with no fair value to look up -- cryptocurrencies.
    # `valued` is what the rest of the code should ask; see the note there.
    morningstar: str = ""
    # Yahoo's symbol for backfill. Usually the same as `symbol`, but not
    # always: Rolls-Royce is RR. on TradingView and RR.L on Yahoo.
    yahoo: str = ""
    # Quote currency. Rolls-Royce trades in pence, so a bare number next to a
    # dollar price would be misleading.
    currency: str = "USD"
    # Which dashboard market filters this ticker appears under.
    markets: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.yahoo:
            object.__setattr__(self, "yahoo", self.symbol)
        object.__setattr__(self, "markets", tuple(self.markets))

    @property
    def valued(self) -> bool:
        """Whether a fair value can be looked up for this at all.

        False for cryptocurrencies: there is no analyst fair value for Bitcoin,
        and inventing a proxy would be worse than admitting it. The consequence
        runs all the way to the verdict -- `signals.is_strong` requires a
        valuation, so an unvalued ticker can fire a buy signal but can never
        earn a rocket. That is deliberate, not an oversight to route around:
        the pattern is the only evidence there is, and the card says so.
        """
        return bool(self.morningstar)

    @property
    def morningstar_url(self) -> str:
        return f"https://www.morningstar.com/stocks/{self.morningstar}/quote"

    @property
    def tradingview_url(self) -> str:
        """Where to look at the chart. The only external link an unvalued
        ticker has, and the fallback for the card's primary button."""
        return (
            "https://www.tradingview.com/symbols/"
            f"{self.tradingview.replace(':', '-')}/technicals/"
        )


@dataclass(frozen=True)
class RsiConfig:
    period: int = 14
    threshold: float = 30.0          # oversold line, for buy signals
    overbought: float = 70.0         # overbought line, for sell signals
    interval: str = "1D"


@dataclass(frozen=True)
class SignalConfig:
    window_days: int = 14
    window_unit: str = "calendar"
    valuation_rule: str = "fair_value_below_price"
    fire_without_valuation: bool = False

    def describe_rule(self) -> str:
        if self.valuation_rule == "fair_value_below_price":
            return "fair value < price (stock trading ABOVE Morningstar fair value)"
        return "price < fair value (stock trading BELOW Morningstar fair value)"


@dataclass(frozen=True)
class StorageConfig:
    database: Path
    csv_dir: Path
    fair_values: Path
    # Which strong buys have already been announced. Deliberately outside
    # `csv_dir`: everything in there is regenerated data and gitignored, while
    # this has to be committed on every run or the same signal is announced
    # again half an hour later. See `screener.notified`.
    notifications: Path = Path("notifications.json")
    # The append-only record of what was recommended, and when. Outside
    # `csv_dir` for the same reason as `notifications`: it has to be committed
    # on every run, and everything in there is regenerated and gitignored.
    recommendations: Path = Path("recommendations.csv")


@dataclass(frozen=True)
class DashboardConfig:
    output: Path
    chart_days: int = 90
    # Where the published pages live, so a notification can link back to the
    # right horizon. Empty means the message carries a bare filename, which is
    # still useful locally and harmless in a webhook.
    site_url: str = ""


@dataclass(frozen=True)
class MorningstarConfig:
    """Note there is deliberately no password field here.

    Signing in happens once, by hand, in a real browser window (see
    `screener.morningstar.save_login_session`). Only the resulting session
    cookie is persisted, to `state_file`. Nothing in this tool reads, stores,
    or transmits a Morningstar password.
    """

    state_file: Path
    page_timeout: int = 45
    debug_on_failure: bool = True
    # False by default: the interactive `login` flow uses a visible browser and
    # gets through Morningstar's bot protection, while a headless scrape has
    # been observed to trip an AWS WAF CAPTCHA on the subscriber-only page. This
    # only runs on a laptop, not CI, so a visible window during a scrape is a
    # fine trade for not being silently blocked.
    headless: bool = False


DEFAULT_PUSH_HORIZONS: tuple[str, ...] = ("4h", "1d")


@dataclass(frozen=True)
class NotifyConfig:
    """Which timeframes are allowed to interrupt someone.

    Separate from which timeframes are *screened*: all four still appear on the
    dashboard, still get journalled, and still open a GitHub issue. This only
    governs the phone.

    The hourly chart is where the volume is -- 55 of the 75 alerts ever sent
    came from it -- and an hourly signal has a freshness window measured in
    hours, so by the time a phone is picked up it is often already stale. The
    slower charts are the ones worth being interrupted for.
    """

    push_horizons: tuple[str, ...] = DEFAULT_PUSH_HORIZONS
    # Which market groups may ring a phone. Empty means every one of them,
    # which is what the file said before this existed -- so an unset block
    # keeps the old behaviour rather than silently muting anything.
    push_markets: tuple[str, ...] = ()

    def pushes(self, horizon_key: str, markets: tuple[str, ...] = ()) -> bool:
        """Whether this signal may interrupt someone.

        Both filters have to pass. `markets` is a ticker's own list and a
        ticker can be in several, so one allowed market is enough -- BNTX is
        tagged europe and nasdaq, and muting europe should not silence it if
        nasdaq is still on.
        """
        if horizon_key not in self.push_horizons:
            return False
        if not self.push_markets:
            return True
        return any(m in self.push_markets for m in markets)


@dataclass(frozen=True)
class ScoringConfig:
    """How much each factor counts towards the conviction score.

    Re-weighting is a config edit, and the weights used are stamped into every
    journal row -- so a re-weighting shows up in the measured results rather
    than quietly rewriting history.

    The defaults are the existing rule expressed as numbers, not a new opinion:
    fair value is the thesis and carries the most, the two quality checks are
    worth about half of it each, and the pattern itself carries least because
    every signal on the page has already completed one. Nothing here is
    measured yet -- that is what shadow mode is for.
    """

    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def weight(self, key: str) -> float:
        return float(self.weights.get(key, 0.0))


DEFAULT_WEIGHTS: dict[str, float] = {
    "valuation": 3.0,
    "ruleone": 2.0,
    "growth": 1.5,
    "earnings": 1.0,
    "pattern": 1.0,
}


@dataclass(frozen=True)
class CryptoConfig:
    """The gate that grades a crypto signal, in place of a fair value.

    Two legs, configured differently because they answer different questions.
    `ath_floor` is a fact about the asset -- how far below its record it trades
    -- so it is one global number no holding period changes. The recent leg is a
    fact about the trade, so it reuses the horizon's own margin: 10% below the
    six-month high on the hourly chart, 50% on the weekly one, exactly as the
    equity gate scales its discount to fair value.

    `min_recent_bars` refuses to grade an asset whose recent window is too
    short. Without it a listing three weeks old would be measured against three
    weeks of history while the card said "6-month high".
    """

    ath_floor: float = DEFAULT_ATH_FLOOR
    recent_window_bars: int = RECENT_WINDOW_BARS
    min_recent_bars: int = 120
    enabled: bool = True


@dataclass(frozen=True)
class StrategiesConfig:
    """Exit rules to measure the recorded signals under.

    Adding a variant is a config edit, not a code change -- which is the point:
    the comparison is only useful if trying a fifth rule costs nothing.

    Keep the list short. Every extra variant tested against the same patterns
    raises the chance that the winner won by luck, and there is no correction
    for that here beyond restraint.
    """

    variants: tuple = ()

    def variant(self, key: str):
        for v in self.variants:
            if v.key == key:
                return v
        raise KeyError(f"no strategy {key!r}")


DEFAULT_STRATEGIES = (
    # The two the comparison was asked for. Both cut at -5%; they differ only
    # in what they ask for on the upside, which is the cleanest possible A/B --
    # any difference in the result is the take-profit and nothing else.
    dict(key="swing", label="Swing +3/-5", take_profit=3.0, stop_loss=5.0,
         max_bars=20,
         note="a shorter rebound trade: takes what it can get, three weeks"),
    dict(key="hold", label="Hold +5/-5", take_profit=5.0, stop_loss=5.0,
         max_bars=60,
         note="a larger move over a longer horizon: a quarter to be right"),
)


@dataclass(frozen=True)
class Config:
    tickers: list[Ticker]
    rsi: RsiConfig
    signal: SignalConfig
    storage: StorageConfig
    morningstar: MorningstarConfig
    dashboard: DashboardConfig
    horizons: tuple[Horizon, ...] = DEFAULT_HORIZONS
    scoring: ScoringConfig = field(default_factory=lambda: ScoringConfig())
    notify: NotifyConfig = field(default_factory=lambda: NotifyConfig())
    strategies: StrategiesConfig = field(default_factory=lambda: StrategiesConfig())
    crypto: CryptoConfig = field(default_factory=lambda: CryptoConfig())
    source_path: Path = field(default=DEFAULT_CONFIG)

    def ticker(self, symbol: str) -> Ticker:
        for t in self.tickers:
            if t.symbol.upper() == symbol.upper():
                return t
        raise KeyError(f"{symbol!r} is not in {self.source_path.name}")

    def horizon(self, key: str) -> Horizon:
        for h in self.horizons:
            if h.key == key:
                return h
        available = ", ".join(h.key for h in self.horizons)
        raise KeyError(f"{key!r} is not a configured horizon (have: {available})")

    def tickers_in(self, market: str) -> list[Ticker]:
        return [t for t in self.tickers if market in t.markets]

    @property
    def active_markets(self) -> tuple[str, ...]:
        """Markets that actually have tickers, in MARKETS order.

        Filtered rather than hardcoded so an empty group never renders a
        dashboard tab that shows nothing when clicked.
        """
        return tuple(m for m in MARKETS if self.tickers_in(m))


def _resolve(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def load_config(path: str | Path | None = None) -> Config:
    """Read config.yaml, validating the values a typo would otherwise hide."""
    config_path = Path(path) if path else DEFAULT_CONFIG
    if not config_path.exists():
        raise FileNotFoundError(f"No config file at {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}

    tickers = []
    for entry in raw.get("tickers", []):
        missing = {"symbol", "tradingview"} - set(entry)
        if missing:
            raise ValueError(f"Ticker {entry!r} is missing: {', '.join(sorted(missing))}")
        raw_markets = entry.get("markets", []) or []
        if isinstance(raw_markets, str):
            raw_markets = [raw_markets]
        markets = tuple(str(m).strip().lower() for m in raw_markets)
        unknown = set(markets) - set(MARKETS)
        if unknown:
            raise ValueError(
                f"Ticker {entry['symbol']!r} lists unknown market(s) "
                f"{sorted(unknown)}; valid: {', '.join(MARKETS)}"
            )
        slug = str(entry.get("morningstar", "") or "").strip("/")
        # A missing slug has a real consequence -- the ticker can never earn a
        # rocket, because `is_strong` requires a valuation -- so it has to be
        # deliberate. Crypto is the case where it is; on an equity it is a typo,
        # and one that would quietly downgrade the stock forever.
        if not slug and "crypto" not in markets:
            raise ValueError(
                f"Ticker {entry['symbol']!r} has no morningstar slug. Only "
                f"crypto tickers may omit it (they have no fair value); an "
                f"equity without one could never be a strong buy."
            )
        tickers.append(
            Ticker(
                symbol=str(entry["symbol"]).upper(),
                tradingview=str(entry["tradingview"]),
                morningstar=slug,
                yahoo=str(entry.get("yahoo", "") or ""),
                currency=str(entry.get("currency", "USD")).upper(),
                markets=markets,
            )
        )
    if not tickers:
        raise ValueError(f"No tickers configured in {config_path}")

    rsi_raw = raw.get("rsi", {})
    rsi = RsiConfig(
        period=int(rsi_raw.get("period", 14)),
        threshold=float(rsi_raw.get("threshold", 30)),
        overbought=float(rsi_raw.get("overbought", 70)),
        interval=str(rsi_raw.get("interval", "1D")),
    )
    if rsi.overbought <= rsi.threshold:
        raise ValueError(
            f"rsi.overbought ({rsi.overbought:g}) must be above rsi.threshold "
            f"({rsi.threshold:g}) -- they are the two ends of the same scale"
        )
    if rsi.period < 2:
        raise ValueError("rsi.period must be at least 2")
    # Backfill can compute any period locally, but the daily live reading comes
    # from TradingView, which only publishes fixed-period RSI fields. Allowing a
    # period it doesn't serve would silently mix two different indicators in one
    # series, so reject it up front rather than at the first run.
    from .tradingview import SUPPORTED_LIVE_PERIODS

    if rsi.period not in SUPPORTED_LIVE_PERIODS:
        raise ValueError(
            f"rsi.period must be one of {SUPPORTED_LIVE_PERIODS} — TradingView only "
            f"publishes those, and backfilled history has to match the live readings."
        )

    sig_raw = raw.get("signal", {})
    signal = SignalConfig(
        window_days=int(sig_raw.get("window_days", 14)),
        window_unit=str(sig_raw.get("window_unit", "calendar")),
        valuation_rule=str(sig_raw.get("valuation_rule", "fair_value_below_price")),
        fire_without_valuation=bool(sig_raw.get("fire_without_valuation", False)),
    )
    if signal.valuation_rule not in VALUATION_RULES:
        raise ValueError(
            f"signal.valuation_rule must be one of {VALUATION_RULES}, got {signal.valuation_rule!r}"
        )
    if signal.window_unit not in WINDOW_UNITS:
        raise ValueError(
            f"signal.window_unit must be one of {WINDOW_UNITS}, got {signal.window_unit!r}"
        )
    if signal.window_days < 1:
        raise ValueError("signal.window_days must be at least 1")

    store_raw = raw.get("storage", {})
    storage = StorageConfig(
        database=_resolve(store_raw.get("database", "data/screener.db")),
        csv_dir=_resolve(store_raw.get("csv_dir", "data")),
        fair_values=_resolve(store_raw.get("fair_values", "fair_values.yaml")),
        notifications=_resolve(store_raw.get("notifications", "notifications.json")),
        recommendations=_resolve(store_raw.get("recommendations", "recommendations.csv")),
    )

    ms_raw = raw.get("morningstar", {})
    morningstar = MorningstarConfig(
        state_file=_resolve(ms_raw.get("state_file", "auth/morningstar_state.json")),
        page_timeout=int(ms_raw.get("page_timeout", 45)),
        debug_on_failure=bool(ms_raw.get("debug_on_failure", True)),
        headless=bool(ms_raw.get("headless", False)),
    )

    dash_raw = raw.get("dashboard", {})
    dashboard = DashboardConfig(
        output=_resolve(dash_raw.get("output", "data/dashboard.html")),
        chart_days=int(dash_raw.get("chart_days", 90)),
        site_url=str(dash_raw.get("site_url", "")).rstrip("/"),
    )
    if dashboard.chart_days < 2:
        raise ValueError("dashboard.chart_days must be at least 2")

    horizons = _load_horizons(raw.get("horizons", {}) or {})
    scoring = _load_scoring(raw.get("scoring", {}) or {})
    notify = _load_notify(raw.get("notify", {}) or {}, horizons)
    strategies = _load_strategies(raw.get("strategies", None))
    crypto = _load_crypto(raw.get("crypto", {}) or {})

    return Config(
        tickers=tickers,
        scoring=scoring,
        notify=notify,
        strategies=strategies,
        crypto=crypto,
        rsi=rsi,
        signal=signal,
        storage=storage,
        morningstar=morningstar,
        dashboard=dashboard,
        horizons=horizons,
        source_path=config_path,
    )


def _load_strategies(raw) -> StrategiesConfig:
    """Read the `strategies:` block, falling back to the two built-in variants.

    An omitted block gives the defaults; an explicitly empty one gives none,
    which is how the comparison is turned off without deleting the code.
    """
    from .strategies import Strategy

    entries = DEFAULT_STRATEGIES if raw is None else (raw or ())
    variants = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError(f"strategies entries must be mappings, got {item!r}")
        missing = {"key", "take_profit", "stop_loss"} - set(item)
        if missing:
            raise ValueError(
                f"strategy {item.get('key', '?')!r} is missing "
                f"{', '.join(sorted(missing))}"
            )
        key = str(item["key"])
        if key in seen:
            raise ValueError(f"duplicate strategy key {key!r}")
        seen.add(key)

        take, stop = float(item["take_profit"]), float(item["stop_loss"])
        # Both must be positive: a "stop_loss: -5" reads naturally but would
        # invert the comparison silently, stopping out every winner.
        if take <= 0 or stop <= 0:
            raise ValueError(
                f"strategy {key!r}: take_profit and stop_loss are positive "
                f"percentages in both directions (got {take}, {stop})"
            )
        bars = int(item.get("max_bars", 60))
        if bars < 1:
            raise ValueError(f"strategy {key!r}: max_bars must be at least 1")

        variants.append(Strategy(
            key=key,
            label=str(item.get("label", key)),
            take_profit=take, stop_loss=stop, max_bars=bars,
            note=str(item.get("note", "")),
        ))
    return StrategiesConfig(variants=tuple(variants))


def _load_crypto(raw: dict) -> CryptoConfig:
    """Read the `crypto:` block, keeping every default the file doesn't set.

    An unknown key is an error. These numbers decide which crypto signals earn
    a rocket, and a typo like `ath_floor_pct:` would silently leave the default
    in place while the file appeared to say otherwise.
    """
    known = {"ath_floor", "recent_window_bars", "min_recent_bars", "enabled"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"crypto has no setting {', '.join(sorted(unknown))!r} — "
            f"valid: {', '.join(sorted(known))}"
        )

    floor = float(raw.get("ath_floor", DEFAULT_ATH_FLOOR))
    # A fraction, like every other margin in this file. 50 would read as
    # "5000% below the all-time high" and silently gate everything out.
    if not 0.0 <= floor < 1.0:
        raise ValueError(
            f"crypto.ath_floor is a fraction between 0 and 1 (0.5 = 50% below "
            f"the all-time high); got {floor}"
        )
    window = int(raw.get("recent_window_bars", RECENT_WINDOW_BARS))
    min_bars = int(raw.get("min_recent_bars", 120))
    if window < 1:
        raise ValueError("crypto.recent_window_bars must be at least 1")
    if min_bars > window:
        raise ValueError(
            f"crypto.min_recent_bars ({min_bars}) exceeds recent_window_bars "
            f"({window}), so nothing could ever qualify"
        )
    return CryptoConfig(
        ath_floor=floor,
        recent_window_bars=window,
        min_recent_bars=min_bars,
        enabled=bool(raw.get("enabled", True)),
    )


def _load_notify(raw: dict, horizons) -> NotifyConfig:
    """Read the `notify:` block, checking the timeframes actually exist.

    A typo here is silent in the worst way -- `notify.push_horizons: [4hr]`
    would simply stop every phone alert, and nothing would look broken until
    someone noticed the quiet.
    """
    given = raw.get("push_horizons", None)
    if given is None:
        keys = list(DEFAULT_PUSH_HORIZONS)
    else:
        if isinstance(given, str):
            given = [given]
        keys = [str(k) for k in given]
        known = {h.key for h in horizons}
        unknown = [k for k in keys if k not in known]
        if unknown:
            raise ValueError(
                f"notify.push_horizons names no timeframe {', '.join(unknown)!r} — "
                f"known timeframes are {', '.join(sorted(known))}"
            )

    # Same failure mode as the timeframes, and the same answer: a typo here
    # would silently mute a whole market and nothing would look broken.
    given_markets = raw.get("push_markets", None)
    if given_markets is None:
        markets: list[str] = []
    else:
        if isinstance(given_markets, str):
            given_markets = [given_markets]
        markets = [str(m).strip().lower() for m in given_markets]
        unknown_markets = [m for m in markets if m not in MARKETS]
        if unknown_markets:
            raise ValueError(
                f"notify.push_markets names no market "
                f"{', '.join(unknown_markets)!r} — known markets are "
                f"{', '.join(MARKETS)}"
            )

    return NotifyConfig(
        push_horizons=tuple(dict.fromkeys(keys)),
        push_markets=tuple(dict.fromkeys(markets)),
    )


def _load_scoring(raw: dict) -> ScoringConfig:
    """Read the `scoring:` block, keeping every default the file doesn't set.

    An unknown key is an error rather than a shrug. A weight is invisible in the
    output -- a typo like `valuations:` would silently drop the heaviest factor
    to zero and the page would carry on looking plausible.
    """
    weights = dict(DEFAULT_WEIGHTS)
    given = raw.get("weights", {}) or {}
    unknown = set(given) - set(DEFAULT_WEIGHTS)
    if unknown:
        known = ", ".join(sorted(DEFAULT_WEIGHTS))
        raise ValueError(
            f"scoring.weights has no factor {', '.join(sorted(unknown))!r} — "
            f"known factors are {known}"
        )
    for key, value in given.items():
        weight = float(value)
        if weight < 0:
            raise ValueError(f"scoring.weights.{key} cannot be negative")
        weights[key] = weight
    if not any(weights.values()):
        raise ValueError("scoring.weights cannot all be zero")
    return ScoringConfig(weights=weights)


def _load_horizons(raw: dict) -> tuple[Horizon, ...]:
    """Apply per-horizon overrides on top of the built-in defaults.

    Only the three tunables are overridable. The interval codes are not: they
    are what the two data sources actually accept, so a typo there would fail
    at fetch time rather than at load time -- exactly the kind of thing this
    loader exists to catch early.
    """
    unknown = set(raw) - {h.key for h in DEFAULT_HORIZONS}
    if unknown:
        valid = ", ".join(h.key for h in DEFAULT_HORIZONS)
        raise ValueError(f"Unknown horizon(s) {sorted(unknown)} in config; valid: {valid}")

    out = []
    for base in DEFAULT_HORIZONS:
        over = raw.get(base.key, {}) or {}
        window_days = int(over.get("window_days", base.window_days))
        margin = float(over.get("margin", base.margin))
        leverage = int(over.get("leverage", base.leverage))
        if window_days < 1:
            raise ValueError(f"horizons.{base.key}.window_days must be at least 1")
        if margin < 0:
            raise ValueError(f"horizons.{base.key}.margin must not be negative")
        if leverage < 1:
            raise ValueError(f"horizons.{base.key}.leverage must be at least 1")
        out.append(
            Horizon(
                key=base.key, label=base.label, tv_interval=base.tv_interval,
                yahoo_interval=base.yahoo_interval, yahoo_range=base.yahoo_range,
                window_days=window_days, margin=margin, leverage=leverage,
                # Carried from the built-in: bar_hours describes what the
                # timeframe *is*, not a preference, so overriding the tunables
                # in config.yaml must not silently reset every bar to a day.
                intraday=base.intraday, bar_hours=base.bar_hours,
            )
        )
    return tuple(out)
