"""
Backtest calculation audit — hand-computed values, every pattern routes to its
own engine, and equity/drawdown follow the real close order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import polars as pl

import backtest as bt
import doji_logic
import hammer_context_logic as hc
import logic
from backtest import (
    BacktestConfig,
    ExitModel,
    PositionSizingMode,
    TradeOutcome,
    calculate_pnl,
    calculate_position_size,
)


def _cfg(pattern_type: str, strategy, **extra) -> BacktestConfig:
    opts = dict(
        strategy_config=strategy,
        pattern_type=pattern_type,
        symbol="XAUUSD",
        data_root="data",
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 2, 1),
        timeframes_to_test=["1hour"],
        max_forward_candles=50,
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
        allow_overlapping_trades=True,
        sessions_enabled={"Asian": True, "London": True, "US": True},
    )
    opts.update(extra)
    return BacktestConfig(**opts)


def _df(bars):
    t0 = datetime(2024, 1, 2, 3, 0)
    return pl.DataFrame({
        "datetime": [t0 + timedelta(hours=i) for i in range(len(bars))],
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[3] for b in bars],
    })


_FLAT = [(100.0, 100.5, 99.5, 100.2)] * 6


def _routes(pattern_type: str, strategy):
    """Which engine's run_strategy did simulate_timeframe_outcomes call?"""
    called = []

    def spy(name):
        def _f(*_a, **_k):
            called.append(name)
            return []
        return _f

    with patch.object(bt, "load_candles_df", return_value=_df(_FLAT)), \
         patch.object(bt.logic, "run_strategy", side_effect=spy("hammer")), \
         patch.object(bt.doji_logic, "run_strategy", side_effect=spy("doji")), \
         patch.object(bt.hwc_logic, "run_strategy", side_effect=spy("hwc")), \
         patch.object(bt.hwc35_logic, "run_strategy", side_effect=spy("hwc35")):
        bt.simulate_timeframe_outcomes("1hour", _cfg(pattern_type, strategy))
    return called


def test_each_pattern_routes_to_its_own_engine_only():
    assert _routes("hammer", logic.StrategyConfig()) == ["hammer"]
    assert _routes("doji", doji_logic.DojiStrategyConfig()) == ["doji"]
    assert _routes(hc.PATTERN_TYPE, hc.HammerContextConfig()) == ["hwc"]
    assert _routes(hc.PATTERN_TYPE_35, hc.HammerContextConfig(entry_pullback_pct=35.0)) == ["hwc35"]


def _buy_signal(entry=100.0, sl=90.0, rr=2.0):
    risk = entry - sl
    return logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=logic.Candle("s", 95, 101, sl, 100),
        entry_candle=logic.Candle("e", entry, entry + 1, entry - 1, entry),
        entry_price=entry, stop_loss=sl, risk=risk, rr_multiple=rr,
        target=entry + rr * risk, timeframe="1h",
    )


def test_fixed_risk_win_pays_rr_times_risk_and_loss_costs_risk():
    sig = _buy_signal(entry=100.0, sl=90.0, rr=2.0)  # risk 10 → target 120
    cfg = _cfg("hammer", logic.StrategyConfig(), fixed_risk_usd=150.0)
    size, risk_usd = calculate_position_size(sig, cfg, 10000.0)
    assert abs(size - 15.0) < 1e-12 and risk_usd == 150.0
    assert abs(calculate_pnl(sig, TradeOutcome.WIN, sig.target, size, cfg) - 300.0) < 1e-9
    assert abs(calculate_pnl(sig, TradeOutcome.LOSS, sig.stop_loss, size, cfg) + 150.0) < 1e-9


def test_slippage_and_commission_hand_computed():
    sig = _buy_signal(entry=100.0, sl=90.0, rr=2.0)
    cfg = _cfg("hammer", logic.StrategyConfig(), slippage_usd=0.5, commission_per_trade=3.0)
    size = 2.0
    # Win: exit 120-0.5, entry 100+0.5 → 19 × 2 − 3 = 35
    assert abs(calculate_pnl(sig, TradeOutcome.WIN, 120.0, size, cfg) - 35.0) < 1e-9
    # Loss: exit 90-0.5, entry 100.5 → −11 × 2 − 3 = −25
    assert abs(calculate_pnl(sig, TradeOutcome.LOSS, 90.0, size, cfg) + 25.0) < 1e-9


def test_percent_of_equity_sizing_hand_computed():
    sig = _buy_signal(entry=100.0, sl=90.0)
    cfg = _cfg(
        "hammer", logic.StrategyConfig(),
        position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY,
        risk_pct_of_equity=2.0,
    )
    size, risk_usd = calculate_position_size(sig, cfg, 12000.0)
    assert abs(risk_usd - 240.0) < 1e-9
    assert abs(size - 24.0) < 1e-9


def test_end_to_end_hammer_trade_levels_and_pnl():
    """Classic green hammer → next-open entry, low−buffer SL, RR target, WIN pnl."""
    strat = logic.StrategyConfig(
        entry_rule=logic.EntryRule.NEXT_CANDLE_OPEN,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=0.5,
        enable_risk_limit=False,
        timeframe_settings={"1h": logic.TimeframeSetting(2.0, 500.0)},
    )
    hammer = logic.Candle("h", 100.0, 103.5, 90.0, 102.0)
    probe = logic.check_hammer(hammer, strat)
    assert probe.direction == logic.TradeDirection.BUY, "fixture must be a BUY hammer"

    bars = [
        (103.0, 103.5, 102.8, 103.2),   # not a hammer
        (hammer.open, hammer.high, hammer.low, hammer.close),
        (100.6, 101.0, 100.2, 100.8),   # entry bar: open 100.6
        (100.8, 101.5, 100.5, 101.0),
        (101.0, 125.0, 100.9, 124.0),   # target hit
        (124.0, 124.5, 123.9, 124.2),
    ]
    with patch.object(bt, "load_candles_df", return_value=_df(bars)):
        cfg = _cfg("hammer", strat)
        ledger, _ = bt.run_full_backtest(cfg)
    taken = [t for t in ledger if t.outcomes[ExitModel.WORST_CASE][0] != TradeOutcome.SKIPPED_OVERLAP]
    assert len(taken) >= 1
    t = taken[0]
    s = t.signal
    assert abs(s.entry_price - 100.6) < 1e-9
    assert abs(s.stop_loss - (90.0 - 0.5)) < 1e-9
    risk = 100.6 - 89.5
    assert abs(s.target - (100.6 + 2.0 * risk)) < 1e-9
    outcome, px, _ts, _b = t.outcomes[ExitModel.WORST_CASE]
    assert outcome == TradeOutcome.WIN and abs(px - s.target) < 1e-9
    assert abs(t.pnl[ExitModel.WORST_CASE] - 200.0) < 1e-6  # 2R on $100 risk


def test_drawdown_uses_close_order_not_entry_order():
    """Overlapping trades: equity path must follow exit times."""
    t0 = datetime(2024, 1, 1)
    df = pl.DataFrame({
        "entry_time": [t0, t0 + timedelta(hours=1)],
        "exit_time": [t0 + timedelta(hours=10), t0 + timedelta(hours=2)],
        "outcome": ["WIN", "LOSS"],
        "pnl_usd": [200.0, -100.0],
    })
    stats = bt._streaks_and_drawdown(df, 1000.0)
    # Close order: −100 first (1000→900), then +200 → DD = 100
    assert abs(stats["max_drawdown_usd"] - 100.0) < 1e-9
    assert abs(stats["ending_capital"] - 1100.0) < 1e-9
