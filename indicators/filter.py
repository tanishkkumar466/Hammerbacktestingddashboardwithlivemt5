"""
Apply indicator-based filters to logic.py TradeSignal objects after pattern detection.

Dashboard copy of these rules: indicators/registry.py → filter_rules on each entry.
Edit behavior here (_supertrend_passes, _vwap_passes); keep registry text in sync.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import polars as pl

import logic
from indicators.config import IndicatorCombineMode, IndicatorStackConfig
from indicators.supertrend import compute_supertrend
from indicators.vwap import compute_vwap


def _ts_lookup_keys(ts) -> List[object]:
    """Multiple keys for the same bar time (Polars vs MT5 datetime types)."""
    if ts is None:
        return [("none",)]
    keys: List[object] = [ts]
    if isinstance(ts, datetime):
        keys.append(("epoch", int(ts.timestamp())))
        keys.append(("iso", ts.replace(microsecond=0).isoformat()))
    elif hasattr(ts, "timestamp") and callable(ts.timestamp):
        try:
            keys.append(("epoch", int(ts.timestamp())))
        except (TypeError, ValueError, OSError):
            pass
    keys.append(("str", str(ts)))
    return keys


def _build_timestamp_index(timestamps: List) -> Dict[object, int]:
    idx_map: Dict[object, int] = {}
    for i, t in enumerate(timestamps):
        for key in _ts_lookup_keys(t):
            idx_map.setdefault(key, i)
    return idx_map


def _index_for_timestamp(timestamps: List, ts, idx_map: Optional[Dict[object, int]] = None) -> Optional[int]:
    if idx_map is None:
        idx_map = _build_timestamp_index(timestamps)
    for key in _ts_lookup_keys(ts):
        if key in idx_map:
            return idx_map[key]
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


def _compute_indicator_arrays(
    df: pl.DataFrame,
    stack: IndicatorStackConfig,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Optional[np.ndarray], List, np.ndarray, np.ndarray, np.ndarray]:
    high, low, close, vol, timestamps = _build_series(df)
    st_line, st_dir = compute_supertrend(
        high, low, close,
        atr_period=stack.supertrend.atr_period,
        multiplier=stack.supertrend.multiplier,
    )
    vwap = compute_vwap(high, low, close, vol, timestamps=timestamps)
    return high, low, close, vol, timestamps, st_line, st_dir, vwap


def _supertrend_passes(
    direction: logic.TradeDirection,
    close: float,
    st_line: float,
    st_dir: int,
) -> Tuple[bool, str]:
    """
    Direction-based rule, applied to EVERY trade (classic, inverted, doji):
      BUY  -> SuperTrend must be bullish (green) AND close above the line.
      SELL -> SuperTrend must be bearish (red)  AND close below the line.
    A SELL while SuperTrend is green is always rejected.
    """
    if np.isnan(st_line):
        return False, (
            "SuperTrend filter: not enough history to compute SuperTrend on this bar "
            "(increase live history bars or backtest data length)."
        )

    if st_dir not in (1, -1):
        return False, (
            f"SuperTrend filter: trend direction not ready (internal state={st_dir})."
        )

    if direction == logic.TradeDirection.BUY:
        if st_dir != 1:
            return False, (
                f"SuperTrend filter: BUY requires bullish (green) SuperTrend — computed state is "
                f"bearish here (line={st_line:.2f}, close={close:.2f})."
            )
        if close <= st_line:
            return False, (
                f"SuperTrend filter: BUY requires close above the SuperTrend line "
                f"(line={st_line:.2f}, close={close:.2f})."
            )
        return True, ""

    if direction == logic.TradeDirection.SELL:
        if st_dir != -1:
            return False, (
                f"SuperTrend filter: SELL requires bearish (red) SuperTrend — computed state is "
                f"bullish here (line={st_line:.2f}, close={close:.2f})."
            )
        if close >= st_line:
            return False, (
                f"SuperTrend filter: SELL requires close below the SuperTrend line "
                f"(line={st_line:.2f}, close={close:.2f})."
            )
        return True, ""

    return True, ""


def _vwap_passes(direction: logic.TradeDirection, close: float, vwap: float) -> Tuple[bool, str]:
    """
    Direction-based rule, applied to EVERY trade:
      BUY  -> close must be above VWAP.
      SELL -> close must be below VWAP.
    """
    if np.isnan(vwap):
        return False, "VWAP filter: VWAP not available on this bar."

    if direction == logic.TradeDirection.BUY and close <= vwap:
        return False, f"VWAP filter: BUY requires close above VWAP (VWAP={vwap:.2f}, close={close:.2f})."
    if direction == logic.TradeDirection.SELL and close >= vwap:
        return False, f"VWAP filter: SELL requires close below VWAP (VWAP={vwap:.2f}, close={close:.2f})."
    return True, ""


def _indicator_checks_at_index(
    direction: logic.TradeDirection,
    idx: int,
    close: np.ndarray,
    st_line: np.ndarray,
    st_dir: np.ndarray,
    vwap: np.ndarray,
    stack: IndicatorStackConfig,
) -> List[Tuple[bool, str]]:
    checks: List[Tuple[bool, str]] = []
    if idx < 0 or idx >= len(close):
        return [(False, f"Indicator filter: bar index {idx} out of range.")]

    c = float(close[idx])

    if stack.supertrend.enabled and stack.supertrend.apply_trade_filter:
        ok, reason = _supertrend_passes(
            direction, c, float(st_line[idx]), int(st_dir[idx]),
        )
        checks.append((ok, reason))

    if stack.vwap.enabled and stack.vwap.apply_trade_filter:
        ok, reason = _vwap_passes(direction, c, float(vwap[idx]))
        checks.append((ok, reason))

    return checks


def _combine_checks(stack: IndicatorStackConfig, checks: List[Tuple[bool, str]]) -> Tuple[bool, str]:
    if not checks:
        return True, ""
    if stack.combine_mode == IndicatorCombineMode.ALL:
        failed = [(ok, r) for ok, r in checks if not ok]
        if failed:
            return False, failed[0][1]
        return True, ""
    if any(ok for ok, _ in checks):
        return True, ""
    return False, checks[0][1] or "Indicator filter rejected trade."


def verify_signal_passes_indicators_at_bar(
    df: pl.DataFrame,
    bar_index: int,
    direction: logic.TradeDirection,
    stack: IndicatorStackConfig,
) -> Tuple[bool, str]:
    """
    Fail-closed gate: same rules as apply_indicator_filters on one bar index.
    Used by live trading before sending orders.
    """
    if not stack.enabled_indicator_ids():
        return True, ""

    _, _, close, _, _, st_line, st_dir, vwap = _compute_indicator_arrays(df, stack)
    checks = _indicator_checks_at_index(
        direction, bar_index, close, st_line, st_dir, vwap, stack,
    )
    ok, reason = _combine_checks(stack, checks)
    return ok, reason


def _mark_ignored(sig: logic.TradeSignal, reason: str) -> logic.TradeSignal:
    return logic.TradeSignal(
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
        ignore_reason=reason,
        pattern_variant=getattr(sig, "pattern_variant", None),
    )


def apply_indicator_filters(
    signals: List[logic.TradeSignal],
    df: pl.DataFrame,
    stack: IndicatorStackConfig,
) -> List[logic.TradeSignal]:
    """Returns the same list with ignored=True where indicator rules fail."""
    if not stack.enabled_indicator_ids():
        return signals

    _, _, close, _, timestamps, st_line, st_dir, vwap = _compute_indicator_arrays(df, stack)
    ts_index = _build_timestamp_index(timestamps)

    out: List[logic.TradeSignal] = []
    for sig in signals:
        if sig.ignored:
            out.append(sig)
            continue

        idx = _index_for_timestamp(timestamps, sig.hammer_candle.timestamp, ts_index)
        if idx is None:
            out.append(_mark_ignored(
                sig,
                "Indicator filter: could not align signal candle to price history "
                "(timestamp mismatch) — trade rejected for safety.",
            ))
            continue

        checks = _indicator_checks_at_index(
            sig.direction, idx, close, st_line, st_dir, vwap, stack,
        )
        if not checks:
            out.append(sig)
            continue

        ok, reason = _combine_checks(stack, checks)
        if not ok:
            out.append(_mark_ignored(sig, reason))
        else:
            out.append(sig)

    return out
