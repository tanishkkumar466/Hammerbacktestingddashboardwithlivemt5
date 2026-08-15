"""Unit tests for the three Run Settings position-sizing modes."""

from datetime import datetime, timedelta
from typing import List, Tuple

import logic
from backtest import (
    BacktestConfig,
    ExitModel,
    PositionSizingMode,
    TradeOutcome,
    calculate_pnl,
    calculate_position_size,
    settle_position_sizing,
    validate_position_sizing_config,
)


def _candle(ts: datetime, price: float) -> logic.Candle:
    return logic.Candle(timestamp=ts, open=price, high=price, low=price, close=price)


def _signal(
    *,
    entry: float,
    sl: float,
    rr: float,
    when: datetime,
    direction: logic.TradeDirection = logic.TradeDirection.BUY,
) -> logic.TradeSignal:
    risk = abs(entry - sl)
    if direction == logic.TradeDirection.BUY:
        target = entry + risk * rr
    else:
        target = entry - risk * rr
    hammer = _candle(when - timedelta(minutes=3), entry)
    nxt = _candle(when, entry)
    return logic.TradeSignal(
        direction=direction,
        hammer_candle=hammer,
        entry_candle=nxt,
        entry_price=entry,
        stop_loss=sl,
        risk=risk,
        rr_multiple=rr,
        target=target,
        timeframe="3m",
    )


def _row(sig: logic.TradeSignal, outcome: TradeOutcome, exit_price: float, exit_time: datetime):
    return (sig, outcome, exit_price, exit_time, 1)


def _bundle(rows: List[Tuple], tf: str = "3min"):
    filtered = {m: list(rows) for m in ExitModel}
    return [(tf, filtered)]


def test_fixed_units_ignores_equity():
    sig = _signal(entry=2000.0, sl=1990.0, rr=2.0, when=datetime(2024, 1, 1, 12, 0))
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.FIXED_UNITS,
        position_size=2.0,
        starting_capital=10_000.0,
    )
    size_lo, risk_lo = calculate_position_size(sig, cfg, 500.0)
    size_hi, risk_hi = calculate_position_size(sig, cfg, 5_000_000.0)
    assert size_lo == size_hi == 2.0
    assert abs(risk_lo - 20.0) < 1e-9  # 2 units * $10 risk
    assert abs(risk_hi - 20.0) < 1e-9


def test_fixed_risk_usd_does_not_compound():
    sig = _signal(entry=2000.0, sl=1990.0, rr=2.0, when=datetime(2024, 1, 1, 12, 0))
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10_000.0,
        equity_floor_usd=0.0,
    )
    size_a, risk_a = calculate_position_size(sig, cfg, 10_000.0)
    size_b, risk_b = calculate_position_size(sig, cfg, 1_000_000.0)
    size_c, risk_c = calculate_position_size(sig, cfg, -50.0)  # scoreboard can go negative
    assert abs(risk_a - 100.0) < 1e-9
    assert abs(risk_b - 100.0) < 1e-9
    assert abs(risk_c - 100.0) < 1e-9
    assert abs(size_a - 10.0) < 1e-9  # $100 / $10
    assert abs(size_a - size_b) < 1e-9
    assert abs(size_a - size_c) < 1e-9

    win_pnl = calculate_pnl(sig, TradeOutcome.WIN, sig.target, size_a, cfg)
    loss_pnl = calculate_pnl(sig, TradeOutcome.LOSS, sig.stop_loss, size_a, cfg)
    assert abs(loss_pnl + 100.0) < 1e-6
    assert abs(win_pnl - 200.0) < 1e-6  # 2R


def test_percent_of_equity_compounds_but_caps():
    sig = _signal(entry=2000.0, sl=1990.0, rr=2.0, when=datetime(2024, 1, 1, 12, 0))
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY,
        risk_pct_of_equity=1.0,
        starting_capital=10_000.0,
        max_equity_multiple=50.0,
        equity_floor_usd=0.0,
    )
    size_start, risk_start = calculate_position_size(sig, cfg, 10_000.0)
    assert abs(risk_start - 100.0) < 1e-9
    size_grown, risk_grown = calculate_position_size(sig, cfg, 20_000.0)
    assert abs(risk_grown - 200.0) < 1e-9
    assert size_grown > size_start

    _size_capped, risk_capped = calculate_position_size(sig, cfg, 10_000.0 * 200)
    assert abs(risk_capped - (10_000.0 * 50 * 0.01)) < 1e-6

    size_dead, risk_dead = calculate_position_size(sig, cfg, 0.0)
    assert size_dead == 0.0 and risk_dead == 0.0


