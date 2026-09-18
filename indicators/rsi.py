"""Wilder RSI on close (TradingView / MT5 style)."""

import numpy as np

DEFAULT_PERIOD = 14
MIN_PERIOD = 2
MAX_PERIOD = 500


def coerce_rsi_period(period, default: int = DEFAULT_PERIOD) -> int:
    try:
        p = int(period)
    except (TypeError, ValueError):
        return default
    if p != p:
        return default
    return max(MIN_PERIOD, min(MAX_PERIOD, p))


def coerce_rsi_level(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return max(0.0, min(100.0, v))


def compute_rsi(close: np.ndarray, period: int = DEFAULT_PERIOD) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan)
    period = coerce_rsi_period(period)
    if n < period + 1:
        return out

    delta = np.diff(close.astype(float))
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    def _rsi(ag: float, al: float) -> float:
        if al <= 0.0 and ag <= 0.0:
            return 50.0
        if al <= 0.0:
            return 100.0
        if ag <= 0.0:
            return 0.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi(avg_gain, avg_loss)
    return out
