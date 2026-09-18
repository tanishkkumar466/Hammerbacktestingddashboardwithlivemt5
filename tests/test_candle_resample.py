"""Unit tests for custom timeframe resampling from 1-minute bars."""
from __future__ import annotations

from datetime import datetime, timedelta

import candle_resample as cr


def _bar(dt: datetime, o: float, h: float, low: float, c: float, vol: int = 1) -> dict:
    return {"datetime": dt, "open": o, "high": h, "low": low, "close": c, "volume": vol}


def test_folder_and_logic_labels():
    assert cr.folder_for_minutes(4) == "4min"
    assert cr.folder_for_minutes(12) == "12min"
    assert cr.folder_for_minutes(60) == "1hour"
    assert cr.logic_label_for_folder("4min") == "4m"
    assert cr.logic_label_for_folder("12min") == "12m"
    assert cr.logic_label_for_folder("1hour") == "1h"
    assert cr.minutes_from_folder("4min") == 4
    assert cr.folder_for_logic_label("4m") == "4min"
    assert cr.folder_for_logic_label("12m") == "12min"
    assert cr.folder_for_logic_label("1h") == "1hour"


def test_resample_4min_ohlc():
    start = datetime(2024, 1, 2, 10, 0, 0)
    # 8 one-minute bars -> two 4-minute buckets
    candles = []
    prices = [
        (100, 101, 99, 100.5),
        (100.5, 102, 100, 101),
        (101, 103, 100.5, 102),
        (102, 102.5, 101, 101.5),
        (101.5, 104, 101, 103),
        (103, 105, 102, 104),
        (104, 104.5, 103, 103.5),
        (103.5, 106, 103, 105),
    ]
    for i, (o, h, low, c) in enumerate(prices):
        candles.append(_bar(start + timedelta(minutes=i), o, h, low, c, vol=i + 1))

    out = cr.resample_ohlc_candles(candles, 4)
    assert len(out) == 2
    assert out[0]["datetime"] == start
    assert out[0]["open"] == 100
    assert out[0]["high"] == 103
    assert out[0]["low"] == 99
    assert out[0]["close"] == 101.5
    assert out[0]["volume"] == 1 + 2 + 3 + 4

    assert out[1]["datetime"] == start + timedelta(minutes=4)
    assert out[1]["open"] == 101.5
    assert out[1]["high"] == 106
    assert out[1]["low"] == 101
    assert out[1]["close"] == 105


def test_resample_aligns_to_wall_clock():
    # 10:01-10:03 should land in 10:00 bucket for 4m
    start = datetime(2024, 6, 1, 10, 1, 0)
    candles = [
        _bar(start, 1, 2, 0.5, 1.5),
        _bar(start + timedelta(minutes=1), 1.5, 3, 1, 2),
        _bar(start + timedelta(minutes=2), 2, 2.5, 1.8, 2.2),
    ]
    out = cr.resample_ohlc_candles(candles, 4)
    assert len(out) == 1
    assert out[0]["datetime"] == datetime(2024, 6, 1, 10, 0, 0)
    assert out[0]["open"] == 1
    assert out[0]["close"] == 2.2
