"""Unit tests for Hammer's self-update helpers (no network)."""

from __future__ import annotations

import updater


def test_is_newer_basic():
    assert updater.is_newer("1.0.1", "1.0.0")
    assert not updater.is_newer("1.0.0", "1.0.0")
    assert not updater.is_newer("0.9.9", "1.0.0")
    assert updater.is_newer("v2.0.0", "1.9.9")


def test_version_tuple_strips_prefix():
    assert updater._version_tuple("v1.2.3") == (1, 2, 3)
    assert updater._version_tuple("1.2") == (1, 2)


def test_skip_user_data_paths():
    assert updater._should_skip_path("data/XAUUSD/1hour/x.csv")
    assert updater._should_skip_path("output/run/trade_ledger.csv")
    assert updater._should_skip_path("run_history.db")
    assert updater._should_skip_path("presets/client.json")
    assert not updater._should_skip_path("dashboard.py")
    assert not updater._should_skip_path("indicators/supertrend.py")


def test_pick_zip_for_source_mode():
    assets = [
        {"name": "notes.txt"},
        {"name": "Hammer-src.zip", "browser_download_url": "https://x/z", "size": 10},
        {"name": "HammerCandleBacktestDashboard.exe", "browser_download_url": "https://x/e", "size": 20},
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert picked["name"].endswith(".zip")
