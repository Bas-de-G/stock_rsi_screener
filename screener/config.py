"""Configuration loading.

Credentials never live here — they come from the environment (see .env.example).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

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
MARKETS = ("sp500", "nasdaq", "europe", "asia", "penny")
MARKET_LABELS = {
    "sp500": "S&P 500",
    "nasdaq": "NASDAQ",
    "europe": "Europe",
    "asia": "Asia",
    "penny": "Under $10",
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
    morningstar: str
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
    def morningstar_url(self) -> str:
        return f"https://www.morningstar.com/stocks/{self.morningstar}/quote"


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


@dataclass(frozen=True)
class Config:
    tickers: list[Ticker]
    rsi: RsiConfig
    signal: SignalConfig
    storage: StorageConfig
    morningstar: MorningstarConfig
    dashboard: DashboardConfig
    horizons: tuple[Horizon, ...] = DEFAULT_HORIZONS
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
        missing = {"symbol", "tradingview", "morningstar"} - set(entry)
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
        tickers.append(
            Ticker(
                symbol=str(entry["symbol"]).upper(),
                tradingview=str(entry["tradingview"]),
                morningstar=str(entry["morningstar"]).strip("/"),
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

    return Config(
        tickers=tickers,
        rsi=rsi,
        signal=signal,
        storage=storage,
        morningstar=morningstar,
        dashboard=dashboard,
        horizons=horizons,
        source_path=config_path,
    )


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
