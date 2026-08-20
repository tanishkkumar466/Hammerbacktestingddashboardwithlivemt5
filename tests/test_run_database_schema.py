"""Run history SQLite schema: extended metrics + breakdown tables."""

import os
import tempfile
from datetime import datetime

import polars as pl

from backtest import BacktestConfig, PositionSizingMode, compute_metrics_grouped, ledger_to_polars, run_full_backtest
import hammer_context_logic as hc
import logic
from dashboard import RunDatabase, _metrics_from_row


def _sample_tables():
    ratios = logic.HammerRatioConfig(body_pct=20.0, body_tol=25.0)
    tfset = {"1h": logic.TimeframeSetting(1.6, 18.0)}
    cfg = hc.HammerContextConfig(
        buy_hammer_ratios=ratios,
        sell_hammer_ratios=ratios,
        buy_require_wick=False,
        sell_require_wick=False,
        lookback_candles=2,
        timeframe_settings=tfset,
    )
    bt = BacktestConfig(
        strategy_config=cfg,
        pattern_type="hammer_with_candles",
        symbol="XAUUSD",
        data_root="data",
        start_date=datetime(2026, 1, 1),
        allow_overlapping_trades=False,
        timeframes_to_test=["1hour"],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
    )
    ledger, _ = run_full_backtest(bt)
    df = ledger_to_polars(ledger)
    return bt, {
        "overall": compute_metrics_grouped(df, [], 10000.0),
        "by_session": compute_metrics_grouped(df, ["session"], 10000.0),
        "by_timeframe": compute_metrics_grouped(df, ["timeframe"], 10000.0),
        "by_direction": compute_metrics_grouped(df, ["direction"], 10000.0),
        "by_year": compute_metrics_grouped(df, ["year"], 10000.0),
        "by_month": compute_metrics_grouped(df, ["month_key"], 10000.0),
        "ledger": df,
    }


def test_run_database_schema_migration_and_breakdown():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_run_history.db")
        db = RunDatabase(db_path)

        bt, tables = _sample_tables()
        sid, dup, _ = db.save_run(bt, tables, os.path.join(tmp, "out"), os.path.join(tmp, "plots"))
        assert dup is False
        assert sid is not None

        history = db.fetch_run_history()
        assert len(history) == 1
        row = history[0]
        assert row["PatternType"] == "hammer_with_candles"
        assert row["NetProfitWC"] is not None

        breakdown = db.fetch_metrics_breakdown(sid, "session", "worst_case")
        assert len(breakdown) >= 1
        groups = {r["GroupName"] for r in breakdown}
        assert groups <= {"Asian", "London", "US"}

        results = db.fetch_backtest_results(sid)
        assert len(results) == 3
        wc = next(r for r in results if r["ExitModel"] == "worst_case")
        assert wc["GrossProfit"] is not None
        assert wc["TotalSignals"] is not None

        sid2, dup2, _ = db.save_run(bt, tables, os.path.join(tmp, "out2"), os.path.join(tmp, "plots2"))
        assert dup2 is True


def test_metrics_from_row_maps_extended_fields():
    row = {
        "total_signals": 10,
        "total_trades": 8,
        "skipped_overlap": 1,
        "still_open": 1,
        "wins": 5,
        "losses": 3,
        "win_rate_pct": 62.5,
        "gross_profit": 500.0,
        "gross_loss": -200.0,
        "net_pnl": 300.0,
        "avg_win_usd": 100.0,
        "avg_loss_usd": -66.67,
        "profit_factor": 2.5,
        "expectancy_usd": 37.5,
        "max_drawdown_usd": 150.0,
        "max_drawdown_pct": 1.5,
        "sharpe_ratio": 1.1,
        "sortino_ratio": 1.4,
        "total_return_pct": 3.0,
    }
    mapped = _metrics_from_row(row)
    assert mapped["TotalSignals"] == 10
    assert mapped["SkippedOverlap"] == 1
    assert mapped["SortinoRatio"] == 1.4
    assert mapped["Verdict"] == "Promising"
