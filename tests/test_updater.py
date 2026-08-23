"""Unit tests for Hammer's self-update helpers (no network)."""

from __future__ import annotations

import os
import tempfile

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
    assert updater._should_skip_path(".hammer_github_token")
    assert not updater._should_skip_path("dashboard.py")
    assert not updater._should_skip_path("indicators/supertrend.py")


def test_pick_zip_for_source_mode(monkeypatch):
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    assets = [
        {"name": "notes.txt"},
        {"name": "Hammer-src.zip", "browser_download_url": "https://x/z", "size": 10},
        {"name": "HammerCandleBacktestDashboard.exe", "browser_download_url": "https://x/e", "size": 20},
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert picked["name"].endswith(".zip")


def test_pick_windows_zip_when_frozen(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {"name": "Hammer-1.0.4.zip", "browser_download_url": "https://x/src", "size": 100},
        {"name": "HammerCandleBacktestDashboard-windows.zip", "browser_download_url": "https://x/win", "size": 200},
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert "windows" in picked["name"]


def test_tree_has_source_only_layout(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "main.py").write_text("#")
    (root / "dashboard.py").write_text("#")
    assert updater._tree_has_source_only_layout(str(root))


def test_load_token_from_txt_filename(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        token_path = os.path.join(tmp, "hammer_github_token.txt")
        with open(token_path, "w", encoding="utf-8") as f:
            f.write("ghp_testtoken1234567890123456789012345678")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(updater, "_token_search_roots", lambda: [tmp])
        assert updater._load_github_token() == "ghp_testtoken1234567890123456789012345678"


def test_normalize_github_token_strips_quotes_and_bom():
    assert updater._normalize_github_token('"ghp_abc"') == "ghp_abc"
    assert updater._normalize_github_token("\ufeffghp_abc") == "ghp_abc"
