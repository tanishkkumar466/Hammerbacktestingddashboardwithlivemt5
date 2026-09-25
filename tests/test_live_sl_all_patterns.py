"""
Live SL lock is pattern-agnostic: every pattern shares _resolve_live_order →
reanchor_sl_tp_to_fill. Candle extreme ± buffer must stay put on market fill.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import doji_logic
import logic
from live import LiveTradingEngine, reanchor_sl_tp_to_fill
from strategy import hammer_with_candles_35_logic as hwc35
from strategy import hammer_with_candles_logic as hwc


def _c(ts: str, o: float, h: float, l: float, c: float) -> logic.Candle:
    return logic.Candle(ts, o, h, l, c)


def _engine(broker, strategy_cfg) -> LiveTradingEngine:
    eng = LiveTradingEngine.__new__(LiveTradingEngine)
    eng.broker = broker
    eng.live_config = SimpleNamespace(
        order_mode="market",
        max_entry_deviation_points=200.0,
        limit_offset_from_market=True,
        limit_offset_points=0.0,
    )
    eng.strategy_config = strategy_cfg
    eng._broker_lock = MagicMock()
    eng._broker_lock.__enter__ = MagicMock(return_value=None)
    eng._broker_lock.__exit__ = MagicMock(return_value=False)
    eng.log = lambda *_a, **_k: None
    return eng


def _assert_buffer_widens_and_live_locks(
    *,
    label: str,
    direction: logic.TradeDirection,
    signal_candle: logic.Candle,
    entry: float,
    sl: float,
    rr: float = 2.0,
    await_limit: bool = False,
    market_fill: float,
):
    """Buffer past extreme; Live market fill must not move SL off signal."""
    is_buy = direction == logic.TradeDirection.BUY
    extreme = signal_candle.low if is_buy else signal_candle.high
    if is_buy:
        assert sl <= extreme + 1e-9, f"{label}: BUY SL must be at/below candle low"
    else:
        assert sl >= extreme - 1e-9, f"{label}: SELL SL must be at/above candle high"

    if is_buy:
        risk = entry - sl
        tp = entry + rr * risk
    else:
        risk = sl - entry
        tp = entry - rr * risk

    sig = logic.TradeSignal(
        direction=direction,
        hammer_candle=signal_candle,
        entry_candle=_c("e", entry, entry + 1, entry - 1, entry),
        entry_price=entry,
        stop_loss=sl,
        risk=risk,
        rr_multiple=rr,
        target=tp,
        timeframe="3m",
        await_limit_fill=await_limit,
        signal_entry_price=entry if await_limit else None,
        entry_pullback_pct=35.0 if await_limit else None,
    )

    # Old v1.0.33 bug would slide SL by fill−entry.
    old_slid = sl + (market_fill - entry)
    locked_sl, _tp = reanchor_sl_tp_to_fill(sig, fill_price=market_fill)
    assert abs(locked_sl - sl) < 1e-9, (
        f"{label}: reanchor moved SL {sl} → {locked_sl} (old slide would be {old_slid})"
    )

    broker = MagicMock()
    if is_buy:
        broker.get_tick_prices.return_value = (market_fill - 0.05, market_fill)
    else:
        broker.get_tick_prices.return_value = (market_fill, market_fill + 0.05)
    broker.symbol_point.return_value = 0.01
    eng = _engine(
        broker,
        SimpleNamespace(
            entry_offset=0.0,
            inverted_entry_offset=0.0,
            buffer_mode="FLAT_AMOUNT",
            sl_buffer_flat=0.0,
        ),
    )
    _mode, _lp, order_sl, _order_tp, _exec = eng._resolve_live_order(sig, "XAUUSD")
    assert abs(order_sl - sl) < 1e-9, (
        f"{label}: _resolve_live_order placed SL {order_sl}, expected locked {sl}"
    )


def test_hammer_pattern_buy_sl_buffer_and_live_lock():
    hammer = _c("s", 2650, 2660, 2640, 2655)
    nxt = _c("e", 2652, 2665, 2650, 2660)
    flat, offset = 0.50, 0.30
    cfg = logic.StrategyConfig(
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        entry_offset=offset,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=flat,
        sl_mode=logic.StopLossMode.CANDLE_EXTREME,
        enable_risk_limit=False,
    )
    entry = logic.calculate_entry_price(hammer, nxt, cfg)
    sl = logic.calculate_stop_loss(
        hammer, logic.TradeDirection.BUY, cfg, entry_price=entry,
    )
    assert abs(sl - (hammer.low - flat)) < 1e-9
    _assert_buffer_widens_and_live_locks(
        label="hammer BUY",
        direction=logic.TradeDirection.BUY,
        signal_candle=hammer,
        entry=entry,
        sl=sl,
        market_fill=nxt.open,  # fill without offset
    )


def test_hammer_pattern_sell_sl_buffer_and_live_lock():
    hammer = _c("s", 2655, 2660, 2640, 2650)  # inverted-style high tip
    nxt = _c("e", 2652, 2658, 2648, 2650)
    flat, offset = 0.50, 0.30
    cfg = logic.StrategyConfig(
        inverted_entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        inverted_entry_offset=offset,
        inverted_buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        inverted_sl_buffer_flat=flat,
        inverted_sl_mode=logic.StopLossMode.CANDLE_EXTREME,
        enable_risk_limit=False,
    )
    entry = logic.calculate_entry_price(
        hammer, nxt, cfg, logic.HammerVariant.INVERTED,
    )
    sl = logic.calculate_stop_loss(
        hammer, logic.TradeDirection.SELL, cfg,
        logic.HammerVariant.INVERTED, entry_price=entry,
    )
    assert abs(sl - (hammer.high + flat)) < 1e-9
    _assert_buffer_widens_and_live_locks(
        label="hammer SELL",
        direction=logic.TradeDirection.SELL,
        signal_candle=hammer,
        entry=entry,
        sl=sl,
        market_fill=nxt.open,
    )


def test_doji_pattern_buy_sl_buffer_and_live_lock():
    signal = _c("s", 100.0, 101.0, 99.0, 100.05)
    nxt = _c("e", 100.2, 101.0, 100.0, 100.5)
    flat, offset = 0.40, 0.25
    cfg = doji_logic.DojiStrategyConfig(
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        entry_offset=offset,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=flat,
        sl_mode=logic.StopLossMode.CANDLE_EXTREME,
        enable_risk_limit=False,
    )
    entry = doji_logic.calculate_entry_price(signal, nxt, cfg)
    sl = doji_logic.calculate_stop_loss(
        signal, logic.TradeDirection.BUY, cfg, entry_price=entry,
    )
    assert abs(sl - (signal.low - flat)) < 1e-9
    _assert_buffer_widens_and_live_locks(
        label="doji BUY",
        direction=logic.TradeDirection.BUY,
        signal_candle=signal,
        entry=entry,
        sl=sl,
        market_fill=nxt.open,
    )


def test_hwc_pattern_buy_sl_buffer_and_live_lock():
    signal = _c("s", 100.0, 103.5, 90.0, 102.0)  # classic green hammer
    nxt = _c("n", 102.0, 103.0, 101.0, 102.5)
    flat, offset = 0.50, 0.30
    cfg = hwc.HammerContextConfig(
        lookback_candles=1,
        enable_sell=False,
        enable_risk_limit=False,
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        entry_offset=offset,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=flat,
        sl_mode=logic.StopLossMode.CANDLE_EXTREME,
    )
    cfg = hwc.prepare_config(cfg)
    pads = [_c("p", 95.0, 96.0, 94.0, 95.0), signal, nxt]
    taken = [s for s in hwc.run_strategy(pads, "3m", cfg) if not s.ignored]
    assert len(taken) == 1
    sig = taken[0]
    assert abs(sig.stop_loss - (signal.low - flat)) < 1e-9
    _assert_buffer_widens_and_live_locks(
        label="HWC BUY",
        direction=logic.TradeDirection.BUY,
        signal_candle=signal,
        entry=float(sig.entry_price),
        sl=float(sig.stop_loss),
        market_fill=nxt.open,
    )


def test_hwc35_pullback_keeps_sl_absolute_on_market():
    """35% pullback: await_limit_fill — SL still candle extreme ± buffer."""
    signal = _c("s", 100.0, 103.5, 90.0, 102.0)
    nxt = _c("n", 102.0, 103.0, 101.0, 102.5)
    flat = 0.50
    cfg = hwc35.HammerContextConfig(
        lookback_candles=1,
        enable_sell=False,
        enable_risk_limit=False,
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        entry_offset=0.0,
        entry_pullback_pct=35.0,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=flat,
        sl_mode=logic.StopLossMode.CANDLE_EXTREME,
    )
    cfg = hwc35.prepare_config(cfg)
    pads = [_c("p", 95.0, 96.0, 94.0, 95.0), signal, nxt]
    taken = [s for s in hwc35.run_strategy(pads, "3m", cfg) if not s.ignored]
    assert len(taken) == 1
    sig = taken[0]
    assert sig.await_limit_fill
    assert abs(sig.stop_loss - (signal.low - flat)) < 1e-9

    # Market order type on a pullback signal — SL must stay absolute.
    broker = MagicMock()
    broker.get_tick_prices.return_value = (101.9, 102.0)
    broker.symbol_point.return_value = 0.01
    eng = _engine(
        broker,
        SimpleNamespace(
            entry_offset=0.0,
            inverted_entry_offset=0.0,
            buffer_mode="FLAT_AMOUNT",
            sl_buffer_flat=flat,
        ),
    )
    mode, _lp, order_sl, _tp, _exec = eng._resolve_live_order(sig, "XAUUSD")
    assert mode == "market"
    assert abs(order_sl - float(sig.stop_loss)) < 1e-9
