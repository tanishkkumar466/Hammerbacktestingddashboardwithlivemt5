"""Tests for trading-session classification (Asian / London / US)."""

from datetime import datetime

import polars as pl

import sessions
from backtest import (
    ledger_to_polars,
    compute_metrics_grouped,
    SimulatedTrade,
    ExitModel,
    TradeOutcome,
)
from logic import TradeSignal, TradeDirection, Candle
import logic


def _make_trade(entry_hour: int, exit_model_pnl: float = 100.0) -> SimulatedTrade:
    ts = datetime(2026, 1, 15, entry_hour, 30, 0)
    candle = Candle(open=2000.0, high=2005.0, low=1995.0, close=2002.0, timestamp=ts)
    sig = TradeSignal(
        direction=TradeDirection.BUY,
        hammer_candle=candle,
        entry_candle=candle,
        entry_price=2002.0,
        stop_loss=1995.0,
        target=2016.0,
        risk=7.0,
        rr_multiple=2.0,
        timeframe="1hour",
    )
    outcomes = {
        ExitModel.WORST_CASE: (TradeOutcome.WIN, 2016.0, ts, 3),
        ExitModel.BEST_CASE: (TradeOutcome.WIN, 2016.0, ts, 3),
        ExitModel.CANDLE_BIAS: (TradeOutcome.WIN, 2016.0, ts, 3),
    }
    pnl = {m: exit_model_pnl for m in outcomes}
    return SimulatedTrade(
        signal=sig,
        timeframe_folder="1hour",
        entry_time=ts,
        outcomes=outcomes,
        position_size={m: 1.0 for m in outcomes},
        risk_usd={m: 100.0 for m in outcomes},
        pnl=pnl,
        equity_after={m: 10100.0 for m in outcomes},
    )


def test_classify_hour_boundaries():
    assert sessions.classify_hour(0) == sessions.SESSION_ASIAN
    assert sessions.classify_hour(7) == sessions.SESSION_ASIAN
    assert sessions.classify_hour(8) == sessions.SESSION_LONDON
    assert sessions.classify_hour(15) == sessions.SESSION_LONDON
    assert sessions.classify_hour(16) == sessions.SESSION_US
    assert sessions.classify_hour(23) == sessions.SESSION_US


def test_classify_session_datetime():
    assert sessions.classify_session(datetime(2026, 3, 1, 2, 0)) == sessions.SESSION_ASIAN
    assert sessions.classify_session(datetime(2026, 3, 1, 12, 0)) == sessions.SESSION_LONDON
    assert sessions.classify_session(datetime(2026, 3, 1, 20, 0)) == sessions.SESSION_US


def test_ledger_to_polars_adds_session_column():
    trades = [_make_trade(2), _make_trade(10), _make_trade(18)]
    df = ledger_to_polars(trades)
    assert "session" in df.columns
    sessions_found = sorted(df["session"].unique().to_list())
    assert sessions_found == sorted([sessions.SESSION_ASIAN, sessions.SESSION_LONDON, sessions.SESSION_US])


def test_compute_metrics_by_session_order():
    trades = [_make_trade(1), _make_trade(9), _make_trade(17)]
    df = ledger_to_polars(trades)
    metrics = compute_metrics_grouped(df, ["session"], starting_capital=10000.0)
    wc = metrics.filter(pl.col("exit_model") == "worst_case")
    assert wc["group"].to_list() == sessions.SESSION_ORDER


def test_backtest_session_filter_skips_disabled_session():
    from backtest import BacktestConfig, simulate_timeframe_outcomes, PositionSizingMode
    import hammer_context_logic as hc

    ratios = logic.HammerRatioConfig(body_pct=20.0, body_tol=25.0)
    tfset = {"1h": logic.TimeframeSetting(1.6, 18.0)}
    strat = hc.HammerContextConfig(
        buy_hammer_ratios=ratios,
        sell_hammer_ratios=ratios,
        buy_require_wick=False,
        sell_require_wick=False,
        lookback_candles=2,
        timeframe_settings=tfset,
    )
    cfg = BacktestConfig(
        strategy_config=strat,
        pattern_type="hammer_with_candles",
        symbol="XAUUSD",
        data_root="data",
        start_date=datetime(2026, 1, 1),
        timeframes_to_test=["1hour"],
        sessions_enabled=[sessions.SESSION_LONDON],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
    )
    _, ignored = simulate_timeframe_outcomes("1hour", cfg)
    session_ignored = [s for s in ignored if s.ignore_reason and "Session filter" in s.ignore_reason]
    assert session_ignored, "Expected some signals skipped by session filter"
    for sig in session_ignored:
        assert sessions.classify_session(sig.entry_candle.timestamp) != sessions.SESSION_LONDON
