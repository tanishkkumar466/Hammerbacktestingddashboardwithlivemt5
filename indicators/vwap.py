"""Session-cumulative VWAP (uses volume column when present, else weight=1)."""

from typing import Optional

import numpy as np


def compute_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: Optional[np.ndarray] = None,
) -> np.ndarray:
    n = len(close)
    if n == 0:
        return np.array([])

    typical = (high + low + close) / 3.0
    if volume is None or len(volume) != n:
        vol = np.ones(n, dtype=float)
    else:
        vol = np.maximum(volume.astype(float), 0.0)
        vol[vol <= 0] = 1.0

    cum_vol = np.cumsum(vol)
    cum_tp_vol = np.cumsum(typical * vol)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(cum_vol > 0, cum_tp_vol / cum_vol, typical)
    return vwap
