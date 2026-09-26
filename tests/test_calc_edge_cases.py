"""
Hand-computed edge cases the mutation check showed were untested:
fixed SL for SELL, exact SL touch, same-bar SL+TP per exit model, pullback limit
fill search (first bar, never filled), equity floor, pullback gap fill, win rate,
drawdown from the running peak, indicator ANY mode, Limit ± offset base,
and the real-money risk gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import polars as pl
import pytest

import backtest as bt
import doji_logic
import hammer_context_logic as hc
import live as live_trading
import logic
from backtest import BacktestConfig, ExitModel, PositionSizingMode, TradeOutcome
from indicators.config import IndicatorCombineMode, IndicatorStackConfig
from indicators.filter import _combine_checks
from live import LiveTradingEngine

BUY, SELL = logic.TradeDirection.BUY, logic.TradeDirection.SELL
T0 = datetime(2024, 1, 2, 3, 0)


# ---------------------------------------------------------------- fixed SL from entry

@pytest.mark.parametrize("direction, want", [(BUY, 95.0), (SELL, 105.0)])
def test_fixed_sl_from_entry_sits_on_the_losing_side(direction, want):
    candle = logic.Candle("s", 99, 101, 98, 100)
    cfg = logic.StrategyConfig(
        sl_mode=logic.StopLossMode.FIXED_FROM_ENTRY, sl_fixed_distance=5.0,
        inverted_sl_mode=logic.StopLossMode.FIXED_FROM_ENTRY, inverted_sl_fixed_distance=5.0,
    )
    for variant in (logic.HammerVariant.CLASSIC, logic.HammerVariant.INVERTED):
        assert logic.calculate_stop_loss(candle, direction, cfg, variant, entry_price=100.0) == want
    dcfg = doji_logic.DojiStrategyConfig(sl_mode=logic.StopLossMode.FIXED_FROM_ENTRY, sl_fixed_distance=5.0)
    assert doji_logic.calculate_stop_loss(candle, direction, dcfg, entry_price=100.0) == want


# ---------------------------------------------------------------- hammer body band

def _body_band_cfg():
    return logic.StrategyConfig(
        hammer_wick_side=logic.WickSide.LOWER,
        hammer_ratios=logic.HammerRatioConfig(
            dominant_wick_pct=60.0, dominant_wick_tol=20.0,   # lower wick 40–80 %
            small_wick_pct=10.0, small_wick_tol=10.0,         # upper wick 0–20 %
            body_pct=10.0, body_tol=5.0,                      # body 5–15 %
        ),
    )


@pytest.mark.parametrize("o, h, l, c, ok", [
    (97.5, 100.0, 90.0, 98.5, True),     # body 10 %, lower 75 %, upper 15 % → hammer
    (95.5, 100.0, 90.0, 100.0, False),   # wicks fine (lower 55 %, upper 0 %), body 45 % too big
    (97.8, 99.8, 90.0, 98.0, False),     # wicks fine (lower 79.6 %, upper 18.4 %), body 2 % too small
])
def test_hammer_body_must_sit_inside_its_band(o, h, l, c, ok):
    res = logic.check_hammer(logic.Candle("t", o, h, l, c), _body_band_cfg())
    assert res.is_valid_shape is ok, (res.body_pct, res.lower_wick_pct, res.upper_wick_pct)


# ---------------------------------------------------------------- exact SL touch

def _scan(direction, bars, sl, target):
    arr = np.array(bars, dtype=float)
    ts = [T0 + timedelta(hours=i) for i in range(len(bars))]
    return bt.resolve_all_exit_models_for_trade(
        direction, sl, target, arr[:, 1], arr[:, 2], arr[:, 0], arr[:, 3], ts, max_scan=len(bars),
    )


@pytest.mark.parametrize("pad", [0, 80], ids=["short-scan", "long-scan"])
def test_price_touching_sl_exactly_is_a_loss(pad):
    quiet = [(100.0, 101.0, 99.0, 100.5)] * pad
    buy = _scan(BUY, quiet + [(100.0, 101.0, 95.0, 96.0)], sl=95.0, target=110.0)
    sell = _scan(SELL, quiet + [(100.0, 105.0, 99.0, 104.0)], sl=105.0, target=90.0)
    for res in (buy, sell):
        for model in bt.ALL_EXIT_MODELS:
            outcome, px, _ts, bars = res[model]
            assert outcome == TradeOutcome.LOSS and bars == pad + 1
    assert buy[ExitModel.WORST_CASE][1] == 95.0 and sell[ExitModel.WORST_CASE][1] == 105.0


@pytest.mark.parametrize("pad", [0, 80], ids=["short-scan", "long-scan"])
def test_price_touching_tp_exactly_is_a_win(pad):
    quiet = [(100.0, 101.0, 99.0, 100.5)] * pad
    res = _scan(BUY, quiet + [(100.0, 110.0, 99.5, 109.0)], sl=95.0, target=110.0)
    assert res[ExitModel.WORST_CASE][0] == TradeOutcome.WIN
    assert res[ExitModel.WORST_CASE][1] == 110.0


@pytest.mark.parametrize("pad", [0, 80], ids=["short-scan", "long-scan"])
@pytest.mark.parametrize("green", [True, False], ids=["green-bar", "red-bar"])
def test_bar_hitting_sl_and_tp_splits_by_exit_model(pad, green):
    quiet = [(100.0, 101.0, 99.0, 100.5)] * pad
    o, c = (99.0, 104.0) if green else (104.0, 99.0)
    buy = _scan(BUY, quiet + [(o, 111.0, 94.0, c)], sl=95.0, target=110.0)
    sell = _scan(SELL, quiet + [(o, 106.0, 89.0, c)], sl=105.0, target=90.0)
    for res, sl, tp, bias_wins in ((buy, 95.0, 110.0, green), (sell, 105.0, 90.0, not green)):
        assert res[ExitModel.WORST_CASE][:2] == (TradeOutcome.LOSS, sl)
        assert res[ExitModel.BEST_CASE][:2] == (TradeOutcome.WIN, tp)
        want = (TradeOutcome.WIN, tp) if bias_wins else (TradeOutcome.LOSS, sl)
        assert res[ExitModel.CANDLE_BIAS][:2] == want
        for model in bt.ALL_EXIT_MODELS:
            assert res[model][3] == pad + 1


# ---------------------------------------------------------------- pullback limit fill search

def _fill(direction, bars, limit, sl, start=0, max_bars=10):
    arr = np.array(bars, dtype=float)
    return hc.find_limit_fill_index(direction, limit, sl, arr[:, 1], arr[:, 2], arr[:, 0], start, max_bars)


@pytest.mark.parametrize("start", [0, 2])
def test_limit_touched_on_the_first_bar_fills_on_that_bar(start):
    away_buy, away_sell = (105.0, 106.0, 104.0, 105.5), (95.0, 96.0, 94.0, 94.5)
    lead_buy, lead_sell = [away_buy] * start, [away_sell] * start
    assert _fill(BUY, lead_buy + [(103.0, 104.0, 99.5, 101.0)], 100.0, 90.0, start) == start
    assert _fill(SELL, lead_sell + [(97.0, 100.5, 96.0, 99.0)], 100.0, 110.0, start) == start
    assert _fill(BUY, lead_buy + [away_buy, (103.0, 104.0, 99.5, 101.0)], 100.0, 90.0, start) == start + 1


def test_limit_never_touched_or_gapped_past_sl_does_not_fill():
    assert _fill(BUY, [(105.0, 106.0, 101.0, 105.0)] * 5, 100.0, 90.0) is None
    assert _fill(SELL, [(95.0, 99.0, 94.0, 95.0)] * 5, 100.0, 110.0) is None
    assert _fill(BUY, [(105.0, 106.0, 101.0, 105.0), (88.0, 89.0, 85.0, 86.0)], 100.0, 90.0) is None
    assert _fill(BUY, [(105.0, 106.0, 101.0, 105.0)] * 3 + [(101.0, 102.0, 99.0, 100.0)], 100.0, 90.0,
                 max_bars=3) is None


# ---------------------------------------------------------------- equity floor

def _sig(entry=100.0, sl=90.0):
    return logic.TradeSignal(
        direction=BUY, hammer_candle=logic.Candle("s", 95, 101, sl, 100),
        entry_candle=logic.Candle("e", entry, entry + 1, entry - 1, entry),
        entry_price=entry, stop_loss=sl, risk=entry - sl, rr_multiple=2.0,
        target=entry + 2 * (entry - sl), timeframe="1h",
    )


def test_percent_sizing_halts_at_the_equity_floor():
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY, risk_pct_of_equity=1.0,
        starting_capital=10000.0, equity_floor_usd=5000.0,
    )
    assert bt.calculate_position_size(_sig(), cfg, 5000.0) == (0.0, 0.0)
    assert bt.calculate_position_size(_sig(), cfg, 4000.0) == (0.0, 0.0)
    size, risk_usd = bt.calculate_position_size(_sig(), cfg, 6000.0)
    assert risk_usd == pytest.approx(60.0) and size == pytest.approx(6.0)


# ---------------------------------------------------------------- HWC 35% gap fill

def _df(bars):
    return pl.DataFrame({
        "datetime": [T0 + timedelta(hours=i) for i in range(len(bars))],
        "open": [b[0] for b in bars], "high": [b[1] for b in bars],
        "low": [b[2] for b in bars], "close": [b[3] for b in bars],
    })


@pytest.mark.parametrize("direction", [BUY, SELL])
def test_pullback_limit_gapped_through_fills_at_the_better_open_with_tp_fixed(direction):
    is_buy = direction == BUY
    # BUY: limit 100, SL 90, TP 120; bar 2 opens 98 (below limit, above SL) → fill 98.
    # SELL mirror: limit 100, SL 110, TP 80; bar 2 opens 102 → fill 102.
    limit, sl, tp = 100.0, (90.0 if is_buy else 110.0), (120.0 if is_buy else 80.0)
    gap_open = 98.0 if is_buy else 102.0
    bars = [
        (104.0, 105.0, 103.0, 104.5) if is_buy else (96.0, 97.0, 95.0, 95.5),
        (104.5, 105.0, 103.5, 104.0) if is_buy else (95.5, 96.5, 95.0, 96.0),
        (gap_open, gap_open + 1, gap_open - 1, gap_open) if is_buy else (gap_open, gap_open + 1, gap_open - 1, gap_open),
        (98.0, 121.0, 97.5, 120.5) if is_buy else (102.0, 102.5, 79.0, 79.5),
    ]
    candles = bt.df_to_candles(_df(bars))
    sig = logic.TradeSignal(
        direction=direction, hammer_candle=candles[0], entry_candle=candles[1],
        entry_price=limit, stop_loss=sl, risk=abs(limit - sl), rr_multiple=2.0, target=tp,
        timeframe="1h", await_limit_fill=True, signal_entry_price=104.0 if is_buy else 96.0,
        entry_pullback_pct=35.0,
    )
    cfg = BacktestConfig(
        pattern_type=hc.PATTERN_TYPE_35, strategy_config=hc.HammerContextConfig(entry_pullback_pct=35.0),
        timeframes_to_test=["1hour"], max_forward_candles=10, allow_overlapping_trades=True,
    )
    with patch.object(bt, "load_candles_df", return_value=_df(bars)), \
         patch.object(bt.hwc35_logic, "run_strategy", return_value=[sig]):
        ledger, _ = bt.run_full_backtest(cfg)
    assert len(ledger) == 1
    s = ledger[0].signal
    assert s.entry_price == gap_open
    assert s.risk == pytest.approx(abs(gap_open - sl))
    assert s.target == tp
    assert ledger[0].outcomes[ExitModel.WORST_CASE][0] == TradeOutcome.WIN


def test_pullback_limit_never_reached_is_ignored_not_traded():
    # BUY limit 100 (SL 90) while price stays at 103-106: never fills.
    bars = [(104.0, 105.0, 103.0, 104.5)] * 3 + [(104.0, 130.0, 103.0, 129.0)]
    candles = bt.df_to_candles(_df(bars))
    sig = logic.TradeSignal(
        direction=BUY, hammer_candle=candles[0], entry_candle=candles[1],
        entry_price=100.0, stop_loss=90.0, risk=10.0, rr_multiple=2.0, target=128.0,
        timeframe="1h", await_limit_fill=True, signal_entry_price=104.0, entry_pullback_pct=35.0,
    )
    cfg = BacktestConfig(
        pattern_type=hc.PATTERN_TYPE_35, strategy_config=hc.HammerContextConfig(entry_pullback_pct=35.0),
        timeframes_to_test=["1hour"], max_forward_candles=10, allow_overlapping_trades=True,
    )
    with patch.object(bt, "load_candles_df", return_value=_df(bars)), \
         patch.object(bt.hwc35_logic, "run_strategy", return_value=[sig]):
        ledger, ignored = bt.run_full_backtest(cfg)
    assert ledger == []
    assert [s.ignore_reason for s in ignored] == ["Limit not filled within scan window"]


# ---------------------------------------------------------------- metrics

def _metrics_df(rows):
    return pl.DataFrame({
        "exit_model": ["WORST_CASE"] * len(rows),
        "outcome": [r[0] for r in rows],
        "pnl_usd": [r[1] for r in rows],
        "bars_held": [3] * len(rows),
        "risk_usd": [100.0] * len(rows),
        "entry_time": [T0 + timedelta(hours=i) for i in range(len(rows))],
        "exit_time": [T0 + timedelta(hours=i, minutes=30) for i in range(len(rows))],
    })


def test_win_rate_counts_only_closed_trades():
    df = _metrics_df([
        ("WIN", 200.0), ("LOSS", -100.0), ("SKIPPED_OVERLAP", 0.0), ("STILL_OPEN", 0.0), ("WIN", 200.0),
    ])
    row = bt.compute_metrics_grouped(df, [], 10000.0).row(0, named=True)
    assert row["total_trades"] == 3
    assert row["win_rate_pct"] == pytest.approx(200.0 / 3)
    assert row["profit_factor"] == pytest.approx(4.0)
    assert row["net_pnl"] == pytest.approx(300.0)


def test_drawdown_is_measured_from_the_running_peak():
    # 10000 → 9970 → 10070 (peak) → 9990: worst drop is 80 from the 10070 peak
    df = _metrics_df([("LOSS", -30.0), ("WIN", 100.0), ("LOSS", -80.0)])
    row = bt.compute_metrics_grouped(df, [], 10000.0).row(0, named=True)
    assert row["max_drawdown_usd"] == pytest.approx(80.0)
    assert row["max_drawdown_pct"] == pytest.approx(80.0 / 10070.0 * 100.0)
    assert row["ending_capital"] == pytest.approx(9990.0)
    assert row["max_consecutive_losses"] == 1


# ---------------------------------------------------------------- indicator combine mode

def test_indicator_any_mode_passes_when_one_indicator_agrees():
    checks = [(True, ""), (False, "RSI below 50")]
    assert _combine_checks(IndicatorStackConfig(combine_mode=IndicatorCombineMode.ANY), checks)[0] is True
    ok, reason = _combine_checks(IndicatorStackConfig(combine_mode=IndicatorCombineMode.ALL), checks)
    assert ok is False and reason == "RSI below 50"
    assert _combine_checks(
        IndicatorStackConfig(combine_mode=IndicatorCombineMode.ANY), [(False, "a"), (False, "b")],
    )[0] is False


# ---------------------------------------------------------------- Live: Limit ± offset base

def _engine(order_mode, **cfg):
    broker = MagicMock()
    broker.get_tick_prices.return_value = (2651.9, 2652.0)
    broker.symbol_point.return_value = 0.01
    broker.limit_price_with_offset.side_effect = lambda sym, d, base, pts: (base - pts * 0.01, True)
    opts = dict(order_mode=order_mode, max_entry_deviation_points=0.0,
                limit_offset_from_market=True, limit_offset_points=50.0)
    opts.update(cfg)
    eng = LiveTradingEngine.__new__(LiveTradingEngine)
    eng.broker = broker
    eng.live_config = SimpleNamespace(**opts)
    eng.strategy_config = logic.StrategyConfig()
    eng.pattern_type = "hammer"
    eng._broker_lock = MagicMock()
    eng._broker_lock.__enter__ = MagicMock(return_value=None)
    eng._broker_lock.__exit__ = MagicMock(return_value=False)
    eng.log = lambda *_a, **_k: None
    return eng


def _live_sig(entry=2650.0):
    return logic.TradeSignal(
        direction=BUY, hammer_candle=logic.Candle("s", 2645, 2651, 2640, 2649),
        entry_candle=logic.Candle("e", entry, entry + 5, entry - 5, entry),
        entry_price=entry, stop_loss=2639.7, risk=entry - 2639.7, rr_multiple=2.0,
        target=entry + 2 * (entry - 2639.7), timeframe="5m",
    )


@pytest.mark.parametrize("from_market, base", [(True, 2652.0), (False, 2650.0)])
def test_limit_offset_base_follows_the_from_market_setting(from_market, base):
    eng = _engine("limit_offset", limit_offset_from_market=from_market)
    mode, limit_px, _sl, _tp, exec_entry = eng._resolve_live_order(_live_sig(), "XAUUSD")
    assert mode == "limit_entry"
    assert limit_px == pytest.approx(base - 0.5) and exec_entry == pytest.approx(base - 0.5)
    assert eng.broker.limit_price_with_offset.call_args.args[2] == pytest.approx(base)


# ---------------------------------------------------------------- Live: real-money risk gate

def _live_cfg(**kw):
    base = dict(symbol="XAUUSD", timeframe_label="5m", volume=0.1, magic=1, max_open_positions=1,
                poll_interval_sec=2.0, max_daily_trades=5, max_daily_loss_usd=200.0, max_lot_size=1.0,
                dry_run=False)
    base.update(kw)
    return live_trading.LiveRunConfig(**base)


def test_real_money_needs_every_risk_limit():
    assert live_trading.validate_risk_config(_live_cfg(), real_money=True) is None
    for bad in (dict(max_daily_loss_usd=0.0), dict(max_daily_trades=0),
                dict(volume=2.0, max_lot_size=1.0), dict(volume=0.0, max_lot_size=0.0)):
        assert live_trading.validate_risk_config(_live_cfg(**bad), real_money=True), bad
        assert live_trading.validate_risk_config(_live_cfg(**bad), real_money=False) is None
        assert live_trading.validate_risk_config(_live_cfg(**{**bad, "dry_run": True}), real_money=True) is None
