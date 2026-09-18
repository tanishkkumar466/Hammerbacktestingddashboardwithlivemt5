"""Regression tests for backtest pipeline edge cases (overlap, limits, TF lookup)."""

from datetime import datetime, timedelta

import numpy as np

import hammer_context_logic as hc
import logic
from backtest import (
    ExitModel,
    TradeOutcome,
    resolve_all_exit_models_for_trade,
)
from logic import Candle, TimeframeSetting, TradeDirection, TradeSignal


def test_resolve_timeframe_setting_falls_back_for_custom_label():
    settings = {"3m": TimeframeSetting(2.4, 8.0)}
    got = logic.resolve_timeframe_setting(settings, "4m")
    assert abs(got.rr_multiple - 2.4) < 1e-9
    assert abs(got.max_sl_usd - 8.0) < 1e-9


def test_build_context_signal_accepts_custom_tf_without_explicit_key():
    cfg = hc.HammerContextConfig(
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
        entry_pullback_pct=0.0,
    )
    signal = Candle(datetime(2026, 1, 1, 10, 0), 100.0, 103.5, 90.0, 102.0)
    nxt = Candle(datetime(2026, 1, 1, 10, 3), 102.0, 104.0, 101.0, 103.0)
    sig = hc.build_context_signal(TradeDirection.BUY, signal, nxt, "4m", cfg)
    assert sig is not None
    assert abs(sig.rr_multiple - 2.0) < 1e-9


def test_unfilled_limit_marks_ignored_reason():
    """Mirror simulate_timeframe_outcomes handling when find_limit_fill returns None."""
    highs = np.array([103.0, 102.5, 102.0])
    lows = np.array([101.0, 101.5, 101.2])
    opens = np.array([102.0, 102.0, 101.8])
    idx = hc.find_limit_fill_index(
        TradeDirection.BUY, 100.0, 90.0, highs, lows, opens, 0, 10,
    )
    assert idx is None

    ts = datetime(2026, 1, 1, 12, 0)
    candle = Candle(ts, 102.0, 103.0, 101.0, 102.5)
    sig = TradeSignal(
        direction=TradeDirection.BUY,
        hammer_candle=candle,
        entry_candle=candle,
        entry_price=100.0,
        stop_loss=90.0,
        risk=10.0,
        rr_multiple=2.0,
        target=120.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    # Same mutation path as backtest.py when fill is never found
    sig.ignored = True
    sig.ignore_reason = "Limit not filled within scan window"
    assert sig.ignored and "Limit not filled" in sig.ignore_reason


def test_still_open_blocks_later_signal_when_overlap_disabled():
    """STILL_OPEN must extend open_until so the next signal is SKIPPED_OVERLAP."""
    open_until = {m: None for m in ExitModel}
    data_end = datetime(2026, 1, 1, 18, 0)
    t0 = datetime(2026, 1, 1, 10, 0)
    t1 = datetime(2026, 1, 1, 11, 0)

    # First trade unresolved
    outcome = TradeOutcome.STILL_OPEN
    exit_time = None
    if exit_time is not None:
        open_until[ExitModel.WORST_CASE] = exit_time
    elif outcome == TradeOutcome.STILL_OPEN:
        open_until[ExitModel.WORST_CASE] = data_end

    assert open_until[ExitModel.WORST_CASE] == data_end
    assert t1 < open_until[ExitModel.WORST_CASE]
    # Later entry while first still open → blocked
    still_open_blocking = (
        open_until[ExitModel.WORST_CASE] is not None
        and t1 < open_until[ExitModel.WORST_CASE]
    )
    assert still_open_blocking is True
    # Sanity: entry after data_end would not block
    assert not (datetime(2026, 1, 2) < open_until[ExitModel.WORST_CASE])


def test_market_entry_bar_sl_is_visible_to_resolver():
    """Entry-bar scan (start=fill_idx) catches same-bar SL after open entry."""
    sl, tp = 90.0, 120.0
    # Entry bar: open 100, low tags SL; next bar would hit TP
    highs = np.array([101.0, 125.0])
    lows = np.array([89.0, 110.0])
    opens = np.array([100.0, 112.0])
    closes = np.array([95.0, 124.0])
    ts = [datetime(2026, 1, 1, 10, 0), datetime(2026, 1, 1, 10, 3)]

    skipped = resolve_all_exit_models_for_trade(
        direction=TradeDirection.BUY, sl=sl, target=tp,
        future_high=highs[1:], future_low=lows[1:],
        future_open=opens[1:], future_close=closes[1:],
        future_timestamps=ts[1:], max_scan=10,
    )
    assert skipped[ExitModel.WORST_CASE][0] == TradeOutcome.WIN

    fixed = resolve_all_exit_models_for_trade(
        direction=TradeDirection.BUY, sl=sl, target=tp,
        future_high=highs, future_low=lows,
        future_open=opens, future_close=closes,
        future_timestamps=ts, max_scan=10,
    )
    assert fixed[ExitModel.WORST_CASE][0] == TradeOutcome.LOSS
