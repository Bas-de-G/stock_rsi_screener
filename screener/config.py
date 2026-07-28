"""Configuration loading.

Credentials never live here — they come from the environment (see .env.example).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

VALUATION_RULES = ("fair_value_below_price", "price_below_fair_value")
WINDOW_UNITS = ("calendar", "trading")


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

    def __post_init__(self):
        if not self.yahoo:
            object.__setattr__(self, "yahoo", self.symbol)

    @property
    def morningstar_url(self) -> str:
        return f"https://www.morningstar.com/stocks/{self.morningstar}/quote"


@dataclass(frozen=True)
class RsiConfig:
    period: int = 14
    threshold: float = 30.0
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


@dataclass(frozen=True)
class DashboardConfig:
    output: Path
    chart_days: int = 90


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
    source_path: Path = field(default=DEFAULT_CONFIG)

    def ticker(self, symbol: str) -> Ticker:
        for t in self.tickers:
            if t.symbol.upper() == symbol.upper():
                return t
        raise KeyError(f"{symbol!r} is not in {self.source_path.name}")


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
        tickers.append(
            Ticker(
                symbol=str(entry["symbol"]).upper(),
                tradingview=str(entry["tradingview"]),
                morningstar=str(entry["morningstar"]).strip("/"),
                yahoo=str(entry.get("yahoo", "") or ""),
                currency=str(entry.get("currency", "USD")).upper(),
            )
        )
    if not tickers:
        raise ValueError(f"No tickers configured in {config_path}")

    rsi_raw = raw.get("rsi", {})
    rsi = RsiConfig(
        period=int(rsi_raw.get("period", 14)),
        threshold=float(rsi_raw.get("threshold", 30)),
        interval=str(rsi_raw.get("interval", "1D")),
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
    )
    if dashboard.chart_days < 2:
        raise ValueError("dashboard.chart_days must be at least 2")

    return Config(
        tickers=tickers,
        rsi=rsi,
        signal=signal,
        storage=storage,
        morningstar=morningstar,
        dashboard=dashboard,
        source_path=config_path,
    )
