"""
Append-only live trading journal under output/live/.

  live_session.log  — same lines as the UI Activity log (+ console)
  live_trades.csv   — signals, dry-run marks, and MT5 order outcomes
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

LIVE_SUBDIR = "live"
SESSION_LOG_NAME = "live_session.log"
TRADES_CSV_NAME = "live_trades.csv"

TRADE_CSV_COLUMNS: List[str] = [
    "logged_at",
    "event",
    "symbol",
    "timeframe",
    "pattern",
    "direction",
    "volume",
    "entry_price",
    "stop_loss",
    "target",
    "risk",
    "dry_run",
    "order_mode",
    "mt5_order_id",
    "mt5_message",
    "signal_bar_time",
    "entry_bar_time",
]


def live_journal_dir(output_root: str) -> str:
    path = os.path.join(output_root, LIVE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def session_log_path(journal_dir: str) -> str:
    return os.path.join(journal_dir, SESSION_LOG_NAME)


def trades_csv_path(journal_dir: str) -> str:
    return os.path.join(journal_dir, TRADES_CSV_NAME)


def append_session_log(journal_dir: str, line: str) -> None:
    if not journal_dir:
        return
    try:
        os.makedirs(journal_dir, exist_ok=True)
        with open(session_log_path(journal_dir), "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError:
        pass


def append_trade_row(journal_dir: str, row: Dict[str, Any]) -> None:
    if not journal_dir:
        return
    try:
        os.makedirs(journal_dir, exist_ok=True)
        path = trades_csv_path(journal_dir)
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            out = {col: row.get(col, "") for col in TRADE_CSV_COLUMNS}
            if not out.get("logged_at"):
                out["logged_at"] = datetime.now().isoformat(timespec="seconds")
            writer.writerow(out)
    except OSError:
        pass
