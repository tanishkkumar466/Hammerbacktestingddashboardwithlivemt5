"""logs/ layout helpers for boot + live journals."""
from __future__ import annotations

import os
from pathlib import Path

import hammer_boot
import live_journal as lj


def test_logs_dir_created(tmp_path, monkeypatch):
    monkeypatch.setattr(hammer_boot, "exe_dir", lambda: str(tmp_path))
    path = hammer_boot.logs_dir()
    assert path == str(tmp_path / "logs")
    assert os.path.isdir(path)
    hammer_boot.boot_log("hello")
    assert (tmp_path / "logs" / "boot.log").is_file()
    # CI / frozen smoke still reads hammer_boot.log next to the exe
    legacy = tmp_path / "hammer_boot.log"
    assert legacy.is_file()
    assert "hello" in legacy.read_text(encoding="utf-8")
    hammer_boot.write_crash(RuntimeError("boom"))
    assert (tmp_path / "logs" / "crash.log").is_file()
    assert (tmp_path / "hammer_crash.log").is_file()


def test_live_account_and_slot_dirs(tmp_path):
    root = str(tmp_path)
    acc = lj.account_log_dir(root, "abcd1234", "Main Demo")
    assert "logs" in acc.replace("\\", "/")
    assert "accounts" in acc
    assert os.path.isdir(acc)
    lj.append_log_file(lj.account_log_path(root, "abcd1234", "Main Demo"), "line1")
    assert os.path.isfile(lj.account_log_path(root, "abcd1234", "Main Demo"))

    slot = lj.slot_journal_dir(root, "abcd1234", "Main Demo", "s1", "Slot 1", "3m")
    assert "slots" in slot
    lj.append_session_log(slot, "slot line")
    lj.append_trade_row(slot, {"event": "SIGNAL", "symbol": "XAUUSD"})
    assert os.path.isfile(lj.session_log_path(slot))
    assert os.path.isfile(lj.trades_csv_path(slot))


def test_live_journal_dir_from_output_or_app(tmp_path):
    app = tmp_path / "Hammer"
    app.mkdir()
    out = app / "output"
    out.mkdir()
    assert Path(lj.live_journal_dir(str(out))) == app / "logs" / "live"
    assert Path(lj.live_journal_dir(str(app))) == app / "logs" / "live"
