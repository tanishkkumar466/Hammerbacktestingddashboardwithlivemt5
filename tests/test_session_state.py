"""Last-session persistence: the app reopens where the user left it."""
from __future__ import annotations

import os
import sys

import polars as pl

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import session_state  # noqa: E402


def test_save_load_roundtrip(tmp_path):
    path = session_state.session_path(str(tmp_path))
    state = {
        "saved_at": "2026-09-25T10:00:00",
        "parameters": {"pattern": "Doji", "fields": {"rr_multiple": 2.5}},
        "ui": {"param_tab": "Risk", "results_tab": "Metrics", "front_docks": ["Results"]},
        "last_run": {"output_dir": "/x", "plots_dir": "/y"},
    }
    session_state.save_session(path, state)
    loaded = session_state.load_session(path)
    assert loaded["parameters"] == state["parameters"]
    assert loaded["ui"] == state["ui"]
    assert loaded["version"] == session_state.SESSION_VERSION
    assert not os.path.exists(path + ".tmp")


def test_comparable_ignores_timestamps():
    a = {"saved_at": "1", "parameters": {"saved_at": "1", "fields": {"x": 1}}}
    b = {"saved_at": "2", "parameters": {"saved_at": "2", "fields": {"x": 1}}}
    c = {"saved_at": "2", "parameters": {"saved_at": "2", "fields": {"x": 2}}}
    assert session_state.comparable(a) == session_state.comparable(b)
    assert session_state.comparable(a) != session_state.comparable(c)


def test_missing_corrupt_or_newer_file_is_ignored(tmp_path):
    path = session_state.session_path(str(tmp_path))
    assert session_state.load_session(path) is None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert session_state.load_session(path) is None
    session_state.save_session(path, {"version": session_state.SESSION_VERSION + 1})
    assert session_state.load_session(path) is None


def test_run_tables_reload_from_output_folder(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    pl.DataFrame({"exit_model": ["M1"], "net_pnl": [123.5]}).write_csv(out / "summary_overall.csv")
    pl.DataFrame({"year": [2024], "net_pnl": [123.5]}).write_csv(out / "summary_by_year.csv")
    pl.DataFrame({"outcome": ["WIN"], "pnl": [123.5]}).write_csv(out / "trade_ledger.csv")
    tables = session_state.load_run_tables(str(out))
    assert set(tables) == {"overall", "by_year", "ledger"}
    assert tables["overall"]["net_pnl"][0] == 123.5
    assert tables["ledger"]["outcome"][0] == "WIN"


def test_deleted_run_folder_gives_no_tables(tmp_path):
    assert session_state.load_run_tables(str(tmp_path / "gone")) is None
    assert session_state.load_run_tables("") is None


def test_run_config_pickle_roundtrip(tmp_path):
    from backtest import BacktestConfig

    path = session_state.run_config_path(str(tmp_path))
    cfg = BacktestConfig()
    assert session_state.save_run_config(path, cfg)
    loaded = session_state.load_run_config(path)
    assert type(loaded) is BacktestConfig
    assert loaded.starting_capital == cfg.starting_capital
    with open(path, "wb") as f:
        f.write(b"garbage")
    assert session_state.load_run_config(path) is None
