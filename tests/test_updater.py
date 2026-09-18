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
    assert not updater._should_skip_path("indicators/rolling_vwap.py")
    assert not updater._should_skip_path("indicators/rsi.py")


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


def test_pick_large_exe_when_frozen(monkeypatch):
    """Packaged app must prefer the real ~430 MB exe, not tiny source zips."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {"name": "Hammer-1.0.4.zip", "browser_download_url": "https://x/src", "size": 100},
        {
            "name": "HammerCandleBacktestDashboard.exe",
            "browser_download_url": "https://x/e",
            "size": 430_000_000,
        },
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert picked["name"].endswith(".exe")
    assert int(picked["size"]) >= 350_000_000


def test_reject_tiny_assets_when_frozen(monkeypatch):
    """Frozen update with only tiny zips must refuse (would brick the install)."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {"name": "Hammer-1.0.4.zip", "browser_download_url": "https://x/src", "size": 100},
        {"name": "HammerCandleBacktestDashboard-windows.zip", "browser_download_url": "https://x/win", "size": 200},
    ]
    assert updater._pick_release_asset(assets) is None


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


def test_windows_update_bat_is_bounded_and_uses_ping(tmp_path, monkeypatch):
    """Apply bat must never infinite-loop on imagename; use ping not timeout."""
    monkeypatch.setattr(updater, "app_root", lambda: str(tmp_path))
    bat = updater._write_windows_update_bat(
        pid=12345,
        exe_path=str(tmp_path / "HammerCandleBacktestDashboard.exe"),
        lines=updater._windows_replace_exe_lines(
            staged=str(tmp_path / "HammerCandleBacktestDashboard.exe.new"),
            current_exe=str(tmp_path / "HammerCandleBacktestDashboard.exe"),
            log_path=str(tmp_path / "_hammer_update.log"),
        )
        + updater._windows_relaunch_lines(
            str(tmp_path),
            str(tmp_path / "HammerCandleBacktestDashboard.exe"),
            str(tmp_path / "_hammer_update.log"),
        ),
    )
    text = open(bat, encoding="ascii", errors="replace").read()
    assert "setlocal EnableExtensions EnableDelayedExpansion" in text
    assert "WAIT_N" in text
    assert "ping -n" in text
    assert "timeout /t" not in text
    assert "Start-Process" in text
    assert "MOVE_TRIES" in text
    assert "rundll32" not in text
    assert "taskkill" not in text
    assert "sidecar" in text.lower() or "-updated.exe" in text

