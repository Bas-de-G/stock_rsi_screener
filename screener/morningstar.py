"""Morningstar price + fair value, via a logged-in browser session.

Why a browser at all: the fair value estimate is subscriber-only (it's the
padlocked "Price vs Fair Value" card on the quote page). Morningstar's own
JSON API refuses unauthenticated calls, so there is no key-free shortcut the
way there is for the RSI.

How the login works, and why it never touches this repo:

  `python -m screener.cli login` opens a real Chromium window at the
  Morningstar sign-in page and simply waits. Whoever owns the account types
  their own password into that window. When the login completes, Playwright
  saves the resulting *session cookies* to auth/morningstar_state.json, which
  is gitignored. Later runs reuse that file and never see a password.

Extraction runs three ways, most reliable first, because a scraped layout is
a moving target:

  1. Morningstar's own JSON, captured off the network as the page loads.
  2. The rendered "Price vs Fair Value" card, read by its labels.
  3. A regex over the page text.

If all three miss, the page HTML and a screenshot are written to debug/ so
the selectors can be repaired against what the site actually served.

Headless is off by default (`MorningstarConfig.headless = False`). A headless
scrape has been observed to trip Morningstar's AWS WAF bot-protection — served
as a "Let's confirm you are human" CAPTCHA page in place of the quote page —
even with a perfectly valid session cookie. The interactive `login` flow's
visible browser gets through, so scraping mirrors that rather than trying to
be invisible.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, MorningstarConfig, Ticker
from .storage import Valuation

SIGNIN_URL = "https://www.morningstar.com/sign-in"

# Keys Morningstar uses across its SAL endpoints. Checked in order.
_FAIR_VALUE_KEYS = ("fairValue", "fairValueEstimate", "fvEstimate", "qualFairValue", "fairValueUSD")
_PRICE_KEYS = ("lastPrice", "closePrice", "regularMarketLastPrice", "last", "price")

# Not just dollars: Amsterdam quotes in euros and London in pounds/pence, and
# a regex that hard-requires "$" silently returns None for every one of them.
_CURRENCY = r"[$€£]"
_MONEY = rf"{_CURRENCY}?\s*([0-9][0-9,]*\.?[0-9]*)"


class MorningstarError(RuntimeError):
    """Raised when Morningstar data can't be read (login expired, layout changed…)."""


class AuthenticationError(MorningstarError):
    """Raised specifically when the saved session is missing or no longer valid."""


class BotChallengeError(MorningstarError):
    """Raised when Morningstar's bot-protection intercepts the request.

    Distinct from `AuthenticationError` on purpose: this is not a login
    problem. The saved session cookie can be perfectly valid while an AWS WAF
    rule still serves a "Let's confirm you are human" CAPTCHA page instead of
    the quote page, because what's being fingerprinted is the automated
    browser, not the account.
    """


# Markers from the AWS WAF challenge page actually served to a headless
# session (captured via `_dump_debug` and inspected directly). These come from
# `page.inner_text("body")` — the *rendered* text — so markers that only exist
# in <script src> URLs (e.g. "awswaf.com") won't match and aren't listed here.
_BOT_CHALLENGE_MARKERS = (
    "let's confirm you are human",
    "verify you are human",
    "you are not a bot",
)


def _looks_like_bot_challenge(page_text: str) -> bool:
    lowered = page_text.lower()
    return any(marker in lowered for marker in _BOT_CHALLENGE_MARKERS)


@dataclass
class ScrapeResult:
    symbol: str
    price: float | None
    fair_value: float | None
    fair_value_date: str | None = None
    uncertainty: str | None = None
    moat: str | None = None
    price_source: str = "morningstar"
    method: str = ""

    @property
    def complete(self) -> bool:
        return self.price is not None and self.fair_value is not None


def _launch_kwargs() -> dict:
    """Chromium launch args. Honours PLAYWRIGHT_CHROMIUM_PATH when the
    bundled browser isn't where Playwright expects it."""
    import os

    kwargs: dict = {}
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if explicit:
        kwargs["executable_path"] = explicit
    return kwargs


