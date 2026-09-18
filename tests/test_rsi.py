from datetime import datetime, timedelta

import numpy as np
import polars as pl

import logic
from indicators.config import IndicatorStackConfig, RSIConfig
from indicators.filter import apply_indicator_filters
from indicators.rsi import compute_rsi


def test_rsi_warmup_and_bounds():
    close = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 6.0, 5.0], dtype=float)
    out = compute_rsi(close, period=3)
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[2])
    assert not np.isnan(out[3])
    assert np.nanmin(out) >= 0.0
    assert np.nanmax(out) <= 100.0


def test_rsi_all_up_is_100():
    close = np.arange(1.0, 20.0)
    out = compute_rsi(close, period=14)
    assert out[-1] == 100.0


def test_rsi_filter_buy_below_threshold():
    n = 20
    start = datetime(2024, 1, 1, 10, 0)
    times = [start + timedelta(minutes=i) for i in range(n)]
    # Drift down so RSI is weak on the last bar.
    close = np.linspace(100.0, 80.0, n)
    df = pl.DataFrame({
        "datetime": times,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.ones(n),
    })
    hammer = logic.Candle(
        timestamp=times[-1], open=float(close[-1]), high=float(close[-1]) + 0.5,
        low=float(close[-1]) - 0.5, close=float(close[-1]),
    )
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=hammer,
        entry_candle=hammer,
        entry_price=float(close[-1]),
        stop_loss=float(close[-1]) - 1.0,
        risk=1.0,
        rr_multiple=1.6,
        target=float(close[-1]) + 1.6,
        timeframe="3m",
    )
    stack = IndicatorStackConfig(
        rsi=RSIConfig(
            enabled=True, period=14, buy_above=50.0, sell_below=60.0,
            apply_trade_filter=True,
        ),
    )
    out = apply_indicator_filters([sig], df, stack)
    assert out[0].ignored
    assert "RSI" in (out[0].ignore_reason or "")


def _ohlc_df(close: np.ndarray):
    n = len(close)
    start = datetime(2024, 1, 1, 10, 0)
    times = [start + timedelta(minutes=i) for i in range(n)]
    return times, pl.DataFrame({
        "datetime": times,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.ones(n),
    })


def _sig(direction, ts, price) -> logic.TradeSignal:
    candle = logic.Candle(
        timestamp=ts, open=price, high=price + 0.5, low=price - 0.5, close=price,
    )
    return logic.TradeSignal(
        direction=direction,
        hammer_candle=candle,
        entry_candle=candle,
        entry_price=price,
        stop_loss=price - 1.0 if direction == logic.TradeDirection.BUY else price + 1.0,
        risk=1.0,
        rr_multiple=1.6,
        target=price + 1.6 if direction == logic.TradeDirection.BUY else price - 1.6,
        timeframe="3m",
    )


def test_rsi_allows_buy_when_above_50():
    from indicators.filter import verify_signal_passes_indicators_at_bar

    close = np.arange(1.0, 21.0)
    times, df = _ohlc_df(close)
    stack = IndicatorStackConfig(
        rsi=RSIConfig(
            enabled=True, period=14, buy_above=50.0, sell_below=60.0,
            apply_trade_filter=True,
        ),
    )
    sig = _sig(logic.TradeDirection.BUY, times[-1], float(close[-1]))
    out = apply_indicator_filters([sig], df, stack)
    assert not out[0].ignored
    ok, _ = verify_signal_passes_indicators_at_bar(
        df, len(close) - 1, logic.TradeDirection.BUY, stack,
    )
    assert ok


def test_rsi_blocks_sell_when_not_below_60():
    from indicators.filter import verify_signal_passes_indicators_at_bar

    close = np.arange(1.0, 21.0)  # RSI=100 on last bar
    times, df = _ohlc_df(close)
    stack = IndicatorStackConfig(
        rsi=RSIConfig(
            enabled=True, period=14, buy_above=50.0, sell_below=60.0,
            apply_trade_filter=True,
        ),
    )
    sig = _sig(logic.TradeDirection.SELL, times[-1], float(close[-1]))
    out = apply_indicator_filters([sig], df, stack)
    assert out[0].ignored
    ok, reason = verify_signal_passes_indicators_at_bar(
        df, len(close) - 1, logic.TradeDirection.SELL, stack,
    )
    assert not ok
    assert "RSI" in reason
