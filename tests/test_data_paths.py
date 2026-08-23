"""CSV / data-root resolution: cwd, spot, futures, both."""
from __future__ import annotations

from pathlib import Path

import backtest


def test_resolve_data_root_prefers_app_anchor_over_cwd(tmp_path: Path, monkeypatch):
    app = tmp_path / "app"
    (app / "data" / "XAUUSD" / "1hour" / "2024").mkdir(parents=True)
    (app / "data" / "XAUUSD" / "1hour" / "2024" / "XAUUSD_1hour_2024-01.csv").write_text(
        "datetime,open,high,low,close\n"
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    root = backtest.resolve_data_root("data", anchor_dir=str(app))
    assert root == str(app / "data")
    files = backtest.find_csv_files(root, "XAUUSD", "1hour", preferred="spot")
    assert len(files) == 1


def test_spot_uses_legacy_folder(tmp_path: Path):
    data = tmp_path / "data"
    legacy = data / "XAUUSD" / "1hour" / "2024"
    legacy.mkdir(parents=True)
    (legacy / "XAUUSD_1hour_2024-01.csv").write_text("datetime,open,high,low,close\n")

    eff_root, eff_sym = backtest.resolve_symbol_data_root(
        str(data), "xauusd", preferred="spot",
    )
    assert eff_root == str(data)
    assert eff_sym == "XAUUSD"
    assert backtest.find_csv_files(str(data), "XAUUSD", "1hour", preferred="spot")


def test_spot_prefers_spot_folder_over_legacy(tmp_path: Path):
    data = tmp_path / "data"
    legacy = data / "XAUUSD" / "1hour" / "2024"
    legacy.mkdir(parents=True)
    (legacy / "XAUUSD_1hour_2024-01.csv").write_text("datetime,open,high,low,close\n")
    spot = data / "spot" / "XAUUSD" / "1hour" / "2024"
    spot.mkdir(parents=True)
    (spot / "XAUUSD_1hour_2024-02.csv").write_text("datetime,open,high,low,close\n")

    eff_root, _ = backtest.resolve_symbol_data_root(str(data), "XAUUSD", preferred="spot")
    assert Path(eff_root).name == "spot"
    files = backtest.find_csv_files(str(data), "XAUUSD", "1hour", preferred="spot")
    assert any("2024-02" in f for f in files)


def test_preferred_spot_and_futures_when_both_exist(tmp_path: Path):
    data = tmp_path / "data"
    for market, month in (("spot", "01"), ("futures", "02")):
        folder = data / market / "XAUUSD" / "1hour" / "2024"
        folder.mkdir(parents=True)
        (folder / f"XAUUSD_1hour_2024-{month}.csv").write_text(
            "datetime,open,high,low,close\n"
        )

    spot_root, _ = backtest.resolve_symbol_data_root(
        str(data), "XAUUSD", preferred="spot",
    )
    fut_root, _ = backtest.resolve_symbol_data_root(
        str(data), "XAUUSD", preferred="futures",
    )
    assert Path(spot_root).name == "spot"
    assert Path(fut_root).name == "futures"

    targets = backtest.expand_market_targets(str(data), "XAUUSD", "both")
    assert [t[0] for t in targets] == ["spot", "futures"]


def test_find_available_symbols_lists_nested(tmp_path: Path):
    data = tmp_path / "data"
    (data / "XAUUSD").mkdir(parents=True)
    (data / "spot" / "EURUSD").mkdir(parents=True)
    (data / "futures" / "GCZ5").mkdir(parents=True)

    labels = backtest.find_available_symbols(str(data))
    assert "XAUUSD" in labels
    assert "spot/EURUSD" in labels
    assert "futures/GCZ5" in labels

    labels2 = backtest.find_available_symbols(str(data / "spot"))
    assert "spot/EURUSD" in labels2
    assert "futures/GCZ5" in labels2


def test_market_data_default_is_spot():
    assert backtest.BacktestConfig().market_data == backtest.MarketDataSource.SPOT
    assert [e.value for e in backtest.MarketDataSource] == ["spot", "futures", "both"]
