"""
Append-only live trading journals under logs/live/ (next to the app / exe).

Layout (delete the whole logs/ folder anytime to clear clutter — config stays elsewhere):

  logs/
    boot.log / crash.log / fault.log     — app start + crashes (hammer_boot)
    live/
      app_live.log                       — every Live UI line
      accounts/
        <name>_<id>/
          account.log                    — that account’s activity
          slots/
            <slot>_<tf>_<id>/
              live_session.log
              live_trades.csv

Paths are created on first write. LiveRunConfig.journal_dir should point at a
slot folder from slot_journal_dir(...).
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

SESSION_LOG_NAME = "live_session.log"
TRADES_CSV_NAME = "live_trades.csv"
APP_LIVE_LOG_NAME = "app_live.log"
ACCOUNT_LOG_NAME = "account.log"

TRADE_CSV_COLUMNS: List[str] = [
    "logged_at",
    "event",
    "symbol",
    "timeframe",
    "pattern",
    "pattern_variant",
    "direction",
    "volume",
    "entry_price",
    "stop_loss",
    "target",
    "risk",
    "rr_multiple",
    "dry_run",
    "order_mode",
    "magic",
    "mt5_order_id",
    "mt5_message",
    "reason",
    "signal_bar_time",
    "entry_bar_time",
    "exit_price",
    "profit",
    "close_reason",
    "slot",
    "strategy_entry",
    "strategy_sl",
    "strategy_tp",
    # History breakdown (see live_history.signal_breakdown)
    "account",
    "preset",
    "signal_open",
    "signal_high",
    "signal_low",
    "signal_close",
    "entry_rule",
    "entry_rule_price",
    "entry_offset",
    "signal_entry",
    "pullback_pct",
    "sl_mode",
    "sl_anchor_label",
    "sl_anchor",
    "sl_buffer_mode",
    "sl_buffer_setting",
    "sl_buffer_amount",
    "params",
]


def _safe_name(text: str, fallback: str = "item") -> str:
    raw = (text or "").strip() or fallback
    cleaned = re.sub(r"[^\w.\-]+", "_", raw, flags=re.UNICODE)
    cleaned = cleaned.strip("._") or fallback
    return cleaned[:48]


def app_logs_dir(app_root: str) -> str:
    path = os.path.join(app_root, "logs")
    os.makedirs(path, exist_ok=True)
    return path


def live_logs_dir(app_root: str) -> str:
    path = os.path.join(app_logs_dir(app_root), "live")
    os.makedirs(path, exist_ok=True)
    return path


def live_journal_dir(output_root_or_app_root: str) -> str:
    """
    Root for live session files.

    Prefer app_root/logs/live. If a legacy output/ path is passed and already
    contains live data, still return logs/live under the app (caller should
    pass APP_DIR). Kept for older call sites that passed DEFAULT_OUTPUT_DIR.
    """
    # If caller passed .../output, use sibling logs/live next to app root.
    base = output_root_or_app_root
    if os.path.basename(base.rstrip(os.sep)) == "output":
        base = os.path.dirname(base)
    return live_logs_dir(base)


def app_live_log_path(app_root: str) -> str:
    return os.path.join(live_logs_dir(app_root), APP_LIVE_LOG_NAME)


def account_log_dir(
    app_root: str,
    account_id: str,
    account_name: str = "",
) -> str:
    label = _safe_name(account_name, "account")
    aid = _safe_name(account_id, "id")
    path = os.path.join(live_logs_dir(app_root), "accounts", f"{label}_{aid}")
    os.makedirs(path, exist_ok=True)
    return path


def account_log_path(
    app_root: str,
    account_id: str,
    account_name: str = "",
) -> str:
    return os.path.join(account_log_dir(app_root, account_id, account_name), ACCOUNT_LOG_NAME)


def slot_journal_dir(
    app_root: str,
    account_id: str,
    account_name: str,
    slot_id: str,
    slot_name: str = "",
    timeframe: str = "",
) -> str:
    acc_dir = account_log_dir(app_root, account_id, account_name)
    slot_label = _safe_name(slot_name or "slot", "slot")
    tf = _safe_name(timeframe or "tf", "tf")
    sid = _safe_name(slot_id, "slotid")
    path = os.path.join(acc_dir, "slots", f"{slot_label}_{tf}_{sid}")
    os.makedirs(path, exist_ok=True)
    return path


def session_log_path(journal_dir: str) -> str:
    return os.path.join(journal_dir, SESSION_LOG_NAME)


def trades_csv_path(journal_dir: str) -> str:
    return os.path.join(journal_dir, TRADES_CSV_NAME)


def append_session_log(journal_dir: str, line: str) -> None:
    if not (journal_dir or "").strip():
        return
    try:
        os.makedirs(journal_dir, exist_ok=True)
        # If journal_dir is a file path ending with .log, append there directly
        if journal_dir.lower().endswith(".log"):
            path = journal_dir
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        else:
            path = session_log_path(journal_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
            f.flush()
    except OSError:
        pass


def append_log_file(path: str, line: str) -> None:
    if not (path or "").strip():
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
            f.flush()
    except OSError:
        pass


def append_session_header(journal_dir: str, header: str) -> None:
    """Mark a new live session in the session log (MT5 connect / Start live)."""
    if not (journal_dir or "").strip():
        return
    bar = "=" * 60
    append_session_log(journal_dir, bar)
    append_session_log(journal_dir, header)
    append_session_log(journal_dir, bar)


def _upgrade_csv_header(path: str) -> None:
    """Rewrite an older live_trades.csv with the current columns (keeps every row)."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None or header == TRADE_CSV_COLUMNS:
            return
        f.seek(0)
        rows = list(csv.DictReader(f))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for old in rows:
            writer.writerow({col: old.get(col, "") or "" for col in TRADE_CSV_COLUMNS})
    os.replace(tmp, path)


def append_trade_row(journal_dir: str, row: Dict[str, Any]) -> None:
    if not (journal_dir or "").strip():
        return
    try:
        os.makedirs(journal_dir, exist_ok=True)
        path = trades_csv_path(journal_dir)
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        if not write_header:
            try:
                _upgrade_csv_header(path)
            except (OSError, csv.Error, UnicodeDecodeError):
                pass
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            out = {col: row.get(col, "") for col in TRADE_CSV_COLUMNS}
            if not out.get("logged_at"):
                out["logged_at"] = datetime.now().isoformat(timespec="seconds")
            writer.writerow(out)
            f.flush()
    except OSError:
        pass
