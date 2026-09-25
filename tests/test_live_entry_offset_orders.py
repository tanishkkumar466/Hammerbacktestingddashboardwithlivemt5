"""Live logs/order levels: entry_offset on entry, flat buffer on SL only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import logic
from live import LiveTradingEngine, reanchor_sl_tp_to_fill


def _candle(ts: str, o: float, h: float, l: float, c: float) -> logic.Candle:
    return logic.Candle(ts, o, h, l, c)


def _engine(broker, order_mode: str, strategy_cfg, **cfg_extra) -> LiveTradingEngine:
    opts = {
        "order_mode": order_mode,
        "max_entry_deviation_points": 200.0,
        "limit_offset_from_market": True,
        "limit_offset_points": 0.0,
    }
    opts.update(cfg_extra)
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.broker = broker
    engine.live_config = SimpleNamespace(**opts)
    engine.strategy_config = strategy_cfg
    engine._broker_lock = MagicMock()
    engine._broker_lock.__enter__ = MagicMock(return_value=None)
    engine._broker_lock.__exit__ = MagicMock(return_value=False)
    engine.log = lambda *_a, **_k: None
    return engine


def _buy_sig(*, entry: float, sl: float, tp: float, rr: float = 2.0) -> logic.TradeSignal:
    return logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 2650, 2660, 2640, 2655),
        entry_candle=_candle("e", 2652, 2665, 2650, 2660),
        entry_price=entry,
        stop_loss=sl,
        risk=entry - sl,
        rr_multiple=rr,
        target=tp,
        timeframe="3m",
        await_limit_fill=False,
    )


def test_flat_buffer_does_not_change_entry_price():
    """Offset and flat buffer are config floats (any value from backtest → live)."""
    hammer = _candle("s", 2650, 2660, 2640, 2655)
    nxt = _candle("e", 2652, 2665, 2650, 2660)
    offset = 0.30  # example only — Live uses whatever StrategyConfig has
    flat = 0.30
    cfg = logic.StrategyConfig(
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        entry_offset=offset,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=flat,
    )
    entry = logic.calculate_entry_price(hammer, nxt, cfg)
    sl = logic.calculate_stop_loss(
        hammer, logic.TradeDirection.BUY, cfg, entry_price=entry,
    )
    assert abs(entry - (nxt.open + offset)) < 1e-9
    assert abs(sl - (hammer.low - flat)) < 1e-9


def test_market_fill_keeps_buffered_sl_does_not_slide_by_offset():
    """
    Whatever entry_offset / sl_buffer_flat the backtest preset uses, Live must
    keep that buffered SL — not slide it by the entry-offset gap on market fill.
    """
    offset, flat = 0.30, 0.30  # variables from preset; not hardcoded in Live
    entry = 2652.0 + offset
    sl_level = 2640.0 - flat
    sig = _buy_sig(entry=entry, sl=sl_level, tp=entry + 2.0 * (entry - sl_level), rr=2.0)
    fill = 2652.0
    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=fill)
    assert abs(sl - sl_level) < 1e-9
    risk = fill - sl_level
    assert abs(tp - (fill + 2.0 * risk)) < 1e-9


def test_sell_market_fill_does_not_shrink_candle_high_sl():
    """
    Regression: old Live did SL += (fill − entry). With entry_offset, strategy
    entry sits above the bid; market fill below entry made delta negative and
    pulled SELL SL down off candle high — tighter stop, extra losses.
    """
    offset, flat = 0.30, 0.30
    candle_high = 2660.0
    entry = 2655.0 + offset  # strategy entry above typical bid
    sl_level = candle_high + flat
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.SELL,
        hammer_candle=_candle("s", 2650, candle_high, 2640, 2655),
        entry_candle=_candle("e", 2655, 2662, 2650, 2652),
        entry_price=entry,
        stop_loss=sl_level,
        risk=sl_level - entry,
        rr_multiple=2.0,
        target=entry - 2.0 * (sl_level - entry),
        timeframe="3m",
        await_limit_fill=False,
    )
    fill = 2655.0  # bid without entry_offset
    # Old bug: sl_level + (fill - entry) == sl_level - offset  (shrunk toward price)
    old_slid = sl_level + (fill - entry)
    assert old_slid < sl_level  # documents the shrink

    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=fill)
    assert abs(sl - sl_level) < 1e-9
    assert abs(sl - old_slid) > 0.1  # must not apply the old slide
    risk = sl_level - fill
    assert abs(tp - (fill - 2.0 * risk)) < 1e-9


def test_buy_gap_up_fill_does_not_raise_candle_low_sl():
    """BUY fill above strategy entry must not lift SL off candle low + buffer."""
    entry = 2652.0
    sl_level = 2640.0 - 0.30
    sig = _buy_sig(entry=entry, sl=sl_level, tp=entry + 2.0 * (entry - sl_level), rr=2.0)
    fill = 2652.50  # gap up
    old_slid = sl_level + (fill - entry)
    assert old_slid > sl_level  # old code would shrink risk distance

    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=fill)
    assert abs(sl - sl_level) < 1e-9
    risk = fill - sl_level
    assert abs(tp - (fill + 2.0 * risk)) < 1e-9


def test_resolve_logs_exec_entry_separate_from_strategy_entry():
    offset, flat = 0.30, 0.30
    broker = MagicMock()
    broker.get_tick_prices.return_value = (2651.90, 2652.0)
    broker.symbol_point.return_value = 0.01
    strategy = SimpleNamespace(
        entry_offset=offset,
        inverted_entry_offset=0.0,
        buffer_mode="FLAT_AMOUNT",
        sl_buffer_flat=flat,
    )
    engine = _engine(broker, "market", strategy)
    entry = 2652.0 + offset
    sl_level = 2640.0 - flat
    sig = _buy_sig(entry=entry, sl=sl_level, tp=entry + 2.0 * (entry - sl_level), rr=2.0)

    mode, limit_px, sl, tp, exec_entry = engine._resolve_live_order(sig, "XAUUSD")
    assert mode == "market"
    assert limit_px is None
    assert abs(exec_entry - 2652.0) < 1e-9
    assert abs(sl - sl_level) < 1e-9
    risk = 2652.0 - sl_level
    assert abs(tp - (2652.0 + 2.0 * risk)) < 1e-9


def test_record_trade_event_order_uses_exec_prices_not_strategy_entry(tmp_path):
    """ORDER journal uses sent prices; strategy_* columns keep preset-built levels."""
    offset, flat = 0.30, 0.30
    strat_entry = 2652.0 + offset
    strat_sl = 2640.0 - flat
    exec_entry = 2652.0
    exec_tp = exec_entry + 2.0 * (exec_entry - strat_sl)

    rows = []
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine._journal_dir = str(tmp_path)
    engine._active_symbol = "XAUUSD"
    engine.live_config = SimpleNamespace(
        symbol="XAUUSD",
        timeframe_label="3m",
        order_mode="market",
        magic=1,
        notify_enabled=False,
        account_id="",
        account_name="",
        slot_name="",
    )
    engine.pattern_label = "Hammer"
    engine._telegram = MagicMock()
    engine._refresh_tracked_from_broker = MagicMock()

    import live as live_mod

    orig = live_mod.append_trade_row

    def capture(journal_dir, row):
        rows.append(dict(row))
        return orig(journal_dir, row)

    live_mod.append_trade_row = capture
    try:
        sig = _buy_sig(
            entry=strat_entry, sl=strat_sl,
            tp=strat_entry + 2.0 * (strat_entry - strat_sl), rr=2.0,
        )
        engine._record_trade_event(
            "ORDER",
            sig,
            volume=0.1,
            dry_run=False,
            order_mode="market",
            entry_price=exec_entry,
            stop_loss=strat_sl,
            target=exec_tp,
        )
    finally:
        live_mod.append_trade_row = orig

    assert rows
    assert abs(float(rows[0]["entry_price"]) - exec_entry) < 1e-9
    assert abs(float(rows[0]["stop_loss"]) - strat_sl) < 1e-9
    assert abs(float(rows[0]["strategy_entry"]) - strat_entry) < 1e-9
    assert abs(float(rows[0]["strategy_sl"]) - strat_sl) < 1e-9
