"""Live/backtest parity for Hammer-with-candle pullback entry + fixed TP."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import logic
from hammer_context_logic import (
    HammerContextConfig,
    build_context_signal,
    find_limit_fill_index,
    pullback_entry_price,
)
from live import LiveTradingEngine, reanchor_sl_tp_to_fill
from indicators.filter import _mark_ignored
from logic import TimeframeSetting


def _candle(ts: str, o: float, h: float, l: float, c: float) -> logic.Candle:
    return logic.Candle(ts, o, h, l, c)


def test_reanchor_keeps_absolute_sl_tp_for_pullback():
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4006.5,
        stop_loss=4000.0,
        risk=6.5,
        rr_multiple=2.0,
        target=4030.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=4005.0)
    assert sl == 4000.0
    assert tp == 4030.0


def test_reanchor_shifts_sl_tp_for_normal_market_fill():
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4010.0,
        stop_loss=4000.0,
        risk=10.0,
        rr_multiple=2.0,
        target=4030.0,
        timeframe="3m",
        await_limit_fill=False,
    )
    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=4012.0)
    assert abs(sl - 4002.0) < 1e-9
    assert abs(tp - 4032.0) < 1e-9


def _pullback_engine(broker, order_mode: str, **cfg_extra) -> LiveTradingEngine:
    opts = {
        "order_mode": order_mode,
        "max_entry_deviation_points": 200.0,
        "limit_offset_from_market": True,
        "limit_offset_points": 0.0,
    }
    opts.update(cfg_extra)
    cfg = SimpleNamespace(**opts)
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.broker = broker
    engine.live_config = cfg
    engine._broker_lock = MagicMock()
    engine._broker_lock.__enter__ = MagicMock(return_value=None)
    engine._broker_lock.__exit__ = MagicMock(return_value=False)
    engine.log = lambda *_a, **_k: None
    return engine


def test_resolve_live_order_market_mode_uses_market_on_pullback():
    """Order type Market → instant market even when the signal is a pullback wait."""
    broker = MagicMock()
    broker.get_tick_prices.return_value = (4009.0, 4010.0)
    broker.symbol_point.return_value = 0.01
    engine = _pullback_engine(broker, "market")

    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4006.5,
        stop_loss=4000.0,
        risk=6.5,
        rr_multiple=2.0,
        target=4030.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    resolved = engine._resolve_live_order(sig, "XAUUSD")
    assert resolved is not None
    mode, limit_px, sl, tp = resolved
    assert mode == "market"
    assert limit_px is None
    assert sl == 4000.0
    assert tp == 4030.0


def test_resolve_live_order_limit_mode_keeps_pullback_limit():
    """Order type Limit → resting limit at pullback price; max_dev must not force market."""
    broker = MagicMock()
    # Market far from pullback limit (ask 4010, limit 4006.5 → 350 pts at 0.01)
    broker.get_tick_prices.return_value = (4009.0, 4010.0)
    broker.symbol_point.return_value = 0.01
    engine = _pullback_engine(broker, "limit_entry")

    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4006.5,
        stop_loss=4000.0,
        risk=6.5,
        rr_multiple=2.0,
        target=4030.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    resolved = engine._resolve_live_order(sig, "XAUUSD")
    assert resolved is not None
    mode, limit_px, sl, tp = resolved
    assert mode == "limit_entry"
    assert limit_px == 4006.5
    assert sl == 4000.0
    assert tp == 4030.0


def test_resolve_live_order_pullback_ignores_limit_offset_mode():
    broker = MagicMock()
    broker.get_tick_prices.return_value = (4009.0, 4010.0)
    broker.symbol_point.return_value = 0.01
    broker.limit_price_with_offset.return_value = (4009.5, True)

    engine = _pullback_engine(
        broker, "limit_offset", limit_offset_points=10.0,
    )

    sig = logic.TradeSignal(
        direction=logic.TradeDirection.SELL,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4003.5,
        stop_loss=4010.0,
        risk=6.5,
        rr_multiple=2.0,
        target=3980.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    mode, limit_px, sl, tp = engine._resolve_live_order(sig, "XAUUSD")
    assert mode == "limit_entry"
    assert limit_px == 4003.5
    assert sl == 4010.0
    assert tp == 3980.0
    broker.limit_price_with_offset.assert_not_called()


def test_find_limit_fill_and_fixed_target_match_backtest_rule():
    # Bars: entry bar never touches; next bar dips to pullback
    highs = [4012.0, 4011.0, 4010.0]
    lows = [4008.0, 4005.0, 4004.0]
    opens = [4010.0, 4009.0, 4008.0]
    idx = find_limit_fill_index(
        logic.TradeDirection.BUY, 4006.5, 4000.0, highs, lows, opens, 0, 10,
    )
    assert idx == 1
    buy_e, _ = pullback_entry_price(logic.TradeDirection.BUY, 4010.0, 4000.0, 35.0)
    assert abs(buy_e - 4006.5) < 1e-9
    # TP from signal entry × RR stays 4030
    assert abs(4010.0 + 2.0 * 10.0 - 4030.0) < 1e-9


def test_mark_ignored_preserves_await_limit_fill():
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=4006.5,
        stop_loss=4000.0,
        risk=6.5,
        rr_multiple=2.0,
        target=4030.0,
        timeframe="3m",
        await_limit_fill=True,
    )
    out = _mark_ignored(sig, "SuperTrend")
    assert out.ignored is True
    assert out.await_limit_fill is True
    assert out.target == 4030.0


def test_build_context_signal_sell_pullback_tp_from_base():
    cfg = HammerContextConfig(
        lookback_candles=1,
        enable_buy=False,
        entry_pullback_pct=35.0,
        buffer_mode=logic.BufferMode.NONE,
        sl_buffer_pct=0.0,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    signal = _candle("s", 4010.0, 4020.0, 4008.0, 4009.0)
    nxt = _candle("n", 4000.0, 4005.0, 3995.0, 3998.0)
    # Zero-pullback signal establishes base entry / SL / TP from shared engine
    cfg0 = HammerContextConfig(
        lookback_candles=1,
        enable_buy=False,
        entry_pullback_pct=0.0,
        buffer_mode=logic.BufferMode.NONE,
        sl_buffer_pct=0.0,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    base_sig = build_context_signal(logic.TradeDirection.SELL, signal, nxt, "3m", cfg0)
    sig = build_context_signal(logic.TradeDirection.SELL, signal, nxt, "3m", cfg)
    assert base_sig is not None and sig is not None
    assert not sig.ignored and sig.await_limit_fill is True
    base = float(base_sig.entry_price)
    sl = float(base_sig.stop_loss)
    dist = abs(base - sl)
    expected_entry = base + dist * 0.35  # SELL rises toward SL
    expected_tp = float(base_sig.target)  # TP must stay on signal-entry RR
    assert abs(sig.stop_loss - sl) < 1e-9
    assert abs(sig.entry_price - expected_entry) < 1e-9
    assert abs(sig.target - expected_tp) < 1e-9
    assert abs(sig.target - (base - dist * 2.0)) < 1e-9