def save_login_session(config: Config, timeout_minutes: int = 10) -> Path:
    """Open a visible browser, wait for a human to sign in, save the session.

    Deliberately interactive: no password is ever read from a file, typed by
    this code, or stored anywhere. Only the resulting cookies are saved.
    """
    from playwright.sync_api import sync_playwright

    state_file = config.morningstar.state_file
    state_file.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, **_launch_kwargs())
        context = browser.new_context()
        page = context.new_page()
        page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60_000)

        print("\n  A browser window has opened on the Morningstar sign-in page.")
        print("  Sign in there (the account holder should type their own password).")
        print("  Complete any 2-factor prompt as well.")
        print(f"\n  Waiting up to {timeout_minutes} minutes, then saving the session...\n")

        deadline = dt.datetime.now() + dt.timedelta(minutes=timeout_minutes)
        signed_in = False
        while dt.datetime.now() < deadline:
            page.wait_for_timeout(2000)
            if "sign-in" not in page.url and "login" not in page.url.lower():
                # Give the post-login redirect a moment to settle cookies.
                page.wait_for_timeout(3000)
                signed_in = True
                break

        if not signed_in:
            browser.close()
            raise AuthenticationError(
                "Timed out waiting for sign-in. Re-run `login` and finish signing in."
            )

        context.storage_state(path=str(state_file))
        browser.close()

    state_file.chmod(0o600)
    return state_file


def _require_session(ms_config: MorningstarConfig) -> None:
    if not ms_config.state_file.exists():
        raise AuthenticationError(
            f"No saved Morningstar session at {ms_config.state_file}. "
            "Run: python -m screener.cli login"
        )


def _scrape_on_page(page, ticker: Ticker, ms_config: MorningstarConfig) -> ScrapeResult:
    """Read one ticker using an already-open page.

    Split out from `scrape_ticker` so the single-ticker and batch paths share
    exactly one copy of the navigate-and-extract logic — a selector repair only
    ever has to be made in one place.
    """
    captured: list[dict] = []

    def on_response(response):
        url = response.url
        if "morningstar.com" not in url:
            return
        if not any(tag in url for tag in ("sal-service", "api-global", "valuation", "priceFairValue")):
            return
        try:
            if "json" in (response.headers.get("content-type") or ""):
                captured.append(response.json())
        except Exception:
            pass  # A response we can't parse is simply not a source.

    page.on("response", on_response)
    try:
        page.goto(
            ticker.morningstar_url,
            wait_until="domcontentloaded",
            timeout=ms_config.page_timeout * 1000,
        )
        # The valuation card renders client-side after its XHR returns.
        page.wait_for_timeout(6000)
        try:
            page.wait_for_selector("text=Fair Value", timeout=ms_config.page_timeout * 1000)
        except Exception:
            pass  # Fall through to the other strategies and the debug dump.

        page_text = page.inner_text("body")

        # Checked before extraction, not after: a challenge page has no Fair
        # Value card to find, so letting the three extraction strategies run
        # first only produces a confusing "fair_value=None" with no clue why.
        if _looks_like_bot_challenge(page_text):
            if ms_config.debug_on_failure:
                _dump_debug(page, ticker.symbol, captured)
            raise BotChallengeError(
                f"Morningstar's bot-protection (AWS WAF) challenged the request for "
                f"{ticker.symbol} instead of serving the page. Your session is not the "
                "problem — this is a fingerprinting check, not a login check. "
                f"{'Retrying headless will not help; headless=false is already set.' if not ms_config.headless else 'Set morningstar.headless: false in config.yaml and try again — the interactive login uses a visible browser and gets through.'}"
            )

        result = _extract(ticker.symbol, captured, page_text)

        if not result.complete and ms_config.debug_on_failure:
            _dump_debug(page, ticker.symbol, captured)

        if result.fair_value is None and _looks_logged_out(page_text):
            raise AuthenticationError(
                f"Morningstar returned a signed-out page for {ticker.symbol}. "
                "The saved session has expired — re-run: python -m screener.cli login"
            )
    finally:
        page.remove_listener("response", on_response)

    return result


