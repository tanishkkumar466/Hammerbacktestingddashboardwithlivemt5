"""
Last-session persistence: reopen the app where the user left it.

Stored under output/session/ (same tree as live_desk.json, so clearing logs/
does not wipe it):

  last_session.json     parameters snapshot + open tabs + last run folders
  last_run_config.pkl   BacktestConfig of the last run (trade inspector needs it)
"""
from __future__ import annotations

import json
import os
import pickle
from typing import Any, Dict, Optional

SESSION_DIR_NAME = "session"
SESSION_FILE_NAME = "last_session.json"
RUN_CONFIG_FILE_NAME = "last_run_config.pkl"
SESSION_VERSION = 1

RUN_TABLE_FILES = {
    "overall": "summary_overall.csv",
    "by_year": "summary_by_year.csv",
    "by_month": "summary_by_month.csv",
    "by_timeframe": "summary_by_timeframe.csv",
    "by_direction": "summary_by_direction.csv",
    "by_session": "summary_by_session.csv",
    "by_market": "summary_by_market.csv",
    "ledger": "trade_ledger.csv",
}


def session_dir(output_root: str) -> str:
    return os.path.join(output_root, SESSION_DIR_NAME)


def session_path(output_root: str) -> str:
    return os.path.join(session_dir(output_root), SESSION_FILE_NAME)


def run_config_path(output_root: str) -> str:
    return os.path.join(session_dir(output_root), RUN_CONFIG_FILE_NAME)


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def comparable(state: Dict[str, Any]) -> str:
    """JSON of the state without timestamps — used to skip unchanged autosaves."""
    clean = dict(state)
    clean.pop("saved_at", None)
    params = clean.get("parameters")
    if isinstance(params, dict):
        params = dict(params)
        params.pop("saved_at", None)
        clean["parameters"] = params
    return json.dumps(clean, sort_keys=True, default=str)


def save_session(path: str, state: Dict[str, Any]) -> None:
    payload = dict(state)
    payload.setdefault("version", SESSION_VERSION)
    _atomic_write(path, json.dumps(payload, indent=2, default=str).encode("utf-8"))


def load_session(path: str) -> Optional[Dict[str, Any]]:
    """Saved state, or None when missing / unreadable / from a newer format."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version") or 0) > SESSION_VERSION:
        return None
    return data


def load_run_tables(output_dir: str) -> Optional[Dict[str, Any]]:
    """Results tables of a finished run from its CSVs (None if the run folder is gone)."""
    if not output_dir or not os.path.isfile(os.path.join(output_dir, RUN_TABLE_FILES["overall"])):
        return None
    import polars as pl

    tables: Dict[str, Any] = {}
    for key, name in RUN_TABLE_FILES.items():
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            tables[key] = pl.read_csv(path, infer_schema_length=10000, try_parse_dates=True)
        except Exception:
            continue
    return tables if "overall" in tables else None


def save_run_config(path: str, config: Any) -> bool:
    try:
        _atomic_write(path, pickle.dumps(config))
        return True
    except Exception:
        return False


def load_run_config(path: str) -> Any:
    """Last BacktestConfig, or None if missing / written by an incompatible version."""
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None
