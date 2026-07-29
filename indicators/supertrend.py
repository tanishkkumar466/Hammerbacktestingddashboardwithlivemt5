"""SuperTrend line (vectorized on OHLC arrays)."""

from typing import Tuple

import numpy as np


def compute_supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (supertrend_line, direction) where direction is +1 bullish (green), -1 bearish (red).
    NaN until enough bars for ATR.
    """
    n = len(close)
    if n == 0:
        return np.array([]), np.array([])

    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    atr = np.full(n, np.nan)
    if n >= atr_period:
        atr[atr_period - 1] = np.mean(tr[:atr_period])
        for i in range(atr_period, n):
            atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    final_upper = np.copy(basic_upper)
    final_lower = np.copy(basic_lower)
    st = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)

    for i in range(1, n):
        if np.isnan(atr[i]):
            continue
        # First bar after ATR warmup has NaN previous bands — start fresh
        # from the basic bands (otherwise NaN propagates through the whole
        # series and the SuperTrend filter silently never fires).
        if np.isnan(final_upper[i - 1]) or basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if np.isnan(final_lower[i - 1]) or basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if direction[i - 1] == -1:
            if close[i] > final_upper[i]:
                direction[i] = 1
            else:
                direction[i] = -1
        else:
            if close[i] < final_lower[i]:
                direction[i] = -1
            else:
                direction[i] = 1

        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return st, direction