def scrape_ticker(ticker: Ticker, ms_config: MorningstarConfig) -> ScrapeResult:
    """Read price + fair value for one ticker from its Morningstar quote page."""
    from playwright.sync_api import sync_playwright

    _require_session(ms_config)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=ms_config.headless, **_launch_kwargs())
        try:
            context = browser.new_context(storage_state=str(ms_config.state_file))
            page = context.new_page()
            return _scrape_on_page(page, ticker, ms_config)
        finally:
            browser.close()


# A price and a fair value for the same stock are the same kind of number, so
# they should land within an order of magnitude of each other. Anything wider is
# almost always a *unit* mismatch rather than a real valuation gap -- the live
# hazard being Rolls-Royce, quoted in pence on xlon, where a price read as GBX
# 1,413.60 against a fair value read as GBP 14.50 would sail through the gate
# and produce a confident, meaningless answer.
_RATIO_FLOOR, _RATIO_CEILING = 0.1, 10.0


class UnitMismatchError(MorningstarError):
    """Raised when price and fair value look like they're in different units."""


def check_units(result: ScrapeResult) -> None:
    """Reject a result whose price and fair value can't be in the same currency."""
    if result.price is None or result.fair_value is None:
        return
    ratio = result.price / result.fair_value
    if not _RATIO_FLOOR <= ratio <= _RATIO_CEILING:
        raise UnitMismatchError(
            f"{result.symbol}: price {result.price:,.2f} and fair value "
            f"{result.fair_value:,.2f} differ by {ratio:.1f}x — almost certainly a "
            f"currency/unit mismatch (pence vs pounds?), not a real gap. Not recorded."
        )


def scrape_many(
    tickers: list[Ticker],
    ms_config: MorningstarConfig,
    pause_range: tuple[float, float] = (3.0, 8.0),
    on_result=None,
) -> list[tuple[Ticker, ScrapeResult | None, str | None]]:
    """Read several tickers in one browser session.

    Returns a (ticker, result, error) triple per ticker, so one bad page doesn't
    discard the good ones alongside it. `result` is None exactly when `error` is
    set.

    Two deliberate choices:

    * **One browser, many pages.** `scrape_ticker` launches its own Chromium,
      which for 35 tickers means 35 process launches.
    * **A randomised pause between tickers.** Reading page after page as fast as
      Chromium can go is the pattern that gets a subscriber session flagged.
      This is a logged-in account on a paid product; pace it like a person.

    An expired session, or a bot-protection challenge, aborts the whole batch —
    both apply to the browser session as a whole, so every remaining ticker
    would fail identically, and 35 copies of the same error helps nobody.
    """
    import random
    import time

    from playwright.sync_api import sync_playwright

    _require_session(ms_config)
    out: list[tuple[Ticker, ScrapeResult | None, str | None]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=ms_config.headless, **_launch_kwargs())
        try:
            context = browser.new_context(storage_state=str(ms_config.state_file))
            for index, ticker in enumerate(tickers):
                if index:
                    time.sleep(random.uniform(*pause_range))
                page = context.new_page()
                try:
                    result = _scrape_on_page(page, ticker, ms_config)
                    check_units(result)
                    entry = (ticker, result, None)
                except (AuthenticationError, BotChallengeError):
                    raise  # every remaining ticker would fail the same way
                except MorningstarError as exc:
                    entry = (ticker, None, str(exc))
                finally:
                    page.close()
                out.append(entry)
                if on_result is not None:
                    on_result(*entry)
        finally:
            browser.close()

    return out


def _looks_logged_out(page_text: str) -> bool:
    lowered = page_text.lower()
    markers = ("sign in to unlock", "start a free trial", "subscribe to unlock", "sign up for free")
    return any(m in lowered for m in markers)


