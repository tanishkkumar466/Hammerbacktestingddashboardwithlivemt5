"""Metrics: avg_win_usd must equal gross_profit / wins."""

from datetime import datetime

import polars as pl
import logic
import hammer_context_logic as hc
from backtest import (
    BacktestConfig,
    PositionSizingMode,
    compute_metrics_grouped,
    ledger_to_polars,
    run_full_backtest,
)


def test_avg_win_equals_gross_profit_over_wins():
    ratios = logic.HammerRatioConfig(body_pct=20.0, body_tol=25.0)
    tfset = {
        "1h": logic.TimeframeSetting(1.6, 18.0),
        "30m": logic.TimeframeSetting(1.6, 15.0),
        "15m": logic.TimeframeSetting(2.0, 15.0),
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
        timeframes_to_test=["1hour", "30min", "15min"],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
    )
    ledger, _ = run_full_backtest(bt)
    df = ledger_to_polars(ledger)
    by_tf = compute_metrics_grouped(df, ["timeframe"], 10000.0).filter(
        pl.col("exit_model") == "worst_case"
    )
    for row in by_tf.iter_rows(named=True):
        wins = row["wins"] or 0
        if wins == 0:
            continue
        gp = row["gross_profit"] or 0.0
        avg = row["avg_win_usd"]
        assert avg is not None
        assert abs(avg * wins - gp) < 1e-6, row["group"]
        assert row["group"] in ("1h", "30m", "15m")
