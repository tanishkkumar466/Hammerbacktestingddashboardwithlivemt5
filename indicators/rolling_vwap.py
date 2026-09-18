"""Rolling VWAP over the last N bars (does not reset at the calendar day).

Typical price = (high + low + close) / 3, volume-weighted. Bars with no
volume use weight=1 so the line is still defined. The first (period - 1)
bars are NaN (warmup).
"""

from typing import Optional

import numpy as np

DEFAULT_PERIOD = 20
MIN_PERIOD = 2
MAX_PERIOD = 20_000


def coerce_rolling_period(period, default: int = DEFAULT_PERIOD) -> int:
    try:
        p = int(period)
    except (TypeError, ValueError):
        return default
    if p != p:  # NaN
        return default
    return max(MIN_PERIOD, min(MAX_PERIOD, p))


def compute_rolling_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: Optional[np.ndarray] = None,
    period: int = DEFAULT_PERIOD,
) -> np.ndarray:
    n = len(close)
    if n == 0:
        return np.array([])

    period = coerce_rolling_period(period)
    out = np.full(n, np.nan)
    typical = (high + low + close) / 3.0
    if volume is None or len(volume) != n:
        vol = np.ones(n, dtype=float)
    else:
        vol = np.maximum(np.asarray(volume, dtype=float), 0.0)
        vol[vol <= 0] = 1.0

    if n < period:
        return out

    tp_vol = typical * vol
    cs_tp = np.cumsum(tp_vol)
    cs_v = np.cumsum(vol)
    window_tp = np.empty(n, dtype=float)
    window_v = np.empty(n, dtype=float)
    window_tp[:period] = cs_tp[:period]
    window_v[:period] = cs_v[:period]
    window_tp[period:] = cs_tp[period:] - cs_tp[:-period]
    window_v[period:] = cs_v[period:] - cs_v[:-period]

    with np.errstate(divide="ignore", invalid="ignore"):
        rv = np.where(window_v > 0, window_tp / window_v, typical)
    out[period - 1:] = rv[period - 1:]
    return out
