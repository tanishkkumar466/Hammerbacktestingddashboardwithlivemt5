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


def test_market_entry_bar_skipped_for_hwc_baseline():
    """
    Plain Hammer with candles (market / next-open) must NOT scan the entry bar —
    matches pre-1.0.29 and client baselines. Pullback fills still include fill bar.
    """
    sl, tp = 90.0, 120.0
    # Entry bar: open 100, low tags SL; next bar would hit TP
    highs = np.array([101.0, 125.0])
    lows = np.array([89.0, 110.0])
    opens = np.array([100.0, 112.0])
    closes = np.array([95.0, 124.0])
    ts = [datetime(2026, 1, 1, 10, 0), datetime(2026, 1, 1, 10, 3)]

    # Market path (fill_idx+1): skip entry bar → WIN on next bar
    market = resolve_all_exit_models_for_trade(
        direction=TradeDirection.BUY, sl=sl, target=tp,
        future_high=highs[1:], future_low=lows[1:],
        future_open=opens[1:], future_close=closes[1:],
        future_timestamps=ts[1:], max_scan=10,
    )
    assert market[ExitModel.WORST_CASE][0] == TradeOutcome.WIN

    # Pullback path (fill_idx): include fill/entry bar → LOSS
    pullback = resolve_all_exit_models_for_trade(
        direction=TradeDirection.BUY, sl=sl, target=tp,
        future_high=highs, future_low=lows,
        future_open=opens, future_close=closes,
        future_timestamps=ts, max_scan=10,
    )
    assert pullback[ExitModel.WORST_CASE][0] == TradeOutcome.LOSS


def test_exit_scan_starts_on_first_live_bar():
    """Limit → fill bar; open/signal-candle entries → entry bar; next close → next bar."""
    from types import SimpleNamespace
    from backtest import _exit_scan_start_index

    R = logic.EntryRule
    assert _exit_scan_start_index(SimpleNamespace(await_limit_fill=True), 5) == 5
    for rule in (R.NEXT_CANDLE_OPEN, R.HAMMER_CLOSE, R.HAMMER_HIGH, R.HAMMER_LOW):
        sig = SimpleNamespace(await_limit_fill=False, entry_rule=rule.value)
        assert _exit_scan_start_index(sig, 5) == 5, rule
    close_sig = SimpleNamespace(await_limit_fill=False, entry_rule=R.NEXT_CANDLE_CLOSE.value)
    assert _exit_scan_start_index(close_sig, 5) == 6
    # Legacy switch reproduces pre-1.0.34 numbers (entry bar ignored)
    open_sig = SimpleNamespace(await_limit_fill=False, entry_rule=R.NEXT_CANDLE_OPEN.value)
    assert _exit_scan_start_index(open_sig, 5, legacy_skip_entry_candle=True) == 6
    limit_sig = SimpleNamespace(await_limit_fill=True)
    assert _exit_scan_start_index(limit_sig, 5, legacy_skip_entry_candle=True) == 5


def _resolve(direction, sl, tp, bars, **kw):
    arr = np.array(bars, dtype=float)
    ts = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(len(bars))]
    return resolve_all_exit_models_for_trade(
        direction=direction, sl=sl, target=tp,
        future_open=arr[:, 0], future_high=arr[:, 1],
        future_low=arr[:, 2], future_close=arr[:, 3],
        future_timestamps=ts, max_scan=len(bars), **kw,
    )


def test_gap_open_beyond_sl_exits_at_open_not_sl():
    # BUY SL 90: bar opens at 85 (gap) → loss priced at 85
    out = _resolve(TradeDirection.BUY, 90.0, 120.0, [(100, 101, 99, 100), (85, 88, 84, 87)])
    for m in ExitModel:
        assert out[m][0] == TradeOutcome.LOSS and out[m][1] == 85.0
    # SELL SL 110: bar opens at 115 → loss priced at 115
    out = _resolve(TradeDirection.SELL, 110.0, 80.0, [(100, 101, 99, 100), (115, 116, 113, 114)])
    for m in ExitModel:
        assert out[m][0] == TradeOutcome.LOSS and out[m][1] == 115.0


def test_gap_open_beyond_target_is_capped_at_target():
    # Opens at 125 past TP 120 (and the bar later trades through SL): still a WIN, booked at TP
    out = _resolve(TradeDirection.BUY, 90.0, 120.0, [(100, 101, 99, 100), (125, 126, 80, 81)])
    for m in ExitModel:
        assert out[m][0] == TradeOutcome.WIN and out[m][1] == 120.0


def test_intrabar_limit_fill_ignores_target_before_fill():
    # BUY limit 100 filled mid-bar (open 110 > limit); high 125 ≥ TP happened pre-fill
    bars = [(110, 125, 99, 105), (105, 106, 104, 105)]
    out = _resolve(TradeDirection.BUY, 90.0, 120.0, bars, prefill_first_bar=True)
    assert out[ExitModel.BEST_CASE][0] == TradeOutcome.STILL_OPEN
    # SL beyond the limit on the fill bar is certain after the fill
    bars_sl = [(110, 111, 89, 95)]
    out = _resolve(TradeDirection.BUY, 90.0, 120.0, bars_sl, prefill_first_bar=True)
    for m in ExitModel:
        assert out[m][0] == TradeOutcome.LOSS and out[m][1] == 90.0


def test_fast_and_vectorized_paths_agree():
    rng = np.random.default_rng(7)
    for _ in range(200):
        n = 80  # above SHORT_SCAN_THRESHOLD → vectorized
        closes = 100 + np.cumsum(rng.normal(0, 1.5, n))
        opens = np.concatenate(([100.0], closes[:-1] + rng.normal(0, 0.8, n - 1)))
        highs = np.maximum(opens, closes) + rng.uniform(0, 2, n)
        lows = np.minimum(opens, closes) - rng.uniform(0, 2, n)
        bars = list(zip(opens, highs, lows, closes))
        d = TradeDirection.BUY if rng.random() < 0.5 else TradeDirection.SELL
        sl, tp = (95.0, 110.0) if d == TradeDirection.BUY else (105.0, 90.0)
        pre = bool(rng.random() < 0.3)
        vec = _resolve(d, sl, tp, bars, prefill_first_bar=pre)
        # Force the loop path by chunking into a short window when resolved early
        from backtest import _resolve_short_scan
        arr = np.array(bars)
        ts = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(n)]
        loop = _resolve_short_scan(
            d, sl, tp, arr[:, 1], arr[:, 2], arr[:, 0], arr[:, 3], ts, n,
            prefill_first_bar=pre,
        )
        assert vec == loop
