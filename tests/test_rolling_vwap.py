import numpy as np

import logic
from indicators.config import IndicatorStackConfig, RollingVWAPConfig
from indicators.filter import apply_indicator_filters
from indicators.rolling_vwap import compute_rolling_vwap


def test_rolling_vwap_warmup_and_window():
    high = np.array([11.0, 12.0, 13.0, 14.0, 15.0])
    low = np.array([9.0, 10.0, 11.0, 12.0, 13.0])
    close = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    vol = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    typical = (high + low + close) / 3.0
    out = compute_rolling_vwap(high, low, close, vol, period=3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert abs(out[2] - typical[:3].mean()) < 1e-12
    assert abs(out[3] - typical[1:4].mean()) < 1e-12
    assert abs(out[4] - typical[2:5].mean()) < 1e-12


def test_rolling_vwap_volume_weights():
    high = low = close = np.array([10.0, 20.0, 30.0])
    vol = np.array([1.0, 1.0, 8.0])
    out = compute_rolling_vwap(high, low, close, vol, period=3)
    expected = (10.0 * 1 + 20.0 * 1 + 30.0 * 8) / 10.0
    assert abs(out[2] - expected) < 1e-12


def test_rolling_vwap_filter_rejects_buy_below():
    import polars as pl
    from datetime import datetime, timedelta

    n = 6
    start = datetime(2024, 1, 1, 10, 0)
    times = [start + timedelta(minutes=i) for i in range(n)]
    # Price sits below a rising typical-price window after warmup.
    close = np.array([100.0, 100.0, 100.0, 100.0, 100.0, 90.0])
    high = close + 1.0
    low = close - 1.0
    df = pl.DataFrame({
        "datetime": times,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.ones(n),
    })
    hammer = logic.Candle(timestamp=times[-1], open=90.0, high=91.0, low=89.0, close=90.0)
    nxt = logic.Candle(timestamp=times[-1], open=90.0, high=91.0, low=89.0, close=90.0)
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=hammer,
        entry_candle=nxt,
        entry_price=90.0,
        stop_loss=89.0,
        risk=1.0,
        rr_multiple=1.6,
        target=91.6,
        timeframe="3m",
    )
    stack = IndicatorStackConfig(
        rolling_vwap=RollingVWAPConfig(enabled=True, period=3, apply_trade_filter=True),
    )
    out = apply_indicator_filters([sig], df, stack)
    assert out[0].ignored
    assert "Rolling VWAP" in (out[0].ignore_reason or "")
