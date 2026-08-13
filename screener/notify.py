"""Optional buy-signal notifications.

Set SCREENER_WEBHOOK_URL in .env to a Slack or Discord incoming-webhook URL
(both accept a JSON body with a "text" field) and fired signals get posted
there. Unset, everything still lands in SQLite, the CSVs, and stdout.
"""

from __future__ import annotations

import os

import requests

from .signals import SELL
from .storage import Signal


def format_signal(sig: Signal, rule_description: str, horizon=None) -> str:
    # Announce the side it actually is. This read "BUY SIGNAL" for everything,
    # which was harmless while only buys existed and became a lie the day the
    # sell direction was added -- and sells outnumber buys most runs.
    side = "SELL SIGNAL" if sig.direction == SELL else "BUY SIGNAL"
    head = f"{side} — {sig.symbol}"
    if horizon is not None:
        head += f" [{horizon.label} chart]"
    lines = [
        head,
        f"  RSI crossed up on {sig.up1_date}, dipped {sig.down_date}, crossed up again {sig.up2_date}",
    ]
    if sig.price is not None and sig.fair_value is not None:
        lines.append(f"  Price {sig.price:,.2f} vs fair value {sig.fair_value:,.2f}")
    if sig.earnings_growth is not None:
        lines.append(f"  YoY EPS growth: {sig.earnings_growth:+.1f}%")
    lines.append(f"  Valuation gate: {rule_description}")
    if horizon is not None:
        lines.append(
            f"  Needs {horizon.margin_pct} headroom to fair value on this timeframe; "
            f"suggested leverage {horizon.leverage}x"
        )
    return "\n".join(lines)


def send_webhook(message: str) -> bool:
    """Post to the configured webhook. Returns False if none is configured."""
    url = os.environ.get("SCREENER_WEBHOOK_URL")
    if not url:
        return False
    try:
        response = requests.post(url, json={"text": message, "content": message}, timeout=15)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"  ! webhook failed: {exc}")
        return False


def format_strong_buy(symbol: str, discount,  price, fair_value, currency: str,
                horizon, threshold: float, url: str) -> str:
    """A newly fired strong buy, as a line worth interrupting someone for.

    Kept short on purpose. This is a push notification, not the dashboard --
    it needs to say which stock, how cheap, and where to look.
    """
    money = f"{price:,.2f}" if price is not None else "?"
    fair = f"{fair_value:,.2f}" if fair_value is not None else "?"
    ccy = "" if currency == "USD" else f" {currency}"
    return "\n".join([
        f"STRONG BUY 🚀 — {symbol}"
        + (f"  ({discount * 100:.0f}% below fair value)" if discount is not None else ""),
        f"  Second cross of {threshold:g} within the last {horizon.fresh_label}"
        f" on the {horizon.label} chart",
        f"  {money}{ccy} against a {fair}{ccy} fair value"
        f" · {horizon.leverage}x suggested",
        f"  {url}",
    ])