def _extract(symbol: str, captured: list[dict], page_text: str) -> ScrapeResult:
    """Try each extraction strategy in order of reliability."""
    result = ScrapeResult(symbol=symbol, price=None, fair_value=None)

    # Strategy 1 — Morningstar's own JSON.
    for payload in captured:
        if result.fair_value is None:
            fv = _deep_find_number(payload, _FAIR_VALUE_KEYS)
            if fv is not None:
                result.fair_value = fv
                result.method = "network-json"
        if result.price is None:
            px = _deep_find_number(payload, _PRICE_KEYS)
            if px is not None:
                result.price = px
                result.method = result.method or "network-json"

    # Strategy 2/3 — read the rendered card.
    if result.fair_value is None:
        fv = _fair_value_from_text(page_text)
        if fv is not None:
            result.fair_value = fv
            result.method = (result.method + "+text").lstrip("+")
    if result.price is None:
        px = _price_from_text(page_text)
        if px is not None:
            result.price = px
            result.method = (result.method + "+text").lstrip("+")

    result.uncertainty = _labelled_word(page_text, "Uncertainty")
    result.moat = _labelled_word(page_text, "Economic Moat")
    result.fair_value_date = _fair_value_date_from_text(page_text)
    return result


def _deep_find_number(obj, keys: tuple[str, ...]) -> float | None:
    """Walk a nested JSON structure looking for the first numeric value under any of `keys`."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                if key in node:
                    value = _coerce_number(node[key])
                    if value is not None:
                        return value
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _coerce_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def _fair_value_from_text(text: str) -> float | None:
    """Read the fair value out of the "Price vs Fair Value" card.

    Guards against the two nearby decoys on that card: the section heading
    "Price vs Fair Value", and the "1-Star Price"/"5-Star Price" rows.
    """
    for match in re.finditer(r"Fair Value", text):
        start = match.start()
        preceding = text[max(0, start - 12) : start]
        if "vs" in preceding:  # the "Price vs Fair Value" heading
            continue
        window = text[match.end() : match.end() + 60]
        money = re.search(_MONEY, window)
        if money:
            return _coerce_number(money.group(1))
    return None


def _price_from_text(text: str) -> float | None:
    """Read the *current* price — the large quote at the top of the page.

    Deliberately preferred over the "Price" row inside the Price vs Fair Value
    card: that row is the previous close Morningstar pins its own comparison to
    (on the IBM page it read $214.19 while the live quote was $217.24), and the
    current price is what's wanted here. The card row is the fallback.
    """
    header = re.search(rf"{_CURRENCY}\s*([0-9][0-9,]*\.[0-9]{{2}})", text)
    if header:
        value = _coerce_number(header.group(1))
        if value is not None:
            return value
    card = re.search(r"\bPrice\b\s*" + _MONEY, text)
    if card:
        return _coerce_number(card.group(1))
    return None


def _fair_value_date_from_text(text: str) -> str | None:
    match = re.search(
        rf"Fair Value\s*{_CURRENCY}?\s*[0-9,.]+\s*([A-Z][a-z]{{2}} \d{{1,2}}, \d{{4}})", text
    )
    return match.group(1) if match else None


def _labelled_word(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*([A-Za-z ]{{3,20}})", text)
    if not match:
        return None
    return match.group(1).strip().split("\n")[0][:20] or None


def _dump_debug(page, symbol: str, captured: list[dict]) -> None:
    """Persist what the site actually served, so selectors can be fixed."""
    debug_dir = Path(__file__).resolve().parent.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = debug_dir / f"{symbol}-{stamp}"
    try:
        base.with_suffix(".html").write_text(page.content())
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        if captured:
            base.with_suffix(".json").write_text(json.dumps(captured, indent=2)[:2_000_000])
        print(f"    [debug] extraction incomplete — dumped page to {base}.*")
    except Exception as exc:  # debugging must never break the run
        print(f"    [debug] could not write debug dump: {exc}")


def to_valuation(result: ScrapeResult, date: str) -> Valuation:
    if result.price is None or result.fair_value is None:
        raise MorningstarError(
            f"Incomplete Morningstar data for {result.symbol}: "
            f"price={result.price}, fair_value={result.fair_value}"
        )
    return Valuation(
        symbol=result.symbol,
        date=date,
        price=result.price,
        fair_value=result.fair_value,
        fair_value_date=result.fair_value_date,
        uncertainty=result.uncertainty,
        moat=result.moat,
    )
