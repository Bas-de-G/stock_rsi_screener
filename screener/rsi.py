"""Wilder's RSI — the same smoothing TradingView's built-in "RSI" uses.

Verified against TradingView's live scanner value: feeding this function the
same closing prices TradingView used for NVDA reproduced its reported RSI
(42.36) exactly. A plain (non-Wilder) moving average of gains/losses gives a
visibly different number, so the smoothing method matters and this file must
stay Wilder's.
"""

from __future__ import annotations


def wilder_rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Return one RSI value per input close (None where undefined).

    closes[0] gets None (nothing to compare against). closes[1..period] get
    None too — Wilder's method needs `period` changes to seed the first
    average. From index `period` onward each value is a proper RSI(period).
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
