"""
Apply indicator-based filters to logic.py TradeSignal objects after pattern detection.

Dashboard copy of these rules: indicators/registry.py → filter_rules on each entry.
Edit behavior here (_supertrend_passes, _vwap_passes); keep registry text in sync.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import polars as pl

import logic
from indicators.config import IndicatorCombineMode, IndicatorStackConfig
from indicators.supertrend import compute_supertrend
from indicators.vwap import compute_vwap


def _index_for_timestamp(timestamps: List, ts) -> Optional[int]:
    try:
        return timestamps.index(ts)
    except ValueError:
        return None


def _build_series(df: pl.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, List]:
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    vol = df["volume"].to_numpy() if "volume" in df.columns else None
    ts = df["datetime"].to_list()
    return high, low, close, vol, ts


def _supertrend_passes(
    variant: str,
    direction: logic.TradeDirection,
    close: float,
    st_line: float,
    st_dir: int,
) -> Tuple[bool, str]:
    if np.isnan(st_line):
        return True, ""

    if variant == "CLASSIC" and direction == logic.TradeDirection.BUY:
        if st_dir != 1:
            return False, "SuperTrend filter: classic BUY requires bullish (green) SuperTrend."
        if close <= st_line:
            return False, "SuperTrend filter: classic BUY requires close above SuperTrend line."
        return True, ""

    if variant == "INVERTED" and direction == logic.TradeDirection.SELL:
        if st_dir != -1:
            return False, "SuperTrend filter: inverted SELL requires bearish (red) SuperTrend."
        if close >= st_line:
            return False, "SuperTrend filter: inverted SELL requires close below SuperTrend line."
        return True, ""

    # Other variant/direction combos: no extra ST rule (pass)
    return True, ""


def _vwap_passes(variant: str, direction: logic.TradeDirection, close: float, vwap: float) -> Tuple[bool, str]:
    if np.isnan(vwap):
        return True, ""

    above = close > vwap
    below = close < vwap

    if variant == "CLASSIC":
        if direction == logic.TradeDirection.BUY:
            if not above:
                return False, "VWAP filter: classic hammer BUY requires close above VWAP."
            return True, ""
        return True, ""

    if variant == "INVERTED":
        if direction == logic.TradeDirection.SELL:
            if not below:
                return False, "VWAP filter: inverted hammer SELL requires close below VWAP."
            return True, ""
        if above:
            return False, "VWAP filter: inverted hammer above VWAP — no trade."
        return True, ""

    return True, ""


def apply_indicator_filters(
    signals: List[logic.TradeSignal],
    df: pl.DataFrame,
    stack: IndicatorStackConfig,
) -> List[logic.TradeSignal]:
    """Returns the same list with ignored=True where indicator rules fail."""
    if not stack.enabled_indicator_ids():
        return signals

    high, low, close, vol, timestamps = _build_series(df)
    st_line, st_dir = compute_supertrend(
        high, low, close,
        atr_period=stack.supertrend.atr_period,
        multiplier=stack.supertrend.multiplier,
    )
    vwap = compute_vwap(high, low, close, vol)

    out: List[logic.TradeSignal] = []
    for sig in signals:
        if sig.ignored:
            out.append(sig)
            continue

        idx = _index_for_timestamp(timestamps, sig.hammer_candle.timestamp)
        if idx is None:
            out.append(sig)
            continue

        variant = getattr(sig, "pattern_variant", None) or "CLASSIC"
        c = float(close[idx])
        checks: List[Tuple[bool, str]] = []

        if stack.supertrend.enabled and stack.supertrend.apply_trade_filter:
            ok, reason = _supertrend_passes(
                variant, sig.direction, c, float(st_line[idx]), int(st_dir[idx]),
            )
            checks.append((ok, reason))

        if stack.vwap.enabled and stack.vwap.apply_trade_filter:
            ok, reason = _vwap_passes(variant, sig.direction, c, float(vwap[idx]))
            checks.append((ok, reason))

        if not checks:
            out.append(sig)
            continue

        if stack.combine_mode == IndicatorCombineMode.ALL:
            failed = [(ok, r) for ok, r in checks if not ok]
            if failed:
                sig = logic.TradeSignal(
                    direction=sig.direction,
                    hammer_candle=sig.hammer_candle,
                    entry_candle=sig.entry_candle,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    risk=sig.risk,
                    rr_multiple=sig.rr_multiple,
                    target=sig.target,
                    timeframe=sig.timeframe,
                    ignored=True,
                    ignore_reason=failed[0][1],
                    pattern_variant=getattr(sig, "pattern_variant", None),
                )
        else:
            if not any(ok for ok, _ in checks):
                sig = logic.TradeSignal(
                    direction=sig.direction,
                    hammer_candle=sig.hammer_candle,
                    entry_candle=sig.entry_candle,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    risk=sig.risk,
                    rr_multiple=sig.rr_multiple,
                    target=sig.target,
                    timeframe=sig.timeframe,
                    ignored=True,
                    ignore_reason=checks[0][1] or "Indicator filter rejected trade.",
                    pattern_variant=getattr(sig, "pattern_variant", None),
                )

        out.append(sig)

    return out
