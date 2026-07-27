"""Optional buy-signal notifications.

Set SCREENER_WEBHOOK_URL in .env to a Slack or Discord incoming-webhook URL
(both accept a JSON body with a "text" field) and fired signals get posted
there. Unset, everything still lands in SQLite, the CSVs, and stdout.
"""

from __future__ import annotations

import os

import requests

from .storage import Signal


def format_signal(sig: Signal, rule_description: str) -> str:
    lines = [
        f"BUY SIGNAL — {sig.symbol}",
        f"  RSI crossed up on {sig.up1_date}, dipped {sig.down_date}, crossed up again {sig.up2_date}",
    ]
    if sig.price is not None and sig.fair_value is not None:
        lines.append(f"  Price {sig.price:,.2f} vs fair value {sig.fair_value:,.2f}")
    lines.append(f"  Valuation gate: {rule_description}")
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
