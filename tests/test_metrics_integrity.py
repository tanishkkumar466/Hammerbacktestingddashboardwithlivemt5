"""Calculation integrity: partitions, PnL identities, fixed-risk loss size."""

from datetime import datetime

import polars as pl

import hammer_context_logic as hc
import logic
from backtest import (
    BacktestConfig,
    PositionSizingMode,
    TradeOutcome,
    calculate_pnl,
    compute_metrics_grouped,
    ledger_to_polars,
    run_full_backtest,
)


def _run_sample():
    ratios = logic.HammerRatioConfig(body_pct=20.0, body_tol=25.0)
    tfset = {
        "1h": logic.TimeframeSetting(1.6, 18.0),
        "30m": logic.TimeframeSetting(1.6, 15.0),
    }
    cfg = hc.HammerContextConfig(
        buy_hammer_ratios=ratios,
        sell_hammer_ratios=ratios,
        buy_require_wick=False,
        sell_require_wick=False,
        lookback_candles=2,
        enable_risk_limit=True,
        timeframe_settings=tfset,
    )
    bt = BacktestConfig(
        strategy_config=cfg,
        pattern_type="hammer_with_candles",
        symbol="XAUUSD",
        data_root="data",
        start_date=datetime(2026, 1, 1),
        allow_overlapping_trades=False,
        timeframes_to_test=["1hour", "30min"],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
        commission_per_trade=0.0,
        slippage_usd=0.0,
    )
    ledger, _ = run_full_backtest(bt)
    return ledger_to_polars(ledger)


def test_pnl_slippage_commission_round_turn():
    sig = type("S", (), {})()
    sig.direction = logic.TradeDirection.BUY
    sig.entry_price = 2000.0
    cfg = BacktestConfig(slippage_usd=0.5, commission_per_trade=2.0)
    # entry 2000.5, exit 2009.5, size 10 → gross 90 − 2 = 88
    assert abs(calculate_pnl(sig, TradeOutcome.WIN, 2010.0, 10.0, cfg) - 88.0) < 1e-9
    sig.direction = logic.TradeDirection.SELL
    assert abs(calculate_pnl(sig, TradeOutcome.WIN, 1990.0, 10.0, cfg) - 88.0) < 1e-9


def test_overall_equals_session_timeframe_direction_partitions():
    df = _run_sample()
    overall = compute_metrics_grouped(df, [], 10000.0)
    by_sess = compute_metrics_grouped(df, ["session"], 10000.0)
    by_tf = compute_metrics_grouped(df, ["timeframe"], 10000.0)
    by_dir = compute_metrics_grouped(df, ["direction"], 10000.0)

    for em in ("worst_case", "best_case", "candle_bias"):
        o = overall.filter(pl.col("exit_model") == em).to_dicts()[0]
        for part in (by_sess, by_tf, by_dir):
            p = part.filter(pl.col("exit_model") == em)
            assert abs(float(p["net_pnl"].sum()) - o["net_pnl"]) < 1e-6, em
            assert int(p["wins"].sum()) == o["wins"], em
            assert int(p["losses"].sum()) == o["losses"], em
            assert int(p["total_trades"].sum()) == o["total_trades"], em

        assert abs(o["net_pnl"] - (o["gross_profit"] + o["gross_loss"])) < 1e-6
        assert abs(o["ending_capital"] - (10000.0 + o["net_pnl"])) < 1e-4
        if o["wins"]:
            assert abs(o["avg_win_usd"] * o["wins"] - o["gross_profit"]) < 1e-4
        if o["losses"]:
            assert abs(o["avg_loss_usd"] * o["losses"] - o["gross_loss"]) < 1e-4
        if o["gross_loss"]:
            assert abs(o["profit_factor"] - o["gross_profit"] / abs(o["gross_loss"])) < 1e-6


def _pnl_from_exit(row) -> float:
    sign = 1.0 if row["direction"] == "BUY" else -1.0
    return sign * (row["exit_price"] - row["entry_price"]) * row["position_size"]


def test_fixed_risk_loss_equals_minus_risk_usd():
    """Stop fills cost exactly 1R; a bar that opens through the stop costs more."""
    df = _run_sample()
    losses = df.filter(
        (pl.col("exit_model") == "worst_case") & (pl.col("outcome") == "LOSS")
    )
    assert losses.height > 0
    at_stop = 0
    for row in losses.iter_rows(named=True):
        assert abs(row["risk_usd"] - 100.0) < 1e-6
        assert abs(row["pnl_usd"] - _pnl_from_exit(row)) < 1e-6
        if abs(row["exit_price"] - row["stop_loss"]) < 1e-9:
            at_stop += 1
            assert abs(row["pnl_usd"] + row["risk_usd"]) < 1e-6
        else:
            assert row["pnl_usd"] < -row["risk_usd"]  # gap through stop
    assert at_stop > 0


def test_fixed_risk_win_equals_risk_times_rr():
    """Target fills pay exactly RR × risk — including bars that gap past target."""
    df = _run_sample()
    wins = df.filter(
        (pl.col("exit_model") == "worst_case") & (pl.col("outcome") == "WIN")
    )
    assert wins.height > 0
    for row in wins.iter_rows(named=True):
        assert abs(row["pnl_usd"] - _pnl_from_exit(row)) < 1e-4
        assert abs(row["exit_price"] - row["target"]) < 1e-9
        expected = row["risk_usd"] * row["rr_multiple_target"]
        assert abs(row["pnl_usd"] - expected) < 1e-4