def test_fixed_risk_shared_account_two_timeframes():
    t0 = datetime(2024, 1, 1, 12, 0)
    t1 = datetime(2024, 1, 1, 13, 0)
    sig_a = _signal(entry=2000.0, sl=1990.0, rr=2.0, when=t0)
    sig_b = _signal(entry=2010.0, sl=2000.0, rr=2.0, when=t1)
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10_000.0,
        commission_per_trade=0.0,
        slippage_usd=0.0,
    )
    rows_a = [_row(sig_a, TradeOutcome.WIN, sig_a.target, t0 + timedelta(minutes=3))]
    rows_b = [_row(sig_b, TradeOutcome.LOSS, sig_b.stop_loss, t1 + timedelta(minutes=3))]
    ledger = settle_position_sizing(
        [("3min", {m: rows_a for m in ExitModel}), ("5min", {m: rows_b for m in ExitModel})],
        cfg,
    )
    assert len(ledger) == 2
    wc = ExitModel.WORST_CASE
    pnls = [t.pnl[wc] for t in ledger]
    assert abs(pnls[0] - 200.0) < 1e-6
    assert abs(pnls[1] + 100.0) < 1e-6
    ending = ledger[-1].equity_after[wc]
    assert abs(ending - 10_100.0) < 1e-6  # 10k + 200 - 100
    # sizes must stay constant (no compounding)
    assert abs(ledger[0].risk_usd[wc] - 100.0) < 1e-9
    assert abs(ledger[1].risk_usd[wc] - 100.0) < 1e-9


def test_percent_does_not_double_compound_across_timeframes():
    t0 = datetime(2024, 1, 1, 12, 0)
    t1 = datetime(2024, 1, 1, 13, 0)
    sig_a = _signal(entry=2000.0, sl=1990.0, rr=2.0, when=t0)
    sig_b = _signal(entry=2010.0, sl=2000.0, rr=2.0, when=t1)
    cfg = BacktestConfig(
        position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY,
        risk_pct_of_equity=10.0,
        starting_capital=10_000.0,
        max_equity_multiple=50.0,
        commission_per_trade=0.0,
        slippage_usd=0.0,
    )
    rows_a = [_row(sig_a, TradeOutcome.WIN, sig_a.target, t0 + timedelta(minutes=3))]
    rows_b = [_row(sig_b, TradeOutcome.WIN, sig_b.target, t1 + timedelta(minutes=3))]
    ledger = settle_position_sizing(
        [("3min", {m: rows_a for m in ExitModel}), ("5min", {m: rows_b for m in ExitModel})],
        cfg,
    )
    wc = ExitModel.WORST_CASE
    # First trade risks 10% of 10k = 1000, 2R win = +2000 → equity 12000
    assert abs(ledger[0].risk_usd[wc] - 1000.0) < 1e-6
    assert abs(ledger[0].pnl[wc] - 2000.0) < 1e-6
    # Second trade must size off 12000, NOT a fresh 10000 (that was the bug)
    assert abs(ledger[1].risk_usd[wc] - 1200.0) < 1e-6
    assert abs(ledger[1].pnl[wc] - 2400.0) < 1e-6
    assert abs(ledger[1].equity_after[wc] - 14_400.0) < 1e-6


def test_validate_rejects_bad_inputs():
    bad = BacktestConfig(position_sizing_mode=PositionSizingMode.FIXED_RISK_USD, fixed_risk_usd=0)
    assert validate_position_sizing_config(bad)
    bad2 = BacktestConfig(position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY, risk_pct_of_equity=150)
    assert validate_position_sizing_config(bad2)
    ok = BacktestConfig(position_sizing_mode=PositionSizingMode.FIXED_UNITS, position_size=0.1)
    assert validate_position_sizing_config(ok) is None


if __name__ == "__main__":
    tests = [
        test_fixed_units_ignores_equity,
        test_fixed_risk_usd_does_not_compound,
        test_percent_of_equity_compounds_but_caps,
        test_fixed_risk_shared_account_two_timeframes,
        test_percent_does_not_double_compound_across_timeframes,
        test_validate_rejects_bad_inputs,
    ]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print("all position-sizing tests passed")
