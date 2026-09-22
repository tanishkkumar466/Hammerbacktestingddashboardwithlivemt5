"""Unit tests for Hammer's self-update helpers (no network)."""

from __future__ import annotations

import os
import tempfile

import update.updater as updater


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


def test_pick_stub_package_zip_over_exe_when_frozen(monkeypatch):
    """Stub package zip is preferred over a raw runtime exe."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {
            "name": "HammerCandleBacktestDashboard.exe",
            "browser_download_url": "https://x/e",
            "size": 430_000_000,
        },
        {
            "name": "Hammer-stub-package.zip",
            "browser_download_url": "https://x/z",
            "size": 435_000_000,
        },
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert picked["name"] == "Hammer-stub-package.zip"


def test_broken_client_zip_name_not_required_for_exe_bridge(monkeypatch):
    """Without Hammer-windows.zip, frozen pickers must take the large .exe bridge."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {
            "name": "HammerCandleBacktestDashboard.exe",
            "browser_download_url": "https://x/e",
            "size": 430_000_000,
        },
        {
            "name": "HammerRuntime.exe",
            "browser_download_url": "https://x/r",
            "size": 430_000_000,
        },
    ]
    picked = updater._pick_release_asset(assets)
    assert picked is not None
    assert picked["name"] == "HammerCandleBacktestDashboard.exe"


def test_reject_tiny_assets_when_frozen(monkeypatch):
    """Frozen update with only tiny zips must refuse (would brick the install)."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assets = [
        {"name": "Hammer-1.0.4.zip", "browser_download_url": "https://x/src", "size": 100},
        {"name": "HammerCandleBacktestDashboard-windows.zip", "browser_download_url": "https://x/win", "size": 200},
    ]
    assert updater._pick_release_asset(assets) is None


def test_install_root_from_runtime_path(tmp_path, monkeypatch):
    import update.paths as hp

    app = tmp_path / "app"
    app.mkdir()
    runtime = app / "HammerRuntime.exe"
    runtime.write_bytes(b"x")
    monkeypatch.setattr(hp.sys, "frozen", True, raising=False)
    monkeypatch.setattr(hp.sys, "executable", str(runtime), raising=False)
    monkeypatch.delenv(hp.INSTALL_ROOT_ENV, raising=False)
    assert hp.install_root() == str(tmp_path)
    assert hp.is_running_as_runtime()


def test_stage_runtime_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "app_root", lambda: str(tmp_path))
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "is_running_as_runtime", lambda: True)
    monkeypatch.setattr(updater, "is_stub_layout", lambda root=None: True)
    monkeypatch.setattr(updater, "_schedule_relaunch_stub", lambda root, cb, note: root)
    # Avoid writing a 350MB file in unit tests / CI
    monkeypatch.setattr(updater, "MIN_RUNTIME_BYTES", 1024)
    src = tmp_path / "new.exe"
    src.write_bytes(b"x" * 2048)
    notes = []
    root = updater._stage_runtime_pending(str(src), notes.append)
    assert root == str(tmp_path)
    pending = tmp_path / "app" / "HammerRuntime.pending.bin"
    assert pending.is_file()
    assert pending.stat().st_size >= 1024
    # Must not leave a .exe.pending name (AV bait)
    assert not (tmp_path / "app" / "HammerRuntime.exe.pending").exists()


def test_schedule_relaunch_stub_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "app_root", lambda: str(tmp_path))
    stub = tmp_path / "HammerCandleBacktestDashboard.exe"
    stub.write_bytes(b"stub")
    monkeypatch.setattr(updater, "stub_exe_path", lambda root=None: str(stub))
    notes = []
    updater._WINDOWS_UPDATE_BAT[0] = "should-clear.bat"
    updater._SILENT_STUB_RELAUNCH[0] = None
    root = updater._schedule_relaunch_stub(str(tmp_path), notes.append, "note")
    assert root == str(tmp_path)
    assert updater._WINDOWS_UPDATE_BAT[0] is None
    assert updater._SILENT_STUB_RELAUNCH[0] == str(stub)
    assert any("restarting" in n.lower() for n in notes)


def test_find_embedded_stub(tmp_path, monkeypatch):
    import update.migrate as hm

    monkeypatch.setattr(hm.sys, "frozen", True, raising=False)
    embed = tmp_path / "_hammer_embedded_stub"
    embed.mkdir()
    stub = embed / "HammerCandleBacktestDashboard.exe"
    stub.write_bytes(b"stub" * 1000)
    monkeypatch.setattr(hm.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert hm.find_embedded_stub() == str(stub)


def test_needs_migration_false_when_already_runtime(monkeypatch):
    import update.migrate as hm

    monkeypatch.setattr(hm, "is_running_as_runtime", lambda: True)
    assert hm.needs_legacy_stub_migration() is False


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


def test_safe_download_name_strips_exe():
    assert updater._safe_download_name("HammerRuntime.exe") == "HammerRuntime.bin.hammerdl"
    assert updater._safe_download_name("Hammer-stub-package.zip") == (
        "Hammer-stub-package.zip.hammerdl"
    )
    assert not updater._safe_download_name("foo.EXE").lower().endswith(".exe")


def test_looks_like_av_interference():
    assert updater._looks_like_av_interference(PermissionError("denied"))
    assert updater._looks_like_av_interference(
        updater.UpdateError("Download file disappeared while writing")
    )
    assert not updater._looks_like_av_interference(
        updater.UpdateError("GitHub returned 404")
    )


def test_download_and_install_retries_then_raises(monkeypatch, tmp_path):
    """AV-style failures should auto-retry, then surface a Try Again message."""
    monkeypatch.setattr(updater, "app_root", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "_DOWNLOAD_ATTEMPTS", 2)
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise updater.UpdateError(
            "Download file disappeared while writing "
            "(security software likely quarantined it)."
        )

    monkeypatch.setattr(updater, "_download_file", boom)
    release = updater.ReleaseInfo(
        tag="v9.9.9",
        version="9.9.9",
        notes="",
        download_url="https://example/x",
        asset_name="Hammer-stub-package.zip",
        asset_size=400_000_000,
        asset_kind="zip",
    )
    try:
        updater.download_and_install(release, lambda *_: None, lambda *_: None)
        raise AssertionError("expected UpdateError")
    except updater.UpdateError as exc:
        assert "Try Again" in str(exc)
    assert calls["n"] == 2

