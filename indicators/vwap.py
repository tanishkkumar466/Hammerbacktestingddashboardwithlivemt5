"""Session-anchored VWAP (resets each calendar day, like TradingView's default VWAP).

Uses the volume column when present (MT5 tick_volume in live, CSV volume in
backtest); bars with no volume fall back to weight=1 so VWAP is still defined.
"""

from typing import List, Optional

import numpy as np


def _session_start_indices(timestamps: Optional[List], n: int) -> np.ndarray:
    """Index of the first bar of each bar's session (calendar day)."""
    if timestamps is None or len(timestamps) != n:
        return np.zeros(n, dtype=np.int64)  # one big session (old behavior)
    dates = []
    for ts in timestamps:
        d = getattr(ts, "date", None)
        dates.append(d() if callable(d) else ts)
    idx = np.arange(n, dtype=np.int64)
    new_session = np.ones(n, dtype=bool)
    new_session[1:] = np.array([dates[i] != dates[i - 1] for i in range(1, n)])
    starts = np.where(new_session, idx, 0)
    return np.maximum.accumulate(starts)


def compute_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: Optional[np.ndarray] = None,
    timestamps: Optional[List] = None,
) -> np.ndarray:
    n = len(close)
    if n == 0:
        return np.array([])

    typical = (high + low + close) / 3.0
    if volume is None or len(volume) != n:
        vol = np.ones(n, dtype=float)
    else:
        vol = np.maximum(np.asarray(volume, dtype=float), 0.0)
        vol[vol <= 0] = 1.0

    cum_vol = np.cumsum(vol)
    cum_tp_vol = np.cumsum(typical * vol)

    # Subtract the running totals up to the start of each session so the
    # average restarts every day (session-anchored, like TradingView).
    session_start = _session_start_indices(timestamps, n)
    base_vol = np.where(session_start > 0, cum_vol[session_start - 1], 0.0)
    base_tp = np.where(session_start > 0, cum_tp_vol[session_start - 1], 0.0)

    sess_vol = cum_vol - base_vol
    sess_tp = cum_tp_vol - base_tp
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(sess_vol > 0, sess_tp / sess_vol, typical)
    return vwap
