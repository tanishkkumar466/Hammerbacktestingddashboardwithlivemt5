"""Regression: live forming-bar tick merge must match MT5 bid OHLC charts."""

from datetime import datetime
from types import SimpleNamespace

import logic
from broker import MT5Broker


def test_forming_tick_merge_prefers_bid_close_not_ask():
    forming = logic.Candle(
        timestamp=datetime(2026, 9, 19, 6, 0, 0),
        open=81200.0,
        high=81250.0,
        low=81190.0,
        close=81210.0,
        volume=10.0,
    )
    tick = SimpleNamespace(bid=81319.70, ask=81324.70, last=0.0)
    merged = MT5Broker._merge_tick_into_forming(
        forming, tick, forming_age_sec=30.0, tf_sec=300,
    )
    assert merged.close == 81319.70
    assert merged.high >= 81324.70  # ask still expands range
    assert merged.open == 81200.0


def test_forming_tick_merge_skips_stale_bar():
    forming = logic.Candle(
        timestamp=datetime(2026, 9, 19, 6, 0, 0),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
    )
    tick = SimpleNamespace(bid=200.0, ask=201.0, last=0.0)
    merged = MT5Broker._merge_tick_into_forming(
        forming, tick, forming_age_sec=900.0, tf_sec=60,
    )
    assert merged.close == 100.5
