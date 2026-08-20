"""Stop-loss mode: candle extreme vs fixed distance from entry."""

from logic import (
    BufferMode,
    Candle,
    CandleColor,
    EntryRule,
    HammerVariant,
    StopLossMode,
    StrategyConfig,
    TimeframeSetting,
    TradeDirection,
    HammerResult,
    build_trade_signal,
    calculate_stop_loss,
)

import hammer_context_logic as hc


def _classic_green():
    return Candle("s", 100.0, 103.5, 90.0, 102.0)


def _next():
    return Candle("n", 102.0, 103.0, 101.0, 102.5)


def test_fixed_sl_from_entry_buy():
    cfg = StrategyConfig(
        sl_mode=StopLossMode.FIXED_FROM_ENTRY,
        sl_fixed_distance=8.0,
        enable_risk_limit=False,
        timeframe_settings={"1h": TimeframeSetting(2.0, 500.0)},
    )
    hammer = _classic_green()
    entry = 102.5
    sl = calculate_stop_loss(
        hammer, TradeDirection.BUY, cfg, HammerVariant.CLASSIC, entry_price=entry,
    )
    assert abs(sl - (entry - 8.0)) < 1e-9


def test_candle_extreme_sl_unchanged():
    cfg = StrategyConfig(
        sl_mode=StopLossMode.CANDLE_EXTREME,
        buffer_mode=BufferMode.NONE,
        enable_risk_limit=False,
        timeframe_settings={"1h": TimeframeSetting(2.0, 500.0)},
    )
    hammer = _classic_green()
    sl = calculate_stop_loss(
        hammer, TradeDirection.BUY, cfg, HammerVariant.CLASSIC, entry_price=102.5,
    )
    assert abs(sl - hammer.low) < 1e-9


def test_hammer_context_fixed_sl_wired():
    cfg = hc.HammerContextConfig(
        sl_mode=StopLossMode.FIXED_FROM_ENTRY,
        sl_fixed_distance=6.0,
        lookback_candles=0,
        enable_sell=False,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    candles = [_classic_green(), _next()]
    sigs = hc.run_strategy(candles, "3m", cfg)
    taken = [s for s in sigs if not s.ignored]
    assert len(taken) == 1
    assert abs(taken[0].risk - 6.0) < 1e-6


def test_build_trade_signal_uses_fixed_sl():
    cfg = StrategyConfig(
        sl_mode=StopLossMode.FIXED_FROM_ENTRY,
        sl_fixed_distance=5.0,
        enable_risk_limit=False,
        timeframe_settings={"1h": TimeframeSetting(2.0, 500.0)},
    )
    hr = HammerResult(
        candle=_classic_green(),
        color=CandleColor.GREEN,
        body_pct=20.0,
        upper_wick_pct=10.0,
        lower_wick_pct=60.0,
        is_valid_shape=True,
        direction=TradeDirection.BUY,
        hammer_variant=HammerVariant.CLASSIC,
    )
    sig = build_trade_signal(hr, _next(), "1h", cfg)
    assert sig is not None and not sig.ignored
    assert abs(sig.risk - 5.0) < 1e-6
