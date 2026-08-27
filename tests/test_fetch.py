"""Tests for fetch.py data-folder detect + merge helpers."""

from datetime import datetime, timezone
from pathlib import Path

import fetch as fetcher


def test_merge_candles_dedupes_and_sorts():
    a = [
        {"datetime": datetime(2026, 1, 1, 1, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
        {"datetime": datetime(2026, 1, 1, 2, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
    ]
    b = [
        {"datetime": datetime(2026, 1, 1, 2, 0), "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 2},
        {"datetime": datetime(2026, 1, 1, 3, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
    ]
    merged = fetcher.merge_candles(a, b)
    assert len(merged) == 3
    assert merged[1]["open"] == 9
    assert merged[2]["datetime"] == datetime(2026, 1, 1, 3, 0)


def test_detect_latest_bar_from_monthly(tmp_path: Path):
    root = tmp_path / "data"
    path = fetcher.monthly_csv_path("XAUUSD", "1hour", 2026, 7, output_root=str(root))
    candles = [
        {"datetime": datetime(2026, 7, 1, 1, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"datetime": datetime(2026, 7, 2, 5, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 11},
    ]
    fetcher.write_csv(path, candles)
    latest = fetcher.detect_latest_bar("XAUUSD", "1hour", output_root=str(root))
    assert latest == datetime(2026, 7, 2, 5, 0)


def test_detect_data_folder_symbols(tmp_path: Path):
    root = tmp_path / "data"
    (root / "XAUUSD").mkdir(parents=True)
    (root / "EURUSD").mkdir(parents=True)
    assert fetcher.detect_data_folder_symbols(str(root)) == ["EURUSD", "XAUUSD"]


def test_classify_market_type_spot_vs_futures():
    assert fetcher.classify_market_type("XAUUSD") == fetcher.MARKET_SPOT
    assert fetcher.classify_market_type("EURUSD") == fetcher.MARKET_SPOT
    assert fetcher.classify_market_type("XAUUSD.s") == fetcher.MARKET_SPOT
    assert fetcher.classify_market_type("GCZ5") == fetcher.MARKET_FUTURES
    assert fetcher.classify_market_type("XAUz25") == fetcher.MARKET_FUTURES
    assert fetcher.classify_market_type("GOLD_FUT") == fetcher.MARKET_FUTURES


def test_market_data_root_and_inventory(tmp_path: Path):
    root = tmp_path / "data"
    (root / "spot" / "XAUUSD").mkdir(parents=True)
    (root / "futures" / "GCZ5").mkdir(parents=True)
    (root / "XAUUSD").mkdir(parents=True)  # legacy
    assert fetcher.market_data_root(str(root), "spot").endswith("spot")
    assert fetcher.market_data_root(str(root), "futures").endswith("futures")
    inv = fetcher.detect_market_inventory(str(root))
    assert "XAUUSD" in inv["spot"]
    assert "GCZ5" in inv["futures"]
    assert "XAUUSD" in inv["legacy_spot"]


def test_disconnect_shared_does_not_shutdown():
    class FakeMT5:
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    fake = FakeMT5()
    fetcher.disconnect_from_mt5(fake, owned=False, log=lambda _m: None)
    assert fake.shutdown_calls == 0
    fetcher.disconnect_from_mt5(fake, owned=True, log=lambda _m: None)
    assert fake.shutdown_calls == 1


def test_write_or_merge_monthly(tmp_path: Path):
    path = tmp_path / "m.csv"
    first = [
        {"datetime": datetime(2026, 1, 1, 1, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
    ]
    second = [
        {"datetime": datetime(2026, 1, 1, 2, 0), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
    ]
    n1 = fetcher.write_or_merge_monthly(str(path), first, merge=False)
    n2 = fetcher.write_or_merge_monthly(str(path), second, merge=True)
    assert n1 == 1
    assert n2 == 2
    assert len(fetcher.read_csv_candles(str(path))) == 2


def test_rates_to_candles_skips_bad_timestamps():
    class Row(dict):
        pass

    good = Row(time=1_704_067_200, open=1, high=2, low=0.5, close=1.5, tick_volume=3)
    bad = Row(time=-1, open=1, high=2, low=0.5, close=1.5, tick_volume=3)
    candles = fetcher._rates_to_candles([good, bad])
    assert len(candles) == 1
    assert candles[0]["close"] == 1.5
    assert candles[0]["volume"] == 3


def test_iter_fetch_subchunks_splits_m1_not_h1():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 20, 23, 59, 59, tzinfo=timezone.utc)
    m1 = list(fetcher.iter_fetch_subchunks(start, end, "1min"))
    h1 = list(fetcher.iter_fetch_subchunks(start, end, "1hour"))
    assert len(m1) >= 2
    assert len(h1) == 1
    assert h1[0][0] == start
    assert h1[0][1] == end


def test_verify_symbol_resolves_alias(monkeypatch):
    class FakeInfo:
        visible = True

    class FakeMT5:
        def symbol_info(self, sym):
            if sym == "XAUUSDm":
                return FakeInfo()
            return None

        def symbol_get(self):
            return []

        def symbol_select(self, sym, visible):
            return True

    monkeypatch.setattr(
        fetcher,
        "ensure_mt5_symbol_visible",
        lambda mt5, sym: (True, "XAUUSDm"),
    )
    ok, resolved = fetcher.verify_symbol(FakeMT5(), "XAUUSD", log=lambda _m: None)
    assert ok is True
    assert resolved == "XAUUSDm"


def test_resolve_write_root_legacy_spot(tmp_path: Path):
    root = tmp_path / "data"
    (root / "XAUUSD").mkdir(parents=True)
    wr = fetcher.resolve_write_root(str(root), "XAUUSD", "spot")
    assert wr == str(root)
    wr_f = fetcher.resolve_write_root(str(root), "GCZ5", "futures")
    assert wr_f.endswith("futures")
