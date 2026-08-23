"""
dashboard_qt.py
================
PySide6 (Qt) version of the Hammer Candle Backtest Dashboard.

WHY THIS FILE EXISTS / RELATIONSHIP TO dashboard.py
------------------------------------------------------
This is a GUI FRAMEWORK MIGRATION, not a functional change. The client
asked for a horizontal-tabbed layout and a more modern, "finished" look
than Tkinter can give -- Qt (via PySide6) is the standard choice for
that in Python. Every backend call in this file is IDENTICAL to
dashboard.py: same logic.StrategyConfig / backtest.BacktestConfig
construction, same backtest.run_backtest_and_export() call, same
plotting.generate_all_plots() / generate_yearly_summary() calls. Nothing
in logic.py, backtest.py, or plotting.py changes or needs to change.

WHY PYSIDE6 AND NOT PYQT6
----------------------------
Same Qt API either way. PySide6 is Qt's own official binding under the
LGPL license, which is the legally cleaner choice for a closed-source
app you're packaging into an exe and delivering to a client. PyQt6 has
the same functionality but a stricter commercial licensing requirement
for closed-source distribution.

LAYOUT CHANGE FROM dashboard.py (per client request: "horizontal not
vertical")
--------------------------------------------------------------------------
The old Tkinter version stacked parameter sections top-to-bottom in one
tall scrolling column (Candle Body, then Candle Wicks, then Direction
Rules, etc. -- all vertical). This version uses a QTabWidget with
HORIZONTAL TABS across the top of the parameter panel -- Body / Wicks /
Direction / Entry & Stop Loss / Timeframes / Run Settings each
get their own tab, selected left-to-right, instead of one long scroll.
The candle preview + tolerance preview keep their own dedicated panel
(center of the window), same idea as before, always visible regardless
of which parameter tab is active.

WHAT STAYS EXACTLY THE SAME
------------------------------
- Every field, every default value, every dataclass mapping
- The live candle preview (redraws on every relevant field change)
- The tolerance boundary preview (loosest/tightest valid shapes)
- Threaded backtest execution (no GUI freeze)
- Path anchoring (data/output/plots resolve next to the exe, not CWD)
- The "Overall" metrics vertical layout (metric=row, exit_model=column)
- Click-to-zoom on chart thumbnails
- Friendly errors for missing data folder / missing symbol

REQUIREMENTS
--------------
    pip install PySide6 matplotlib polars numpy pillow openpyxl
"""

import dataclasses
import hashlib
import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QDir, QPointF, QSettings, QRectF, QUrl, QThread
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPixmap, QPolygonF, QImage, QAction, QKeySequence,
    QLinearGradient, QRadialGradient, QPainterPath, QFontMetrics, QIcon, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QCheckBox, QComboBox, QPushButton, QTabWidget,
    QScrollArea, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QMessageBox, QFrame, QDialog, QSizePolicy,
    QDockWidget, QInputDialog, QPlainTextEdit, QFormLayout,
    QAbstractItemView, QDialogButtonBox, QButtonGroup, QColorDialog, QRadioButton,
    QFileDialog,
)

import logic
import doji_logic
import hammer_context_logic
import backtest
import plotting
import sessions
import polars as pl
from indicators.config import IndicatorCombineMode, IndicatorStackConfig, SuperTrendConfig, VWAPConfig
from indicators.registry import INDICATOR_REGISTRY, INDICATOR_COMBINE_HELP, INDICATOR_FILTER_LOGIC_FILE
import live as live_trading
import telegram_notify
from telegram_workers import TelegramTestWorker

try:
    from shiboken6 import isValid as _qt_widget_valid
except ImportError:
    def _qt_widget_valid(_w) -> bool:  # type: ignore[misc]
        return True
import fetch as data_fetcher
from broker import BrokerCredentials, MT5Broker
from live_journal import append_session_log, live_journal_dir, session_log_path


# ============================================================================
# PATH ANCHORING -- identical logic to dashboard.py, required for
# PyInstaller packaging so data/output/plots resolve next to the exe
# regardless of the launch working directory.
# ============================================================================
def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_asset_path(filename: str) -> str:
    """
    Resolves a bundled, fixed application asset (like the app icon) --
    NOT the same as get_app_dir(). get_app_dir() points at user-editable
    folders that live beside the exe (data/output/plots, meant to be
    added or changed by whoever runs the app). This instead resolves
    files that were baked into the build itself: in a normal Python run
    that's just next to this script; in a PyInstaller --onefile exe,
    bundled data is unpacked to a temp folder exposed as sys._MEIPASS.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


APP_DIR = get_app_dir()
DOCK_LAYOUT_VERSION = 7  # client layout: Preview | Live, bottom tabs for Results/Params
DEFAULT_DATA_DIR = os.path.join(APP_DIR, "data")
DEFAULT_OUTPUT_DIR = os.path.join(APP_DIR, "output")
PRESETS_DIR = os.path.join(APP_DIR, "presets")
DEFAULT_PLOTS_DIR = os.path.join(APP_DIR, "plots")
RUN_DATABASE_PATH = os.path.join(APP_DIR, "run_history.db")
LIVE_JOURNAL_DIR = live_journal_dir(DEFAULT_OUTPUT_DIR)

LIVE_ORDER_MODES = (
    ("market", "Market — instant at ask/bid"),
    ("limit_entry", "Limit — at strategy entry price"),
    ("limit_offset", "Limit — entry price ± offset (points)"),
)


# ============================================================================
# RUN HISTORY DATABASE
# ----------------------------------------------------------------------------
# A local SQLite file (run_history.db, sitting next to the exe like
# data/output/plots) recording every backtest that's actually been run.
# Modeled on the same idea as the client's Strategy Configurator
# workbook: one row per unique parameter combination (a "param set"),
# linked to the metrics that combination produced.
#
# DEDUPLICATION: every time a backtest finishes, its full configuration
# (everything except the always-different output folder / run name) is
# hashed. If that exact hash has already been saved, nothing new is
# written -- same parameters + same data means nothing changed, so
# there's nothing new to record. A different hash (any parameter
# changed) always gets its own row. This is a straight SQLite table,
# not a change to logic.py/backtest.py -- the backend's output is only
# ever read here, never altered.
# ============================================================================
def _to_jsonable(obj):
    """Recursively converts config objects (dataclasses, enums, dicts,
    nested objects) into plain JSON-safe Python values, without needing
    to know their exact types in advance."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _config_to_dict(backtest_config) -> dict:
    d = _to_jsonable(backtest_config)
    if isinstance(d, dict):
        # Always different between runs, and not actually part of "the
        # parameters" -- excluded so identical settings still hash the same.
        d.pop("output_root", None)
        d.pop("run_name", None)
    return d if isinstance(d, dict) else {}


def _compute_param_hash(backtest_config) -> str:
    canonical = json.dumps(_config_to_dict(backtest_config), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rows_from_metrics_table(df):
    """Best-effort conversion of a metrics table (polars or pandas
    DataFrame) to a list of plain dicts, without hard-depending on
    either library."""
    if df is None:
        return []
    for method_name in ("to_dicts", "to_dict"):
        method = getattr(df, method_name, None)
        if method is None:
            continue
        try:
            result = method() if method_name == "to_dicts" else method("records")
            if isinstance(result, list):
                return result
        except Exception:
            continue
    return []


def _migrate_table_columns(conn, table: str, column_defs: dict) -> None:
    """Add missing columns on existing SQLite databases (safe re-run)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, sql_type in column_defs.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")


def _metrics_verdict(profit_factor, net_pnl) -> str:
    if profit_factor is not None and net_pnl is not None:
        return "Promising" if (profit_factor > 1.2 and net_pnl > 0) else "Rejected"
    return "Not yet tested"


def _metrics_from_row(row_dict: dict) -> dict:
    """Map backtest summary row (snake_case) to DB column names."""
    if not row_dict:
        return {}
    wins = row_dict.get("wins")
    losses = row_dict.get("losses")
    total_trades = row_dict.get("total_trades")
    if total_trades is None and (wins is not None or losses is not None):
        total_trades = (wins or 0) + (losses or 0)
    net_pnl = row_dict.get("net_pnl")
    max_dd_usd = row_dict.get("max_drawdown_usd")
    profit_factor = row_dict.get("profit_factor")
    recovery_factor = (net_pnl / max_dd_usd) if (net_pnl is not None and max_dd_usd) else None
    return {
        "TotalSignals": row_dict.get("total_signals"),
        "TotalTrades": total_trades,
        "SkippedOverlap": row_dict.get("skipped_overlap"),
        "StillOpen": row_dict.get("still_open"),
        "Wins": wins,
        "Losses": losses,
        "WinRatePercent": row_dict.get("win_rate_pct"),
        "GrossProfit": row_dict.get("gross_profit"),
        "GrossLoss": row_dict.get("gross_loss"),
        "NetProfit": net_pnl,
        "AvgWin": row_dict.get("avg_win_usd"),
        "AvgLoss": row_dict.get("avg_loss_usd"),
        "AvgTradeUsd": row_dict.get("avg_trade_usd"),
        "LargestWinUsd": row_dict.get("largest_win_usd"),
        "LargestLossUsd": row_dict.get("largest_loss_usd"),
        "ProfitFactor": profit_factor,
        "ExpectedPayoff": row_dict.get("expectancy_usd"),
        "PayoffRatio": row_dict.get("payoff_ratio"),
        "AvgRRAchieved": row_dict.get("avg_rr_achieved"),
        "AvgBarsHeldWin": row_dict.get("avg_bars_held_win"),
        "AvgBarsHeldLoss": row_dict.get("avg_bars_held_loss"),
        "MaxDrawdownUsd": max_dd_usd,
        "MaxDrawdownPercent": row_dict.get("max_drawdown_pct"),
        "MaxConsecutiveWins": row_dict.get("max_consecutive_wins"),
        "MaxConsecutiveLosses": row_dict.get("max_consecutive_losses"),
        "SharpeRatio": row_dict.get("sharpe_ratio"),
        "SortinoRatio": row_dict.get("sortino_ratio"),
        "StartingCapital": row_dict.get("starting_capital"),
        "EndingCapital": row_dict.get("ending_capital"),
        "TotalReturnPct": row_dict.get("total_return_pct"),
        "RecoveryFactor": recovery_factor,
        "Verdict": _metrics_verdict(profit_factor, net_pnl),
    }


BACKTEST_RESULT_METRIC_COLUMNS = [
    "TotalSignals", "TotalTrades", "SkippedOverlap", "StillOpen", "Wins", "Losses",
    "WinRatePercent", "GrossProfit", "GrossLoss", "NetProfit", "AvgWin", "AvgLoss",
    "AvgTradeUsd", "LargestWinUsd", "LargestLossUsd", "ProfitFactor", "ExpectedPayoff",
    "PayoffRatio", "AvgRRAchieved", "AvgBarsHeldWin", "AvgBarsHeldLoss",
    "MaxDrawdownUsd", "MaxDrawdownPercent", "MaxConsecutiveWins", "MaxConsecutiveLosses",
    "SharpeRatio", "SortinoRatio", "StartingCapital", "EndingCapital", "TotalReturnPct",
    "RecoveryFactor", "Verdict",
]

METRICS_BREAKDOWN_SOURCES = (
    ("by_session", "session"),
    ("by_timeframe", "timeframe"),
    ("by_direction", "direction"),
    ("by_year", "year"),
    ("by_month", "month"),
)


class RunDatabase:
    """
    Mirrors the client's Strategy Configurator workbook schema exactly:
    same table names, same column names, in the same order. Populated
    with real values wherever this app has a genuine equivalent to an
    MT4/MT5 Expert Advisor field; left NULL wherever it doesn't (e.g.
    MagicNumber, GridStepPoints, IndicatorFast_Period -- none of that
    exists in a candlestick-pattern backtester).

    Three things are appended AFTER the original columns, because the
    original schema has no column at all for them and literally cannot
    represent this app's data without them:
      - Strategies_Master: ParamHash (dedup key) + Hammer shape parameters
        + PatternType, SlMode, lookback, overlap, date range, etc.
      - Backtest_Results: ExitModel + full metrics (gross PnL, streaks,
        Sharpe/Sortino, return %, skipped overlap, …).
      - Backtest_Metrics_Breakdown: per-session / timeframe / direction /
        year / month rows (same metrics, for deeper analysis).
    Nothing in the original column set is renamed, reordered, or removed.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS Strategies_Master (
                    StrategyID INTEGER PRIMARY KEY AUTOINCREMENT,
                    StrategyName TEXT,
                    StrategyType TEXT,
                    Symbol TEXT,
                    Timeframe TEXT,
                    TradingStartHour TEXT,
                    TradingStopHour TEXT,
                    TradeDaysAllowed TEXT,
                    NewsFilterMinutes REAL,
                    MaxSpreadPoints REAL,
                    EntrySignalType TEXT,
                    IndicatorFast_Period REAL,
                    IndicatorSlow_Period REAL,
                    IndicatorAppliedPrice TEXT,
                    EntryTriggerCondition TEXT,
                    ConfirmationFilter TEXT,
                    TradeDirection TEXT,
                    StopLossMode TEXT,
                    StopLossValue REAL,
                    TakeProfitMode TEXT,
                    TakeProfitValue REAL,
                    RiskRewardRatio REAL,
                    TrailingStopEnabled TEXT,
                    TrailingStopDistance REAL,
                    TrailingStopStep REAL,
                    BreakEvenTriggerPips REAL,
                    PositionSizingMethod TEXT,
                    RiskPercentPerTrade REAL,
                    FixedLotSize REAL,
                    MaxOpenPositions REAL,
                    MaxDailyLossPercent REAL,
                    MagicNumber REAL,
                    MaxSlippagePoints REAL,
                    OrderType TEXT,
                    GridStepPoints REAL,
                    GridMaxLevels REAL,
                    Status TEXT,
                    Notes TEXT,
                    ParamHash TEXT UNIQUE NOT NULL,
                    BodyPct REAL,
                    DominantWickPct REAL,
                    SmallWickPct REAL,
                    WickSide TEXT,
                    BodyTolerance REAL,
                    DominantWickTolerance REAL,
                    SmallWickTolerance REAL,
                    TimeframesTested TEXT,
                    CreatedAt TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS Lookup_Lists (
                    StrategyType TEXT, Timeframe TEXT, EntrySignalType TEXT,
                    IndicatorAppliedPrice TEXT, TradeDirection TEXT, StopLossMode TEXT,
                    TakeProfitMode TEXT, PositionSizingMethod TEXT, OrderType TEXT,
                    Status TEXT, TrailingStopEnabled TEXT
                );

                CREATE TABLE IF NOT EXISTS Tester_Config (
                    StrategyID INTEGER NOT NULL REFERENCES Strategies_Master(StrategyID),
                    TestSymbol TEXT,
                    TestTimeframe TEXT,
                    DateFrom TEXT,
                    DateTo TEXT,
                    ModelingMethod TEXT,
                    SpreadSetting TEXT,
                    CustomSpreadPoints REAL,
                    InitialDeposit REAL,
                    AccountCurrency TEXT,
                    Leverage TEXT,
                    OptimizationEnabled TEXT,
                    VisualMode TEXT,
                    ExecutionDelayMs REAL
                );

                CREATE TABLE IF NOT EXISTS Optimization_Ranges (
                    StrategyID INTEGER NOT NULL REFERENCES Strategies_Master(StrategyID),
                    ParameterName TEXT,
                    MinValue REAL,
                    MaxValue REAL,
                    StepValue REAL,
                    OptimizationCriterion TEXT
                );

                CREATE TABLE IF NOT EXISTS Backtest_Results (
                    StrategyID INTEGER NOT NULL REFERENCES Strategies_Master(StrategyID),
                    TestDate TEXT,
                    NetProfit REAL,
                    ProfitFactor REAL,
                    ExpectedPayoff REAL,
                    MaxDrawdownPercent REAL,
                    WinRatePercent REAL,
                    TotalTrades REAL,
                    AvgWin REAL,
                    AvgLoss REAL,
                    SharpeRatio REAL,
                    RecoveryFactor REAL,
                    Verdict TEXT,
                    ExitModel TEXT,
                    OutputDir TEXT,
                    PlotsDir TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_param_hash ON Strategies_Master(ParamHash);
                CREATE INDEX IF NOT EXISTS idx_tester_strategy ON Tester_Config(StrategyID);
                CREATE INDEX IF NOT EXISTS idx_results_strategy ON Backtest_Results(StrategyID);

                CREATE TABLE IF NOT EXISTS Backtest_Metrics_Breakdown (
                    BreakdownID INTEGER PRIMARY KEY AUTOINCREMENT,
                    StrategyID INTEGER NOT NULL REFERENCES Strategies_Master(StrategyID),
                    TestDate TEXT NOT NULL,
                    ExitModel TEXT NOT NULL,
                    BreakdownType TEXT NOT NULL,
                    GroupName TEXT NOT NULL,
                    TotalSignals REAL,
                    TotalTrades REAL,
                    SkippedOverlap REAL,
                    StillOpen REAL,
                    Wins REAL,
                    Losses REAL,
                    WinRatePercent REAL,
                    GrossProfit REAL,
                    GrossLoss REAL,
                    NetProfit REAL,
                    AvgWin REAL,
                    AvgLoss REAL,
                    AvgTradeUsd REAL,
                    LargestWinUsd REAL,
                    LargestLossUsd REAL,
                    ProfitFactor REAL,
                    ExpectedPayoff REAL,
                    PayoffRatio REAL,
                    AvgRRAchieved REAL,
                    AvgBarsHeldWin REAL,
                    AvgBarsHeldLoss REAL,
                    MaxDrawdownUsd REAL,
                    MaxDrawdownPercent REAL,
                    MaxConsecutiveWins REAL,
                    MaxConsecutiveLosses REAL,
                    SharpeRatio REAL,
                    SortinoRatio REAL,
                    StartingCapital REAL,
                    EndingCapital REAL,
                    TotalReturnPct REAL,
                    RecoveryFactor REAL,
                    Verdict TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_breakdown_strategy
                    ON Backtest_Metrics_Breakdown(StrategyID);
                CREATE INDEX IF NOT EXISTS idx_breakdown_type
                    ON Backtest_Metrics_Breakdown(StrategyID, BreakdownType, ExitModel);
            """)
            _migrate_table_columns(conn, "Strategies_Master", {
                "PatternType": "TEXT",
                "SlMode": "TEXT",
                "SlFixedDistance": "REAL",
                "LookbackCandles": "REAL",
                "AllowOverlappingTrades": "TEXT",
                "FixedRiskUsd": "REAL",
                "DateRangeStart": "TEXT",
                "DateRangeEnd": "TEXT",
                "MarketData": "TEXT",
            })
            _migrate_table_columns(conn, "Backtest_Results", {
                col: "REAL" if col not in ("Verdict",) else "TEXT"
                for col in BACKTEST_RESULT_METRIC_COLUMNS
                if col not in (
                    "NetProfit", "ProfitFactor", "ExpectedPayoff", "MaxDrawdownPercent",
                    "WinRatePercent", "TotalTrades", "AvgWin", "AvgLoss", "SharpeRatio",
                    "RecoveryFactor", "Verdict",
                )
            })
            self._seed_lookup_lists(conn)

    def _seed_lookup_lists(self, conn):
        """Populates Lookup_Lists with the client's own reference values,
        exactly as in their workbook -- only once, if the table is empty."""
        existing = conn.execute("SELECT COUNT(*) FROM Lookup_Lists").fetchone()[0]
        if existing:
            return
        rows = [
            ("Trend Following", "M1", "MA Crossover", "Close", "Buy Only", "Fixed Pips", "Fixed Pips", "Fixed Lot", "Market", "Draft", "Yes"),
            ("Breakout", "M5", "RSI", "Open", "Sell Only", "ATR-based", "Risk-Reward", "Risk % of Equity", "Limit", "Testing", "No"),
            ("Mean Reversion", "M15", "Breakout", "High", "Both", "Fibonacci-based", "ATR-based", "Risk % of Balance", "Stop", "Active", None),
            ("Range/Grid/Scalping", "M30", "Range Breakout", "Low", None, "Structure-based (Swing High/Low)", "Structure-based", "Martingale", "Stop-Limit", "Paused", None),
            ("News Trading", "H1", "Price Action", "Median", None, None, None, "Anti-Martingale", None, "Retired", None),
            ("Arbitrage", "H4", "Bollinger Bands", "Typical", None, None, None, None, None, None, None),
            (None, "D1", "Custom Indicator", "Weighted", None, None, None, None, None, None, None),
            (None, "W1", "MACD", "Bid", None, None, None, None, None, None, None),
            (None, "MN1", None, "Ask", None, None, None, None, None, None, None),
        ]
        conn.executemany(
            "INSERT INTO Lookup_Lists VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )

    def save_run(self, backtest_config, tables: dict, output_dir: str, plots_dir: str):
        """
        Saves this run unless an identical parameter set (ParamHash) has
        already been saved. Returns (strategy_id, was_duplicate, existing_run_info).
        existing_run_info is only set when was_duplicate is True.
        """
        param_hash = _compute_param_hash(backtest_config)
        config_dict = _config_to_dict(backtest_config)
        now = datetime.now().isoformat(timespec="seconds")

        strategy = config_dict.get("strategy_config") or {}
        pattern_type = config_dict.get("pattern_type", "hammer")
        hammer = strategy.get("hammer_ratios") or {}
        doji = strategy.get("doji_ratios") or {}
        timeframes = config_dict.get("timeframes_to_test") or []
        primary_tf = timeframes[0] if timeframes else None
        timeframe_settings = strategy.get("timeframe_settings") or {}
        primary_tf_settings = timeframe_settings.get(primary_tf) or {}

        symbol = config_dict.get("symbol")
        buffer_mode = str(strategy.get("buffer_mode") or "")
        stop_loss_value = (strategy.get("sl_buffer_pct") if "PERCENT" in buffer_mode.upper()
                            else strategy.get("sl_buffer_flat"))

        if pattern_type == "doji":
            pattern_label = "Doji"
            entry_signal = "Doji Candle"
            trade_direction = (
                f"Mode={strategy.get('doji_direction_mode')}, "
                f"Fallback={strategy.get('fallback_direction')}"
            )
            body_pct = doji.get("max_body_pct")
            dominant_wick_pct = doji.get("min_upper_wick_pct")
            small_wick_pct = doji.get("min_lower_wick_pct")
            wick_side = strategy.get("doji_style")
            body_tol = doji.get("max_body_tol")
            dominant_wick_tol = doji.get("min_wick_tol")
            small_wick_tol = doji.get("min_wick_tol")
        elif pattern_type in ("hammer_with_candles", "hammer_context"):
            pattern_label = "Hammer with candles"
            entry_signal = "Hammer with candles"
            buy = strategy.get("buy_hammer_ratios") or {}
            sell = strategy.get("sell_hammer_ratios") or {}
            try:
                trade_direction = hammer_context_logic.describe_hammer_context_rules(
                    backtest_config.strategy_config
                )
            except Exception:
                trade_direction = (
                    f"BUY lookback={strategy.get('lookback_candles', 5)}; "
                    f"wick buy={strategy.get('buy_require_wick', True)} "
                    f"sell={strategy.get('sell_require_wick', True)}"
                )
            body_pct = buy.get("body_pct")
            dominant_wick_pct = buy.get("dominant_wick_pct")
            small_wick_pct = buy.get("small_wick_pct")
            buy_wick = bool(strategy.get("buy_require_wick", True))
            sell_wick = bool(strategy.get("sell_require_wick", True))
            wick_side = (
                f"BUY wick={'on' if buy_wick else 'off'} / "
                f"SELL wick={'on' if sell_wick else 'off'}"
            )
            body_tol = buy.get("body_tol")
            dominant_wick_tol = buy.get("dominant_wick_tol")
            small_wick_tol = buy.get("small_wick_tol")
            if sell:
                trade_direction += (
                    f" | BUY body {buy.get('body_pct')}% / SELL body {sell.get('body_pct')}%"
                )
        else:
            pattern_label = "Hammer"
            entry_signal = "Hammer Candle"
            trade_direction = logic.describe_hammer_direction_matrix(
                logic.StrategyConfig(
                    classic_green=logic.coerce_trade_action(strategy.get("classic_green", "BUY")),
                    classic_red=logic.coerce_trade_action(strategy.get("classic_red", "SELL")),
                    inverted_green=logic.coerce_trade_action(strategy.get("inverted_green", "BUY")),
                    inverted_red=logic.coerce_trade_action(strategy.get("inverted_red", "SELL")),
                )
            ) if strategy.get("classic_green") is not None else (
                f"Green={strategy.get('green_direction')}, Red={strategy.get('red_direction')}"
            )
            body_pct = hammer.get("body_pct")
            dominant_wick_pct = hammer.get("dominant_wick_pct")
            small_wick_pct = hammer.get("small_wick_pct")
            wick_side = strategy.get("hammer_wick_side")
            body_tol = hammer.get("body_tol")
            dominant_wick_tol = hammer.get("dominant_wick_tol")
            small_wick_tol = hammer.get("small_wick_tol")

        sl_mode = strategy.get("sl_mode") or strategy.get("inverted_sl_mode")
        sl_fixed = strategy.get("sl_fixed_distance") or strategy.get("inverted_sl_fixed_distance")
        lookback = strategy.get("lookback_candles")
        allow_overlap = "Yes" if config_dict.get("allow_overlapping_trades") else "No"
        fixed_risk = config_dict.get("fixed_risk_usd")
        date_start = str(config_dict.get("start_date") or "")
        date_end = str(config_dict.get("end_date") or "")
        market_data = config_dict.get("market_data")
        if hasattr(market_data, "value"):
            market_data = market_data.value
        market_data = str(market_data or "spot")
        stop_loss_mode_col = sl_mode or strategy.get("buffer_mode")

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT StrategyID FROM Strategies_Master WHERE ParamHash = ?", (param_hash,))
            existing = cur.fetchone()

            if existing is not None:
                strategy_id = existing[0]
                cur.execute(
                    "SELECT TestDate FROM Backtest_Results WHERE StrategyID = ? ORDER BY rowid DESC LIMIT 1",
                    (strategy_id,),
                )
                existing_result = cur.fetchone()
                if existing_result is not None:
                    return None, True, {"run_id": strategy_id, "run_timestamp": existing_result[0]}
            else:
                cur.execute(
                    """INSERT INTO Strategies_Master (
                        StrategyName, StrategyType, Symbol, Timeframe, TradingStartHour, TradingStopHour,
                        TradeDaysAllowed, NewsFilterMinutes, MaxSpreadPoints, EntrySignalType,
                        IndicatorFast_Period, IndicatorSlow_Period, IndicatorAppliedPrice,
                        EntryTriggerCondition, ConfirmationFilter, TradeDirection, StopLossMode,
                        StopLossValue, TakeProfitMode, TakeProfitValue, RiskRewardRatio,
                        TrailingStopEnabled, TrailingStopDistance, TrailingStopStep, BreakEvenTriggerPips,
                        PositionSizingMethod, RiskPercentPerTrade, FixedLotSize, MaxOpenPositions,
                        MaxDailyLossPercent, MagicNumber, MaxSlippagePoints, OrderType, GridStepPoints,
                        GridMaxLevels, Status, Notes, ParamHash, BodyPct, DominantWickPct, SmallWickPct,
                        WickSide, BodyTolerance, DominantWickTolerance, SmallWickTolerance,
                        TimeframesTested, PatternType, SlMode, SlFixedDistance, LookbackCandles,
                        AllowOverlappingTrades, FixedRiskUsd, DateRangeStart, DateRangeEnd,
                        MarketData, CreatedAt
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{pattern_label}_{symbol}_{primary_tf}", "Candlestick Pattern", symbol, primary_tf,
                        None, None, None, None, None, entry_signal,
                        None, None, None,
                        strategy.get("entry_rule"), None,
                        trade_direction,
                        stop_loss_mode_col, stop_loss_value, "Risk-Reward", None,
                        primary_tf_settings.get("rr_multiple"),
                        "No", None, None, None,
                        config_dict.get("position_sizing_mode"), config_dict.get("risk_pct_of_equity"),
                        config_dict.get("position_size"),
                        1 if not config_dict.get("allow_overlapping_trades") else None,
                        None, None, None, "Market", None, None,
                        "Tested", None, param_hash,
                        body_pct, dominant_wick_pct, small_wick_pct,
                        wick_side, body_tol,
                        dominant_wick_tol, small_wick_tol,
                        ",".join(timeframes), pattern_type, sl_mode, sl_fixed, lookback,
                        allow_overlap, fixed_risk, date_start, date_end, market_data, now,
                    ),
                )
                strategy_id = cur.lastrowid

                cur.execute(
                    """INSERT INTO Tester_Config (
                        StrategyID, TestSymbol, TestTimeframe, DateFrom, DateTo, ModelingMethod,
                        SpreadSetting, CustomSpreadPoints, InitialDeposit, AccountCurrency, Leverage,
                        OptimizationEnabled, VisualMode, ExecutionDelayMs
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        strategy_id, symbol, ",".join(timeframes),
                        str(config_dict.get("start_date") or ""), str(config_dict.get("end_date") or ""),
                        None, None, None,
                        config_dict.get("starting_capital"), None, None,
                        "No", "No", None,
                    ),
                )

            # ---- Backtest_Results: one row per exit model, since this
            # engine (unlike a single-result MT4 strategy tester) always
            # produces Best Case / Candle Bias / Worst Case together.
            overall_rows = _rows_from_metrics_table((tables or {}).get("overall"))
            for row_dict in overall_rows:
                self._insert_backtest_result(
                    cur, strategy_id, now, row_dict.get("exit_model", ""),
                    output_dir, plots_dir, row_dict,
                )

            self._insert_metrics_breakdown(cur, strategy_id, now, tables)

            conn.commit()
            return strategy_id, False, None

    def _insert_backtest_result(self, cur, strategy_id, test_date, exit_model,
                                output_dir, plots_dir, row_dict):
        metrics = _metrics_from_row(row_dict)
        cols = ["StrategyID", "TestDate", "ExitModel", "OutputDir", "PlotsDir"] + BACKTEST_RESULT_METRIC_COLUMNS
        values = [strategy_id, test_date, exit_model, output_dir, plots_dir]
        values += [metrics.get(c) for c in BACKTEST_RESULT_METRIC_COLUMNS]
        cur.execute(
            f"INSERT INTO Backtest_Results ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )

    def _insert_metrics_breakdown(self, cur, strategy_id, test_date, tables):
        for table_key, breakdown_type in METRICS_BREAKDOWN_SOURCES:
            for row_dict in _rows_from_metrics_table((tables or {}).get(table_key)):
                metrics = _metrics_from_row(row_dict)
                cols = (
                    ["StrategyID", "TestDate", "ExitModel", "BreakdownType", "GroupName"]
                    + BACKTEST_RESULT_METRIC_COLUMNS
                )
                values = [
                    strategy_id,
                    test_date,
                    row_dict.get("exit_model", ""),
                    breakdown_type,
                    row_dict.get("group", ""),
                ]
                values += [metrics.get(c) for c in BACKTEST_RESULT_METRIC_COLUMNS]
                cur.execute(
                    f"INSERT INTO Backtest_Metrics_Breakdown ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    values,
                )

    def fetch_run_history(self):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT s.StrategyID, s.CreatedAt, s.PatternType, s.Symbol, s.TimeframesTested,
                       s.MarketData,
                       s.BodyPct, s.DominantWickPct, s.PositionSizingMethod, s.SlMode,
                       s.LookbackCandles, s.AllowOverlappingTrades, s.FixedRiskUsd,
                       t.InitialDeposit,
                       wc.NetProfit AS NetProfitWC,
                       wc.WinRatePercent AS WinRateWC,
                       wc.ProfitFactor AS ProfitFactorWC,
                       wc.TotalTrades AS TotalTradesWC,
                       wc.MaxDrawdownPercent AS MaxDrawdownWC,
                       wc.TotalReturnPct AS TotalReturnWC,
                       wc.OutputDir AS OutputDir,
                       (SELECT GroupName FROM Backtest_Metrics_Breakdown b
                        WHERE b.StrategyID = s.StrategyID AND b.BreakdownType = 'session'
                              AND b.ExitModel = 'worst_case' AND b.ProfitFactor IS NOT NULL
                        ORDER BY b.ProfitFactor DESC, b.NetProfit DESC LIMIT 1) AS BestSessionWC
                FROM Strategies_Master s
                LEFT JOIN Tester_Config t ON t.StrategyID = s.StrategyID
                LEFT JOIN Backtest_Results wc ON wc.rowid = (
                    SELECT rowid FROM Backtest_Results
                    WHERE StrategyID = s.StrategyID AND ExitModel = 'worst_case'
                    ORDER BY TestDate DESC
                    LIMIT 1
                )
                ORDER BY s.StrategyID DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    def fetch_backtest_results(self, strategy_id: int):
        """Per exit-model metrics row(s) for run comparison."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT ExitModel, NetProfit, ProfitFactor, WinRatePercent,
                       MaxDrawdownPercent, TotalTrades, AvgWin, AvgLoss, ExpectedPayoff,
                       GrossProfit, GrossLoss, SkippedOverlap, StillOpen, TotalSignals,
                       PayoffRatio, SortinoRatio, SharpeRatio, TotalReturnPct, MaxDrawdownUsd,
                       RecoveryFactor, Verdict
                FROM Backtest_Results
                WHERE StrategyID = ?
                ORDER BY ExitModel
                """,
                (strategy_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def fetch_metrics_breakdown(self, strategy_id: int, breakdown_type: str = "session",
                                exit_model: str = "worst_case"):
        """Session / timeframe / direction / year / month rows for one run."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT BreakdownType, GroupName, ExitModel, TotalTrades, WinRatePercent,
                       NetProfit, ProfitFactor, MaxDrawdownPercent, ExpectedPayoff, Verdict
                FROM Backtest_Metrics_Breakdown
                WHERE StrategyID = ? AND BreakdownType = ? AND ExitModel = ?
                ORDER BY GroupName
                """,
                (strategy_id, breakdown_type, exit_model),
            )
            return [dict(r) for r in cur.fetchall()]

    def export_to_excel(self, path: str):
        import openpyxl
        from openpyxl.styles import Font, PatternFill

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            sheets_data = {}
            for table in ("Strategies_Master", "Lookup_Lists", "Tester_Config",
                          "Optimization_Ranges", "Backtest_Results", "Backtest_Metrics_Breakdown"):
                sheets_data[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]

        wb = openpyxl.Workbook()
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="34A853")

        readme = wb.active
        readme.title = "README"
        readme["A1"] = "Hammer Candle Backtest -- Run History"
        readme["A1"].font = Font(bold=True, size=14)
        readme["A3"] = "Schema matches Strategy_Configurator_FINAL.xlsx"
        readme["A4"], readme["B4"] = "Strategies_Master", "One row per unique parameter set actually backtested. Columns not applicable to this engine (MagicNumber, GridStepPoints, indicator periods, etc.) are blank."
        readme["A5"], readme["B5"] = "Backtest_Results", "One row per (StrategyID, ExitModel) with full metrics (PnL, PF, drawdown, Sharpe, sortino, streaks, etc.)."
        readme["A6"], readme["B6"] = "Backtest_Metrics_Breakdown", "Granular rows per session, timeframe, direction, year, and month — same metrics as Backtest_Results for deeper analysis."
        readme["A7"], readme["B7"] = "Dedup rule", "Identical parameters (same config, same data) are only ever saved once -- re-running them does not create a duplicate row."
        readme["A8"], readme["B8"] = "Optimization_Ranges", "Present for schema compatibility; this app doesn't run parameter sweeps, so it's always empty."
        readme.column_dimensions["A"].width = 20
        readme.column_dimensions["B"].width = 90

        def write_sheet(name, rows):
            sheet = wb.create_sheet(name)
            if not rows:
                sheet["A1"] = "(no data)"
                return sheet
            headers = list(rows[0].keys())
            sheet.append(headers)
            for cell in sheet[1]:
                cell.font = header_font
                cell.fill = header_fill
            for r in rows:
                sheet.append([r.get(h) for h in headers])
            for col_cells in sheet.columns:
                width = max((len(str(c.value)) for c in col_cells if c.value is not None), default=8)
                sheet.column_dimensions[col_cells[0].column_letter].width = min(40, width + 2)
            return sheet

        for table_name, rows in sheets_data.items():
            write_sheet(table_name, rows)

        wb.save(path)


# ============================================================================
# PATTERN REGISTRY -- identical to dashboard.py
# ============================================================================
PATTERN_REGISTRY = {
    "Hammer": {
        "description": "Classic hammer / inverted hammer candle (logic.py: check_hammer)",
        "ratio_config_class": logic.HammerRatioConfig,
        "pattern_type": "hammer",
    },
    "Doji": {
        "description": "Doji candle -- very small body, wick rules vary by style (doji_logic.py: check_doji)",
        "ratio_config_class": doji_logic.DojiRatioConfig,
        "pattern_type": "doji",
    },
    "Hammer with candles": {
        "description": (
            "Classic green hammer + N prior closes not below hammer low (BUY); "
            "inverted red hammer + N prior closes not above hammer high (SELL). "
            "Separate Body/Wicks for BUY vs SELL. Wick off: body ≤ Body % only — "
            "signal color ignored; previous candle red→BUY, green→SELL."
        ),
        "ratio_config_class": logic.HammerRatioConfig,
        "pattern_type": "hammer_with_candles",
    },
}


# ============================================================================
# FIELD METADATA -- identical to dashboard.py (same fields, same order)
# ============================================================================
FIELD_TYPE_TEXT = "text"
FIELD_TYPE_CHECK = "check"
FIELD_TYPE_DROPDOWN = "dropdown"


def enum_choices(enum_cls) -> List[str]:
    return [e.value for e in enum_cls]


ENTRY_RULE_LABELS_WICK_ON = {
    "NEXT_CANDLE_OPEN": "Next candle open",
    "NEXT_CANDLE_CLOSE": "Next candle close",
    "HAMMER_CLOSE": "Hammer candle close",
    "HAMMER_HIGH": "Hammer candle high",
    "HAMMER_LOW": "Hammer candle low",
}

ENTRY_RULE_LABELS_WICK_OFF = {
    "NEXT_CANDLE_OPEN": "Next candle open",
    "NEXT_CANDLE_CLOSE": "Next candle close",
    "HAMMER_CLOSE": "Signal candle close",
    "HAMMER_HIGH": "Signal candle high",
    "HAMMER_LOW": "Signal candle low",
}


def _populate_enum_combo(combo: QComboBox, choices: List[Any], default_val: Any) -> None:
    """Dropdown stores enum value in itemData; label can be changed without breaking reads."""
    combo.clear()
    default_str = default_val.value if isinstance(default_val, Enum) else str(default_val)
    for choice in choices:
        if isinstance(choice, (tuple, list)) and len(choice) >= 2:
            value, label = str(choice[0]), str(choice[1])
        else:
            value = label = str(choice)
        combo.addItem(label, value)
    idx = combo.findData(default_str)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    elif combo.findText(default_str) >= 0:
        combo.setCurrentText(default_str)


MARKET_DATA_CHOICES = [
    ("spot", "Spot"),
    ("futures", "Futures"),
    ("both", "Both (spot + futures)"),
]


def _prefixed_shape_fields(prefix: str, field_defs) -> List[tuple]:
    """Duplicate Body/Wicks rows for Hammer with candles (group box title shows BUY vs SELL)."""
    return [
        (f"{prefix}_{name}", label, ftype, choices)
        for name, label, ftype, choices in field_defs
    ]


BODY_FIELDS = [
    ("body_pct", "Body % (target)", FIELD_TYPE_TEXT, None),
    ("body_tol", "Body Tolerance (+/-)", FIELD_TYPE_TEXT, None),
    ("body_tol_is_symmetric", "Body Tolerance Symmetric?", FIELD_TYPE_CHECK, None),
    ("body_tol_lower", "Body Tolerance Lower (if asymmetric)", FIELD_TYPE_TEXT, None),
    ("body_tol_upper", "Body Tolerance Upper (if asymmetric)", FIELD_TYPE_TEXT, None),
]

WICK_SHAPE_FIELDS = [
    ("dominant_wick_pct", "Dominant Wick % (target)", FIELD_TYPE_TEXT, None),
    ("dominant_wick_tol", "Dominant Wick Tolerance (+/-)", FIELD_TYPE_TEXT, None),
    ("dominant_tol_is_symmetric", "Dominant Wick Tolerance Symmetric?", FIELD_TYPE_CHECK, None),
    ("dominant_tol_lower", "Dominant Tolerance Lower (if asymmetric)", FIELD_TYPE_TEXT, None),
    ("dominant_tol_upper", "Dominant Tolerance Upper (if asymmetric)", FIELD_TYPE_TEXT, None),
    ("small_wick_pct", "Small Wick % (target)", FIELD_TYPE_TEXT, None),
    ("small_wick_tol", "Small Wick Tolerance (+/-)", FIELD_TYPE_TEXT, None),
    ("small_tol_is_symmetric", "Small Wick Tolerance Symmetric?", FIELD_TYPE_CHECK, None),
    ("small_tol_lower", "Small Tolerance Lower (if asymmetric)", FIELD_TYPE_TEXT, None),
    ("small_tol_upper", "Small Tolerance Upper (if asymmetric)", FIELD_TYPE_TEXT, None),
]

WICK_SIDE_FIELD = [
    ("hammer_wick_side", "Hammer Wick Side", FIELD_TYPE_DROPDOWN, enum_choices(logic.WickSide)),
]

WICK_FIELDS = WICK_SHAPE_FIELDS
HAMMER_SHAPE_FIELDS = BODY_FIELDS + WICK_SHAPE_FIELDS

HAMMER_RATIO_FIELD_NAMES = {f[0] for f in HAMMER_SHAPE_FIELDS}

BUY_CONTEXT_BODY_FIELDS = _prefixed_shape_fields("buy", BODY_FIELDS)
SELL_CONTEXT_BODY_FIELDS = _prefixed_shape_fields("sell", BODY_FIELDS)
BUY_CONTEXT_WICK_FIELDS = _prefixed_shape_fields("buy", WICK_SHAPE_FIELDS)
SELL_CONTEXT_WICK_FIELDS = _prefixed_shape_fields("sell", WICK_SHAPE_FIELDS)

BUY_WICK_REQUIRE_FIELDS = [
    ("buy_require_wick", "Require wick shape?", FIELD_TYPE_CHECK, None),
]
SELL_WICK_REQUIRE_FIELDS = [
    ("sell_require_wick", "Require wick shape?", FIELD_TYPE_CHECK, None),
]

HAMMER_WITH_CANDLES_SHAPE_FIELD_NAMES = {
    f[0] for f in (
        BUY_CONTEXT_BODY_FIELDS + SELL_CONTEXT_BODY_FIELDS
        + BUY_CONTEXT_WICK_FIELDS + SELL_CONTEXT_WICK_FIELDS
        + BUY_WICK_REQUIRE_FIELDS + SELL_WICK_REQUIRE_FIELDS
    )
}

DOJI_ONLY_FIELD_NAMES = {
    "doji_max_body_pct", "doji_max_body_tol",
    "doji_min_upper_wick_pct", "doji_min_lower_wick_pct", "doji_min_wick_tol",
    "doji_dominant_wick_pct", "doji_dominant_wick_tol",
    "doji_small_wick_pct", "doji_small_wick_tol", "doji_wick_bias_threshold_pct",
    "doji_style", "doji_direction_mode", "doji_fallback_direction",
    "doji_green_direction", "doji_red_direction",
    "doji_allow_green_trades", "doji_allow_red_trades",
}

DOJI_BODY_FIELDS = [
    ("doji_max_body_pct", "Max Body % (doji must be <= this)", FIELD_TYPE_TEXT, None),
    ("doji_max_body_tol", "Max Body Tolerance (+)", FIELD_TYPE_TEXT, None),
]

DOJI_WICK_FIELDS = [
    ("doji_min_upper_wick_pct", "Min Upper Wick %", FIELD_TYPE_TEXT, None),
    ("doji_min_lower_wick_pct", "Min Lower Wick %", FIELD_TYPE_TEXT, None),
    ("doji_min_wick_tol", "Min Wick Tolerance (-)", FIELD_TYPE_TEXT, None),
    ("doji_dominant_wick_pct", "Dominant Wick % (dragonfly/gravestone)", FIELD_TYPE_TEXT, None),
    ("doji_dominant_wick_tol", "Dominant Wick Tolerance (+/-)", FIELD_TYPE_TEXT, None),
    ("doji_small_wick_pct", "Small Wick % (dragonfly/gravestone)", FIELD_TYPE_TEXT, None),
    ("doji_small_wick_tol", "Small Wick Tolerance (+/-)", FIELD_TYPE_TEXT, None),
    ("doji_wick_bias_threshold_pct", "Wick Bias Threshold %", FIELD_TYPE_TEXT, None),
]

DOJI_DIRECTION_FIELDS = [
    ("doji_style", "Doji Style", FIELD_TYPE_DROPDOWN, enum_choices(doji_logic.DojiStyle)),
    ("doji_direction_mode", "Direction Mode", FIELD_TYPE_DROPDOWN, enum_choices(doji_logic.DojiDirectionMode)),
    ("doji_fallback_direction", "Fallback Direction (neutral doji / ties)", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeDirection)),
]

DOJI_CANDLE_COLOR_FIELDS = [
    ("doji_green_direction", "Green Doji → Direction", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeDirection)),
    ("doji_red_direction", "Red Doji → Direction", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeDirection)),
    ("doji_allow_green_trades", "Allow trades on GREEN doji", FIELD_TYPE_CHECK, None),
    ("doji_allow_red_trades", "Allow trades on RED doji", FIELD_TYPE_CHECK, None),
]

DOJI_SHAPE_FIELDS = DOJI_BODY_FIELDS + DOJI_WICK_FIELDS

# Dashboard widgets use a doji_ prefix; dataclass fields on DojiRatioConfig /
# DojiStrategyConfig do not — map UI names -> attribute names for defaults.
DOJI_UI_ATTR_OVERRIDES = {
    "doji_fallback_direction": "fallback_direction",
    "doji_green_direction": "green_direction",
    "doji_red_direction": "red_direction",
    "doji_allow_green_trades": "allow_green_trades",
    "doji_allow_red_trades": "allow_red_trades",
}

CONTEXT_UI_ATTR_OVERRIDES = {
    "context_lookback_candles": "lookback_candles",
    "context_enable_buy": "enable_buy",
    "context_enable_sell": "enable_sell",
}

# Per-indicator parameter widgets (field name -> label, type). Registry keys = dropdown ids.
INDICATOR_WIDGET_GROUPS: Dict[str, List[tuple]] = {
    "supertrend": [
        ("indicators_st_atr_period", "ATR period", FIELD_TYPE_TEXT, None),
        ("indicators_st_multiplier", "Multiplier", FIELD_TYPE_TEXT, None),
        ("indicators_st_apply_filter", "Apply trade filter (backtest + live)", FIELD_TYPE_CHECK, None),
    ],
    "vwap": [
        ("indicators_vwap_apply_filter", "Apply trade filter (backtest + live)", FIELD_TYPE_CHECK, None),
    ],
}


def default_value_for_field(name: str, sources: List) -> Any:
    """Resolve the initial widget value from one of the default dataclass instances."""
    if name in INDICATOR_UI_DEFAULTS:
        return INDICATOR_UI_DEFAULTS[name]
    if name.startswith("indicators_"):
        return INDICATOR_UI_DEFAULTS.get(name, "")

    if name.startswith("buy_"):
        ratio_attr = name[4:]
        if ratio_attr in HAMMER_RATIO_FIELD_NAMES:
            for candidate in sources:
                if candidate is not None and hasattr(candidate, "buy_hammer_ratios"):
                    return getattr(candidate.buy_hammer_ratios, ratio_attr)
    if name.startswith("sell_"):
        ratio_attr = name[5:]
        if ratio_attr in HAMMER_RATIO_FIELD_NAMES:
            for candidate in sources:
                if candidate is not None and hasattr(candidate, "sell_hammer_ratios"):
                    return getattr(candidate.sell_hammer_ratios, ratio_attr)

    attr = DOJI_UI_ATTR_OVERRIDES.get(name)
    if attr is None:
        attr = CONTEXT_UI_ATTR_OVERRIDES.get(name)
    if attr is None and name.startswith("doji_"):
        attr = name[len("doji_"):]
    elif attr is None:
        attr = name

    for candidate in sources:
        if candidate is None:
            continue
        if hasattr(candidate, attr):
            return getattr(candidate, attr)
    return ""

DIRECTION_FIELDS = [
    ("classic_green", "Classic Hammer — green candle", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeAction)),
    ("classic_red", "Classic Hammer — red candle", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeAction)),
    ("inverted_green", "Inverted Hammer — green candle", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeAction)),
    ("inverted_red", "Inverted Hammer — red candle", FIELD_TYPE_DROPDOWN, enum_choices(logic.TradeAction)),
]

DIRECTION_FIELD_NAMES = {f[0] for f in DIRECTION_FIELDS}

CONTEXT_PATTERN_FIELDS = [
    ("context_lookback_candles", "Prior candles to check (count)", FIELD_TYPE_TEXT, None),
    ("context_enable_buy", "Enable BUY (classic green hammer)", FIELD_TYPE_CHECK, None),
    ("context_enable_sell", "Enable SELL (inverted red hammer)", FIELD_TYPE_CHECK, None),
]
CONTEXT_PATTERN_FIELD_NAMES = {f[0] for f in CONTEXT_PATTERN_FIELDS}

INDICATOR_UI_DEFAULTS = {
    "indicators_st_atr_period": 10,
    "indicators_st_multiplier": 3.0,
    "indicators_st_apply_filter": True,
    "indicators_vwap_apply_filter": True,
    "indicators_combine_mode": IndicatorCombineMode.ALL.value,
}

# Field names used to show/hide parameter rows when the user switches pattern.
INVERTED_ENTRY_EXIT_FIELD_NAMES = {
    "inverted_entry_rule", "inverted_entry_offset",
    "inverted_buffer_mode", "inverted_sl_buffer_pct", "inverted_sl_buffer_flat",
}
HAMMER_ONLY_FIELD_NAMES = {
    f[0] for f in HAMMER_SHAPE_FIELDS + DIRECTION_FIELDS
} | INVERTED_ENTRY_EXIT_FIELD_NAMES

CLASSIC_ENTRY_EXIT_FIELDS = [
    ("entry_rule", "Entry Rule", FIELD_TYPE_DROPDOWN, enum_choices(logic.EntryRule)),
    ("entry_offset", "Entry Offset ($)", FIELD_TYPE_TEXT, None),
    ("sl_mode", "Stop Loss Mode", FIELD_TYPE_DROPDOWN, enum_choices(logic.StopLossMode)),
    ("sl_fixed_distance", "Fixed SL Distance ($)", FIELD_TYPE_TEXT, None),
    ("buffer_mode", "Buffer Mode (candle SL)", FIELD_TYPE_DROPDOWN, enum_choices(logic.BufferMode)),
    ("sl_buffer_pct", "SL Buffer %", FIELD_TYPE_TEXT, None),
    ("sl_buffer_flat", "SL Buffer Flat ($)", FIELD_TYPE_TEXT, None),
]

INVERTED_ENTRY_EXIT_FIELDS = [
    ("inverted_entry_rule", "Entry Rule", FIELD_TYPE_DROPDOWN, enum_choices(logic.EntryRule)),
    ("inverted_entry_offset", "Entry Offset ($)", FIELD_TYPE_TEXT, None),
    ("inverted_sl_mode", "Stop Loss Mode", FIELD_TYPE_DROPDOWN, enum_choices(logic.StopLossMode)),
    ("inverted_sl_fixed_distance", "Fixed SL Distance ($)", FIELD_TYPE_TEXT, None),
    ("inverted_buffer_mode", "Buffer Mode (candle SL)", FIELD_TYPE_DROPDOWN, enum_choices(logic.BufferMode)),
    ("inverted_sl_buffer_pct", "SL Buffer %", FIELD_TYPE_TEXT, None),
    ("inverted_sl_buffer_flat", "SL Buffer Flat ($)", FIELD_TYPE_TEXT, None),
]

ENTRY_EXIT_FIELDS = CLASSIC_ENTRY_EXIT_FIELDS + INVERTED_ENTRY_EXIT_FIELDS

DOJI_ENTRY_EXIT_FIELDS = [
    ("entry_rule", "Entry Rule", FIELD_TYPE_DROPDOWN, enum_choices(logic.EntryRule)),
    ("entry_offset", "Entry Offset ($)", FIELD_TYPE_TEXT, None),
    ("sl_mode", "Stop Loss Mode", FIELD_TYPE_DROPDOWN, enum_choices(logic.StopLossMode)),
    ("sl_fixed_distance", "Fixed SL Distance ($)", FIELD_TYPE_TEXT, None),
    ("buffer_mode", "Buffer Mode (candle SL)", FIELD_TYPE_DROPDOWN, enum_choices(logic.BufferMode)),
    ("sl_buffer_pct", "SL Buffer %", FIELD_TYPE_TEXT, None),
    ("sl_buffer_flat", "SL Buffer Flat ($)", FIELD_TYPE_TEXT, None),
]

RISK_CONTROL_FIELDS = [
    ("enable_risk_limit", "Enable Risk Limit?", FIELD_TYPE_CHECK, None),
    ("reject_zero_or_negative_risk", "Reject Zero/Negative Risk?", FIELD_TYPE_CHECK, None),
]

STRATEGY_FIELDS = (
    DIRECTION_FIELDS
    + ENTRY_EXIT_FIELDS + RISK_CONTROL_FIELDS
)

TIMEFRAME_LABELS = ["1h", "30m", "15m", "10m", "5m", "3m", "1m"]

TIMEFRAME_TO_FOLDER = {
    "1h": "1hour",
    "30m": "30min",
    "15m": "15min",
    "10m": "10min",
    "5m": "5min",
    "3m": "3min",
    "1m": "1min",
}

# Real price action gets choppier as the timeframe shortens -- used by
# PatternContextChart so the illustrative chart on each timeframe tab
# looks like a distinct, plausible chart instead of an identical shape
# repeated seven times. Values are a noise/pullback-frequency scale, not
# real backtest statistics.
TIMEFRAME_CHOPPINESS = {
    "1h": 0.018,
    "30m": 0.026,
    "15m": 0.034,
    "10m": 0.042,
    "5m": 0.052,
    "3m": 0.064,
    "1m": 0.080,
}

BACKTEST_FIELDS = [
    ("symbol", "Instrument / Symbol", FIELD_TYPE_TEXT, None),
    ("data_root", "Data Folder", FIELD_TYPE_TEXT, None),
    ("market_data", "Market data (spot / futures)", FIELD_TYPE_DROPDOWN, MARKET_DATA_CHOICES),
    ("start_date", "Backtest Start Date (YYYY-MM-DD, blank = all)", FIELD_TYPE_TEXT, None),
    ("end_date", "Backtest End Date (YYYY-MM-DD, blank = all)", FIELD_TYPE_TEXT, None),
    ("max_forward_candles", "Max Forward Scan (candles)", FIELD_TYPE_TEXT, None),
    ("position_sizing_mode", "Position Sizing Mode", FIELD_TYPE_DROPDOWN,
     enum_choices(backtest.PositionSizingMode)),
    ("position_size", "Position Size (if Fixed Units)", FIELD_TYPE_TEXT, None),
    ("fixed_risk_usd", "Fixed Risk per Trade ($)", FIELD_TYPE_TEXT, None),
    ("risk_pct_of_equity", "Risk per Trade (% of Equity)", FIELD_TYPE_TEXT, None),
    ("starting_capital", "Starting Capital ($)", FIELD_TYPE_TEXT, None),
    ("equity_floor_usd", "Equity Floor ($) -- halt below this", FIELD_TYPE_TEXT, None),
    ("max_equity_multiple", "Max Equity Multiple (compounding cap)", FIELD_TYPE_TEXT, None),
    ("allow_overlapping_trades", "Allow Overlapping Trades?", FIELD_TYPE_CHECK, None),
    ("commission_per_trade", "Commission per Trade ($)", FIELD_TYPE_TEXT, None),
    ("slippage_usd", "Slippage ($)", FIELD_TYPE_TEXT, None),
]


# ============================================================================
# FIELD HELP TEXT -- shown as a tooltip on hover for every parameter, so
# the client can see what each setting actually does without leaving the
# dashboard. Falls back to the field's label if a field isn't listed here.
# ============================================================================
FIELD_HELP: Dict[str, str] = {
    "body_pct": "Target candle body size as a % of the candle's full high-low range.",
    "body_tol": "How far the body % can drift from the target above and below (used when Symmetric is on).",
    "body_tol_is_symmetric": "On: one tolerance value applies equally above and below the target.\nOff: set separate lower/upper tolerances.",
    "body_tol_lower": "Body % tolerance only on the low side (used when Symmetric is off).",
    "body_tol_upper": "Body % tolerance only on the high side (used when Symmetric is off).",
    "dominant_wick_pct": "Target size of the long (dominant) wick as a % of the candle's range.",
    "dominant_wick_tol": "How far the dominant wick % can drift from the target (symmetric case).",
    "dominant_tol_is_symmetric": "On: one tolerance value applies both directions.\nOff: set separate lower/upper tolerances for the dominant wick.",
    "dominant_tol_lower": "Dominant wick % tolerance on the low side only.",
    "dominant_tol_upper": "Dominant wick % tolerance on the high side only.",
    "small_wick_pct": "Target size of the short wick on the opposite side of the candle.",
    "small_wick_tol": "How far the small wick % can drift from the target (symmetric case).",
    "small_tol_is_symmetric": "On: one tolerance value applies both directions.\nOff: set separate lower/upper tolerances for the small wick.",
    "small_tol_lower": "Small wick % tolerance on the low side only.",
    "small_tol_upper": "Small wick % tolerance on the high side only.",
    "hammer_wick_side": (
        "Preview fallback only. Detection follows the Direction tab: "
        "if any Classic row is Buy/Sell, classic (long lower wick) is on; "
        "if any Inverted row is Buy/Sell, inverted (long upper wick) is on."
    ),
    "doji_max_body_pct": "Maximum body size (% of range) for a valid doji -- body must be at or below this (+ tolerance).",
    "doji_max_body_tol": "Extra body % allowed above the max (effective ceiling = max + tolerance).",
    "doji_min_upper_wick_pct": "Minimum upper wick % for CLASSIC / LONG_LEGGED doji styles.",
    "doji_min_lower_wick_pct": "Minimum lower wick % for CLASSIC / LONG_LEGGED doji styles.",
    "doji_min_wick_tol": "How far min wick % can drop below the target and still pass.",
    "doji_dominant_wick_pct": "Long wick target % for DRAGONFLY / GRAVESTONE / LONG_LEGGED styles.",
    "doji_dominant_wick_tol": "Tolerance around the dominant wick % target.",
    "doji_small_wick_pct": "Short wick target % for DRAGONFLY / GRAVESTONE styles.",
    "doji_small_wick_tol": "Tolerance around the small wick % target.",
    "doji_wick_bias_threshold_pct": "Minimum upper/lower wick difference before WICK_BIAS picks BUY vs SELL.",
    "doji_style": "Which doji sub-type must match: ANY, CLASSIC, DRAGONFLY, GRAVESTONE, or LONG_LEGGED.",
    "doji_direction_mode": "How to pick trade direction: WICK_BIAS, CANDLE_COLOR (green/red on the doji), FIXED_BUY/SELL, or NEXT_CANDLE_COLOR.",
    "doji_fallback_direction": "Direction used when wicks are tied (WICK_BIAS) or next candle is also a doji.",
    "doji_green_direction": "When Direction Mode is CANDLE_COLOR: trade direction if the doji closes green.",
    "doji_red_direction": "When Direction Mode is CANDLE_COLOR: trade direction if the doji closes red.",
    "doji_allow_green_trades": "When off, green dojis never produce a trade (CANDLE_COLOR mode).",
    "doji_allow_red_trades": "When off, red dojis never produce a trade (CANDLE_COLOR mode).",
    "indicators_combine_mode": "When two or more indicators are added: ALL = every filter must pass; ANY = at least one.",
    "green_direction": "Legacy field — use Classic/Inverted green dropdowns on the Direction tab.",
    "red_direction": "Legacy field — use Classic/Inverted red dropdowns on the Direction tab.",
    "classic_green": "When a CLASSIC hammer (long lower wick) closes green: BUY, SELL, or NO (skip).",
    "context_lookback_candles": (
        "How many candles before the signal bar are part of the pattern (0, 1, 2, 3, 5, …). "
        "0 = only the signal candle (no prior-close check). Those N bars plus the signal "
        "are coloured on Pattern In Context; everything else stays grey."
    ),
    "context_enable_buy": (
        "Turn BUY setups on. Wick on = classic green hammer. "
        "Wick off = signal body ≤ Body % (any color); previous candle must be red. "
        "Prior closes must stay at or above candle low."
    ),
    "context_enable_sell": (
        "Turn SELL setups on. Wick on = inverted red hammer. "
        "Wick off = signal body ≤ Body % (any color); previous candle must be green. "
        "Prior closes must stay at or below candle high."
    ),
    "buy_require_wick": (
        "On (default): BUY needs a classic hammer (long lower wick) that closes green.\n"
        "Off: ignore wick shape AND signal candle color. BUY when the previous candle is RED "
        "and the signal body is at most Body % of range (e.g. Body % = 10)."
    ),
    "sell_require_wick": (
        "On (default): SELL needs an inverted hammer (long upper wick) that closes red.\n"
        "Off: ignore wick shape AND signal candle color. SELL when the previous candle is GREEN "
        "and the signal body is at most Body % of range (e.g. Body % = 10)."
    ),
    "classic_red": "When a CLASSIC hammer (long lower wick) closes red: BUY, SELL, or NO (skip).",
    "inverted_green": "When an INVERTED hammer (long upper wick) closes green: BUY, SELL, or NO (skip).",
    "inverted_red": "When an INVERTED hammer (long upper wick) closes red: BUY, SELL, or NO (skip).",
    "enable_classic_hammer": "Legacy — set Classic green and Classic red to NO to turn classic off.",
    "enable_inverted_hammer": "Legacy — set Inverted green and Inverted red to NO to turn inverted off.",
    "classic_hammer_allow_buy": "Legacy — use Classic green/red = BUY instead.",
    "classic_hammer_allow_sell": "Legacy — use Classic green/red = SELL instead.",
    "inverted_hammer_allow_buy": "Legacy — use Inverted green/red = BUY instead.",
    "inverted_hammer_allow_sell": "Legacy — use Inverted green/red = SELL instead.",
    "indicators_st_enabled": "Compute SuperTrend and show it in Pattern In Context; optional backtest filter.",
    "indicators_vwap_enabled": "Compute VWAP and show it in Pattern In Context; optional backtest filter.",
    "entry_rule": "Classic hammer entry: next candle open/close, or this candle’s close / high / low.",
    "entry_offset": "Classic hammer: extra $ added to the classic entry price.",
    "sl_mode": (
        "Stop anchor: CANDLE_EXTREME = signal candle low (BUY) or high (SELL) plus buffer. "
        "FIXED_FROM_ENTRY = entry price minus/plus Fixed SL Distance ($)."
    ),
    "sl_fixed_distance": "Price distance from entry when Stop Loss Mode is Fixed from entry (e.g. 5.0 on XAUUSD).",
    "buffer_mode": "Classic hammer: extra SL room type when Stop Loss Mode is Candle extreme.",
    "sl_buffer_pct": "Classic hammer: extra SL room as % (when Buffer Mode is percent-based).",
    "sl_buffer_flat": "Classic hammer: extra SL room as a flat $ (when Buffer Mode is flat).",
    "inverted_sl_mode": "Inverted / SELL stop anchor — same choices as classic Stop Loss Mode.",
    "inverted_sl_fixed_distance": "Fixed SL distance ($) for inverted/SELL when using Fixed from entry.",
    "inverted_entry_rule": (
        "Inverted hammer entry — separate from Classic. "
        "Do not reuse Classic HAMMER_HIGH here unless you really want the long-wick tip."
    ),
    "inverted_entry_offset": "Inverted hammer: extra $ added to the inverted entry price.",
    "inverted_buffer_mode": "Inverted hammer stop-loss buffer type.",
    "inverted_sl_buffer_pct": "Inverted hammer: extra SL room as % (when inverted Buffer Mode is percent-based).",
    "inverted_sl_buffer_flat": "Inverted hammer: extra SL room as a flat $ (when inverted Buffer Mode is flat).",
    "enable_risk_limit": "On: trades that would risk more than the configured limit are skipped.",
    "reject_zero_or_negative_risk": "On: skip any trade where entry and stop-loss end up on the same side (zero or negative risk).",
    "symbol": "Instrument folder name under the Data Folder to backtest (e.g. EURUSD).",
    "data_root": (
        "Folder containing your historical price CSVs. Can be the parent data/ folder "
        "or a market folder (data/spot, data/futures). Use Market data to choose spot vs futures."
    ),
    "market_data": (
        "Which CSV tree to backtest (default Spot):\n"
        "• Spot — data/spot/SYMBOL, or your existing data/SYMBOL folder\n"
        "• Futures — data/futures/SYMBOL\n"
        "• Both — run spot and futures in one backtest (tagged in results)\n"
        "No need to move folders; Spot still finds legacy data/SYMBOL."
    ),
    "start_date": "Only include candles on/after this date. Leave blank to use all available history.",
    "end_date": "Only include candles on/before this date. Leave blank to use all available history.",
    "max_forward_candles": "How many candles forward the backtest scans looking for the trade's exit before giving up.",
    "position_sizing_mode": (
        "Backtest only — pick how lot size / $ P&L is simulated:\n"
        "• FIXED_UNITS — every trade uses Position Size (lots/units). Equity does NOT change size.\n"
        "• FIXED_RISK_USD — every trade risks the same Fixed Risk $ (recommended). Equity does NOT compound size.\n"
        "• PERCENT_OF_EQUITY — risks % of current equity (this is the only compounding mode).\n"
        "Live MT5 uses the lot size on the Live panel, not these fields."
    ),
    "position_size": "Units/lots per trade when Position Sizing Mode is Fixed Units. Same size on every trade.",
    "fixed_risk_usd": "$ risked per trade when mode is Fixed Risk. Size = this $ / (entry − stop). Does not grow with wins.",
    "risk_pct_of_equity": "% of current equity risked per trade (Percent of Equity mode only). This is the compounding mode.",
    "starting_capital": "Account the equity curve starts from. Used for % of equity sizing and for return % / drawdown.",
    "equity_floor_usd": "Percent of Equity only: stop taking new trades if equity falls to/below this.",
    "max_equity_multiple": "Percent of Equity only: never size off more than this × Starting Capital (stops runaway compounding).",
    "allow_overlapping_trades": "On: a new signal can open a trade while a previous one is still open.",
    "commission_per_trade": "Flat $ commission charged per trade, subtracted from P&L.",
    "slippage_usd": "Flat $ slippage assumed on entry and exit, subtracted from P&L.",
}

# Hammer with candles: same tooltips as plain hammer fields, per BUY/SELL side.
for _side_prefix in ("buy", "sell"):
    for _base_name, _help_text in list(FIELD_HELP.items()):
        if _base_name in HAMMER_RATIO_FIELD_NAMES:
            FIELD_HELP[f"{_side_prefix}_{_base_name}"] = _help_text

SYMMETRIC_TOLERANCE_GROUPS = (
    ("body_tol_is_symmetric", "body_tol", "body_tol_lower", "body_tol_upper"),
    ("dominant_tol_is_symmetric", "dominant_wick_tol", "dominant_tol_lower", "dominant_tol_upper"),
    ("small_tol_is_symmetric", "small_wick_tol", "small_tol_lower", "small_tol_upper"),
)

# Which Run Settings fields matter for each position sizing mode (UI highlight only).
SIZING_MODE_ACTIVE_FIELDS: Dict[str, List[str]] = {
    "FIXED_UNITS": ["position_size", "starting_capital", "commission_per_trade", "slippage_usd"],
    "fixed_units": ["position_size", "starting_capital", "commission_per_trade", "slippage_usd"],
    "FIXED_RISK_USD": ["fixed_risk_usd", "starting_capital", "commission_per_trade", "slippage_usd"],
    "fixed_risk_usd": ["fixed_risk_usd", "starting_capital", "commission_per_trade", "slippage_usd"],
    "PERCENT_OF_EQUITY": [
        "risk_pct_of_equity", "starting_capital", "equity_floor_usd", "max_equity_multiple",
        "commission_per_trade", "slippage_usd",
    ],
    "percent_of_equity": [
        "risk_pct_of_equity", "starting_capital", "equity_floor_usd", "max_equity_multiple",
        "commission_per_trade", "slippage_usd",
    ],
}

PARAMETERS_GUIDE_TEXT = """
<b>Project goal</b><br>
Find hammer/doji setups on historical data, filter with indicators, compare position-sizing and RR/SL — then run the same signal rules live on MT5 (Windows).<br><br>

<b>What affects a trade signal</b> (backtest + live)<br>
• <b>Body / Wicks</b> — hammer shape percentages.<br>
• <b>Direction</b> — four choices: Classic green, Classic red, Inverted green, Inverted red. Each is BUY, SELL, or NO. Entry is the <i>next</i> bar.<br>
• <b>Hammer types</b> — a type is on if either of its color rows is Buy or Sell (NO + NO turns that type off).<br>
• <b>Entry / Exit</b> — Classic and Inverted each have their own entry rule and SL buffer. HAMMER_HIGH is candle high (wrong wick on inverted unless you set Inverted separately).<br>
• <b>Timeframes</b> — RR and max SL per TF, risk-limit toggles, and include-in-run checkboxes.<br>
• <b>Indicators</b> — add SuperTrend/VWAP; “Apply trade filter” must be on to block trades live/backtest.<br><br>

<b>Backtest only</b> (Run Settings tab)<br>
• <b>Position sizing</b> — FIXED_UNITS | FIXED_RISK_USD | PERCENT_OF_EQUITY (compare strategies).<br>
• Starting capital, equity floor, max equity multiple, overlap, commission, slippage, date range, max forward scan.<br>
• Outputs: CSV ledger with position_size, risk_usd, pnl_usd per exit model.<br><br>

<b>Live only</b> (Live panel + Live settings)<br>
• Symbol, timeframe, lots, magic, dry run, daily loss/trade caps — not backtest sizing fields.<br><br>

<b>Presets (v2 JSON)</b><br>
Save/load includes trade_rules, backtest block, indicators, all field widgets, and timeframe RR/SL — use for client sign-off.
"""

# Results table row/column tints (BUY / SELL / candle_bias exit model).
RESULT_BG_BUY = QColor("#D7F5DD")
RESULT_BG_SELL = QColor("#FADBD8")
RESULT_BG_CANDLE_BIAS = QColor("#D2E3FC")
RESULT_BG_NEUTRAL = QColor("#FFFFFF")
# Trade ledger text: win / loss (direction stays on row background).
RESULT_FG_WIN = QColor("#137333")
RESULT_FG_LOSS = QColor("#C5221F")
TRADE_LEDGER_DISPLAY_MAX_ROWS = 2500


def _normalize_result_label(val) -> str:
    if val is None:
        return ""
    return str(val).strip().upper().replace(" ", "_")


def result_tint_for_row(row: Dict[str, Any]) -> Optional[QColor]:
    """
    Row background for metrics / trade tables.
    Candle-bias exit model wins over direction when both are present.
    """
    exit_model = _normalize_result_label(row.get("exit_model"))
    if exit_model == "CANDLE_BIAS":
        return RESULT_BG_CANDLE_BIAS
    direction = _normalize_result_label(row.get("direction"))
    group = _normalize_result_label(row.get("group"))
    side = direction or group
    if side == "BUY":
        return RESULT_BG_BUY
    if side == "SELL":
        return RESULT_BG_SELL
    return None


def result_fg_for_outcome(row: Dict[str, Any]) -> Optional[QColor]:
    """Trade text color from outcome: WIN = green, LOSS = red."""
    outcome = _normalize_result_label(row.get("outcome"))
    if outcome == "WIN":
        return RESULT_FG_WIN
    if outcome == "LOSS":
        return RESULT_FG_LOSS
    return None


def result_tint_for_exit_model_column(exit_model_name: str) -> Optional[QColor]:
    if _normalize_result_label(exit_model_name) == "CANDLE_BIAS":
        return RESULT_BG_CANDLE_BIAS
    return None


def _table_item(
    text: str,
    background: Optional[QColor] = None,
    foreground: Optional[QColor] = None,
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    if background is not None:
        item.setBackground(background)
    if foreground is not None:
        item.setForeground(foreground)
    return item


def _configure_table_widget_mac(table: QTableWidget):
    """Reduce NSTableView + dock reparent crashes on macOS."""
    if sys.platform != "darwin":
        return
    table.setAttribute(Qt.WA_MacShowFocusRect, False)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)


# ============================================================================
# MODERN QT STYLESHEET -- gives the "finished" look the client asked for
# ============================================================================
def _stylesheet_font_family() -> str:
    if sys.platform == "darwin":
        return "'Helvetica Neue', Arial, sans-serif"
    return "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"


def _stylesheet_mono_font_family() -> str:
    if sys.platform == "darwin":
        return "Menlo, monospace"
    return "Consolas, monospace"


STYLESHEET = """
QMainWindow, QWidget {
    background-color: #F3F5F7;
    font-family: @@APP_FONT@@;
    font-size: 13px;
    color: #202124;
}
QGroupBox {
    background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    margin-top: 16px; padding: 14px; font-weight: 600; font-size: 13.5px; color: #202124;
}
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 8px; color: #188038; }

/* ---- Tabs: sized to fit their text, never truncated ---- */
QTabWidget::pane { border: 1px solid #DDE1E6; border-radius: 10px; background: #FFFFFF; top: -1px; }
QTabWidget::tab-bar { alignment: left; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: #E9ECEF; color: #444B52; padding: 10px 20px; margin-right: 4px;
    border-top-left-radius: 8px; border-top-right-radius: 8px; font-weight: 600;
    font-size: 12.5px; min-width: 80px;
}
QTabBar::tab:selected { background: #FFFFFF; color: #188038; font-weight: 700; border: 1px solid #DDE1E6; border-bottom: none; }
QTabBar::tab:hover:!selected { background: #F1F3F4; color: #188038; }

/* ---- Inputs ---- */
QLineEdit, QComboBox {
    border: 1px solid #D2D6DB; border-radius: 6px; padding: 7px 10px;
    background: #FFFFFF; selection-background-color: #34A853; selection-color: white;
    font-size: 13.5px; font-weight: 500;
}
QLineEdit:hover, QComboBox:hover { border: 1px solid #9AA0A6; }
QLineEdit:focus, QComboBox:focus { border: 1.5px solid #34A853; }
QLineEdit:disabled, QComboBox:disabled { background: #F1F3F4; color: #9AA0A6; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #5F6368; margin-right: 10px;
}
QComboBox QAbstractItemView {
    border: 1px solid #D2D6DB; border-radius: 6px; background: #FFFFFF;
    selection-background-color: #34A853; selection-color: #FFFFFF; outline: none;
    padding: 2px;
}

/* ---- Buttons: green, primary action colour, with hover/press states ---- */
QPushButton {
    background-color: #34A853; color: white; border: none; border-radius: 7px;
    padding: 11px 24px; font-weight: 700; font-size: 14px;
}
QPushButton:hover { background-color: #2E9549; }
QPushButton:pressed { background-color: #257A3C; }
QPushButton:disabled { background-color: #BDC1C6; color: #F1F1F1; }
QPushButton#previewSideToggle {
    background-color: #FFFFFF;
    color: #1A1A1A;
    border: 1px solid #DADCE0;
    padding: 6px 12px;
    border-radius: 6px;
}
QPushButton#previewSideToggle:hover { background-color: #F1F3F4; }
QPushButton#previewSideToggle:checked {
    background-color: #E6F4EA;
    border-color: #188038;
    color: #188038;
    font-weight: bold;
}
QPushButton#secondaryButton {
    background-color: #FFFFFF; color: #188038; border: 1.5px solid #34A853;
    font-weight: 600; padding: 8px 18px; font-size: 12px;
}
QPushButton#secondaryButton:hover { background-color: #E6F4EA; }
QPushButton#secondaryButton:pressed { background-color: #CEEAD6; }

/* ---- Tables ---- */
QTableWidget {
    background: #FFFFFF; alternate-background-color: #F7F9FA; gridline-color: #EDEDED;
    border: 1px solid #DDE1E6; border-radius: 8px; font-size: 12.5px;
}
QTableWidget::item { padding: 3px; }
QTableWidget::item:selected { background-color: #C2E7FF; color: #202124; }
QHeaderView::section {
    background-color: #34A853; color: white; padding: 7px; border: none; font-weight: 700; font-size: 12px;
}
QLineEdit#tableNumberInput {
    border: 1px solid transparent; border-radius: 5px; background: transparent;
    font-size: 14px; font-weight: 700; color: #202124; padding: 4px 6px;
}
QLineEdit#tableNumberInput:hover { border: 1px solid #D2D6DB; background: #FFFFFF; }
QLineEdit#tableNumberInput:focus { border: 1.5px solid #34A853; background: #FFFFFF; }

QProgressBar { border: 1px solid #DDE1E6; border-radius: 6px; text-align: center; background: #F1F3F4; height: 10px; }
QProgressBar::chunk { background-color: #34A853; border-radius: 6px; }
QScrollArea { border: none; }

QCheckBox { spacing: 8px; font-size: 12.5px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1.5px solid #9AA0A6; background: #FFFFFF; }
QCheckBox::indicator:hover { border: 1.5px solid #34A853; }
QCheckBox::indicator:checked { background-color: #34A853; border: 1.5px solid #34A853; image: url(checkicon:checkmark.png); }

QLabel#sectionHint { color: #5F6368; font-size: 11.5px; }
QFrame#topBar { background-color: #FFFFFF; border-bottom: 1px solid #DDE1E6; }
QFrame#candleCard { background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px; }

QFrame#liveHero {
    background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    padding: 12px 16px;
}
QLabel#liveHeroTitle { font-size: 15px; font-weight: 700; color: #188038; }
QLabel#liveStepChip {
    background-color: #E6F4EA; color: #188038; border-radius: 6px;
    padding: 4px 10px; font-size: 11px; font-weight: 700;
}
QFrame#liveControlBar {
    background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    padding: 10px 12px;
}
QFrame#liveNotifyBar {
    background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    padding: 10px 12px;
}
QLabel#liveNotifyTitle { font-size: 12.5px; font-weight: 700; color: #188038; }
QLabel#liveNotifyStatus {
    background-color: #F1F3F4; border: 1px solid #E8EAED; border-radius: 6px;
    padding: 3px 10px; font-size: 11px; font-weight: 600; color: #5F6368;
}
QLabel#liveNotifyStatus[ready="true"] {
    background-color: #E6F4EA; border-color: #CEEAD6; color: #188038;
}
QLabel#liveStatusBadge {
    background-color: #F1F3F4; border: 1px solid #DDE1E6; border-radius: 8px;
    padding: 10px 14px; font-size: 12.5px; font-weight: 600; color: #444B52;
}
QLabel#liveSummaryCard {
    background-color: #F7F9FA; border: 1px solid #E8EAED; border-radius: 8px;
    padding: 10px 12px; font-size: 12px; color: #3C4043; line-height: 140%;
}
QPlainTextEdit#liveLogConsole {
    font-family: @@MONO_FONT@@; font-size: 11.5px;
    background: #FAFBFC; border: 1px solid #E8EAED; border-radius: 8px; padding: 8px;
}
QGroupBox#liveLogGroup { margin-top: 12px; }

QLabel#fieldLabel {
    color: #3C4043; font-size: 12.5px; font-weight: 600;
}
QLabel#appStatus {
    color: #188038; font-size: 12.5px; font-weight: 600;
}
QLabel#emptyState {
    color: #80868B; font-size: 13px; padding: 24px;
    background: #FAFBFC; border: 1px dashed #DDE1E6; border-radius: 10px;
}
QFrame#paramsHeader {
    background: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    padding: 4px 12px;
}
QFrame#resultsToolbar {
    background: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 10px;
    padding: 8px 10px;
}
QPushButton#primaryRunButton {
    min-height: 20px; padding: 12px 28px; font-size: 15px;
}
QPushButton#liveStartButton {
    min-width: 120px;
}
QStatusBar#appStatusBar {
    background: #FFFFFF; border-top: 1px solid #DDE1E6; color: #444B52; font-size: 12px;
}
QTabWidget#mainParamTabs QTabBar::tab { padding: 8px 16px; min-width: 72px; }
QTabWidget#resultsMainTabs QTabBar::tab { padding: 9px 18px; }

QScrollBar:vertical { background: #F3F5F7; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #C7CBD1; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #34A853; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip {
    background-color: #202124; color: #FFFFFF; border: 1px solid #34A853;
    border-radius: 5px; padding: 6px 9px; font-size: 12px;
}

/* ---- Menu bar / View menu ---- */
QMenuBar { background-color: #FFFFFF; border-bottom: 1px solid #DDE1E6; padding: 2px; }
QMenuBar::item { padding: 6px 12px; background: transparent; border-radius: 5px; }
QMenuBar::item:selected { background-color: #E6F4EA; color: #188038; }
QMenu { background-color: #FFFFFF; border: 1px solid #DDE1E6; border-radius: 6px; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 4px; }
QMenu::item:selected { background-color: #E6F4EA; color: #188038; }

/* ---- Dock widgets: draggable/floatable/resizable panels ---- */
QDockWidget { font-weight: 700; font-size: 12.5px; color: #202124; }
QDockWidget::title {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #EEF0F2);
    padding: 10px 12px; text-align: left;
    border-bottom: 2px solid #34A853;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}
QDockWidget::close-button, QDockWidget::float-button {
    background: transparent; border: none; padding: 2px; icon-size: 13px;
}

/* ---- Panel dividers: wide enough to actually grab and drag.
   Without this rule the native separator is ~1px and users think
   the panels are not resizable at all. ---- */
QMainWindow::separator {
    background: #DDE1E6;
    width: 7px;    /* separator between side-by-side panels */
    height: 7px;   /* separator between stacked panels */
    border-radius: 3px;
}
QMainWindow::separator:hover {
    background: #34A853;
}

/* ---- Pattern-In-Context timeframe tabs: 7 tabs need to fit in a
   narrower dock, so give this specific tab bar tighter sizing than
   the main parameter tabs. ---- */
QTabWidget#contextTimeframeTabs QTabBar::tab {
    min-width: 40px; padding: 7px 10px; font-size: 11.5px;
}
"""
STYLESHEET = STYLESHEET.replace("@@APP_FONT@@", _stylesheet_font_family())
STYLESHEET = STYLESHEET.replace("@@MONO_FONT@@", _stylesheet_mono_font_family())


# ============================================================================
# CANDLE PREVIEW WIDGET -- Qt equivalent of the Tkinter Canvas drawing
# ============================================================================
class CandleWidget(QWidget):
    """
    Paints ONE candle from explicit body/dominant/small percentages.
    This is the Qt equivalent of dashboard.py's _draw_candle_on_canvas --
    same math (proportional body/wick heights from the three
    percentages), just using QPainter instead of Tkinter Canvas calls.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.body_pct = 20.0
        self.dominant_pct = 60.0
        self.small_pct = 20.0
        self.wick_side = "LOWER"
        self.is_green = True
        self.setMinimumSize(120, 160)

    def set_shape(self, body_pct, dominant_pct, small_pct, wick_side, is_green):
        self.body_pct = body_pct
        self.dominant_pct = dominant_pct
        self.small_pct = small_pct
        self.wick_side = wick_side
        self.is_green = is_green
        self.update()  # triggers a repaint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor("#FFFFFF"))
        painter.setPen(QPen(QColor("#CCCCCC"), 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        total = self.body_pct + self.dominant_pct + self.small_pct
        if total <= 0:
            return

        body_frac = self.body_pct / total
        dominant_frac = self.dominant_pct / total
        small_frac = self.small_pct / total

        margin = max(10, h * 0.08)
        usable_h = h - 2 * margin
        center_x = w // 2
        # Cap body width so a wide, short dock (e.g. floated panel) does not draw a flat green bar.
        body_width = min(max(20, w * 0.35), max(28, h * 0.5))

        body_h = max(4.0, usable_h * body_frac)
        dominant_h = max(4.0, usable_h * dominant_frac)
        small_h = max(2.0, usable_h * small_frac)

        long_wick_on_bottom = (self.wick_side != "UPPER")

        if long_wick_on_bottom:
            small_top = margin
            body_top = margin + small_h
            body_bottom = body_top + body_h
            dominant_bottom = body_bottom + dominant_h
        else:
            dominant_top = margin
            body_top = margin + dominant_h
            body_bottom = body_top + body_h
            small_bottom = body_bottom + small_h

        body_color = QColor("#2ECC71") if self.is_green else QColor("#E74C3C")
        wick_pen = QPen(QColor("#555555"), 2)

        painter.setPen(wick_pen)
        if long_wick_on_bottom:
            painter.drawLine(center_x, int(small_top), center_x, int(body_top))
            painter.drawLine(center_x, int(body_bottom), center_x, int(dominant_bottom))
        else:
            painter.drawLine(center_x, int(dominant_top), center_x, int(body_top))
            painter.drawLine(center_x, int(body_bottom), center_x, int(small_bottom))

        painter.setPen(QPen(QColor("#333333"), 1.5))
        painter.setBrush(QBrush(body_color))
        painter.drawRect(int(center_x - body_width / 2), int(body_top),
                          int(body_width), int(body_bottom - body_top))


# ============================================================================
# PATTERN-IN-CONTEXT CHART -- shows the signal candle inside a small
# illustrative trade setup (downtrend -> signal -> entry -> uptrend to a
# profit target), one per timeframe, so the client can see how the
# parameters they're setting translate into an actual trade idea instead
# of an isolated candle shape. Modeled on the reference chart the client
# provided (SELL / PROFIT / RESISTANCE / SUPPORT / BUY annotations).
#
# This is a conceptual guide, not real price data -- there is no chart
# to draw from until a backtest actually runs. The lead-in/trail-out
# candles are a fixed illustrative shape; only the signal candle (shape +
# colour) and the RR/SL caption are driven by the live parameter values,
# per timeframe.
#
# add_overlay() is a deliberate extension point: future indicator
# overlays (moving averages, etc.) can be added here later without
# touching the rest of the chart.
# ============================================================================
class PatternContextChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 210)
        self.body_pct = 20.0
        self.dominant_pct = 60.0
        self.small_pct = 20.0
        self.wick_side = "LOWER"
        self.is_green = True
        self.rr_multiple = 2.0
        self.max_sl_usd = 50.0
        self.timeframe_label = "1h"
        self.is_doji = False
        self.doji_style = ""
        self.preview_show_st = False
        self.preview_st_bullish = True
        self.preview_show_vwap = False
        self.preview_price_above_vwap = True
        self.preview_trade_side = "BUY"
        self.context_lookback = 5
        self.entry_rule = "NEXT_CANDLE_OPEN"
        self.entry_offset = 0.0
        self.buffer_mode = "PERCENT_OF_RANGE"
        self.sl_buffer_pct = 5.0
        self.sl_buffer_flat = 0.0
        self.sl_mode = logic.StopLossMode.CANDLE_EXTREME.value
        self.sl_fixed_distance = 5.0
        self._context_trade_side = None
        self._overlays = []
        self._lead_candles = []
        self._trail_candles = []
        self._context_timeframe = None
        self._regenerate_context_candles("1h")

    def set_data(self, body_pct, dominant_pct, small_pct, wick_side, is_green,
                 rr_multiple, max_sl_usd, timeframe_label, is_doji=False, doji_style="",
                 preview_show_st=False, preview_st_bullish=True,
                 preview_show_vwap=False, preview_price_above_vwap=True,
                 preview_trade_side="BUY",
                 context_lookback=5,
                 entry_rule="NEXT_CANDLE_OPEN",
                 entry_offset=0.0,
                 buffer_mode="PERCENT_OF_RANGE",
                 sl_buffer_pct=5.0,
                 sl_buffer_flat=0.0,
                 sl_mode=logic.StopLossMode.CANDLE_EXTREME.value,
                 sl_fixed_distance=5.0):
        self.body_pct = body_pct
        self.dominant_pct = dominant_pct
        self.small_pct = small_pct
        self.wick_side = wick_side
        self.is_green = is_green
        self.is_doji = is_doji
        self.doji_style = doji_style or ""
        self.preview_show_st = preview_show_st
        self.preview_st_bullish = preview_st_bullish
        self.preview_show_vwap = preview_show_vwap
        self.preview_price_above_vwap = preview_price_above_vwap
        self.preview_trade_side = preview_trade_side or "BUY"
        try:
            raw_lb = 0 if context_lookback is None else context_lookback
            self.context_lookback = max(0, min(60, int(raw_lb)))
        except (TypeError, ValueError):
            self.context_lookback = 5
        self.entry_rule = str(entry_rule or "NEXT_CANDLE_OPEN")
        try:
            self.entry_offset = float(entry_offset or 0.0)
        except (TypeError, ValueError):
            self.entry_offset = 0.0
        self.buffer_mode = str(buffer_mode or "PERCENT_OF_RANGE")
        try:
            self.sl_buffer_pct = float(sl_buffer_pct or 0.0)
        except (TypeError, ValueError):
            self.sl_buffer_pct = 0.0
        try:
            self.sl_buffer_flat = float(sl_buffer_flat or 0.0)
        except (TypeError, ValueError):
            self.sl_buffer_flat = 0.0
        raw_sl_mode = sl_mode if sl_mode is not None else logic.StopLossMode.CANDLE_EXTREME.value
        if isinstance(raw_sl_mode, logic.StopLossMode):
            raw_sl_mode = raw_sl_mode.value
        self.sl_mode = str(raw_sl_mode)
        try:
            self.sl_fixed_distance = float(sl_fixed_distance or 0.0)
        except (TypeError, ValueError):
            self.sl_fixed_distance = 0.0
        self.rr_multiple = rr_multiple
        self.max_sl_usd = max_sl_usd
        self.timeframe_label = timeframe_label
        if (
            timeframe_label != self._context_timeframe
            or self.preview_trade_side != self._context_trade_side
            or self.context_lookback != getattr(self, "_last_context_lookback", None)
        ):
            self._regenerate_context_candles(timeframe_label, self.preview_trade_side)
            self._last_context_lookback = self.context_lookback
        self.update()

    def _regenerate_context_candles(self, timeframe_label: str, trade_side: str = "BUY"):
        choppiness = TIMEFRAME_CHOPPINESS.get(timeframe_label, 0.04)
        side_key = "SELL" if trade_side == "SELL" else "BUY"
        rng = random.Random(f"pattern-context::{timeframe_label}::{side_key}::{self.context_lookback}")
        n_lead = max(0, int(self.context_lookback or 0))
        # BUY: downtrend into signal at support, then rally toward resistance (TP up).
        # SELL: downtrend into signal, then continuation down toward support (TP down).
        self._lead_candles = (
            self._generate_leg(rng, 0.16, 0.80, n_lead, choppiness) if n_lead else []
        )
        if side_key == "SELL":
            self._trail_candles = self._generate_leg(rng, 0.78, 0.92, 5, choppiness)
        else:
            self._trail_candles = self._generate_leg(rng, 0.78, 0.20, 5, choppiness)
        self._context_timeframe = timeframe_label
        self._context_trade_side = side_key

    @staticmethod
    def _generate_leg(rng: random.Random, start_frac: float, end_frac: float,
                       n: int, choppiness: float):
        """n candles trending from start_frac to end_frac (y-fraction
        space; smaller = closer to resistance/top), with per-candle
        noise, body-size variation, and occasional counter-trend
        candles, all scaled by choppiness. Returns a list of
        (center_frac, body_half_frac, is_counter_trend)."""
        legs = []
        for i in range(n):
            t = (i + 1) / (n + 1)
            base = start_frac + (end_frac - start_frac) * t
            noise = rng.uniform(-1.0, 1.0) * choppiness * 2.4
            center = min(0.94, max(0.06, base + noise))
            body_half = 0.025 + choppiness * rng.uniform(1.2, 2.3)
            is_counter = rng.random() < min(0.34, choppiness * 3.4)
            legs.append((center, body_half, is_counter))
        return legs

    def add_overlay(self, draw_fn):
        """Hook for future indicator overlays: draw_fn(painter, plot_rect)."""
        self._overlays.append(draw_fn)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        # ---- panel background: soft gradient + rounded border ----
        panel_path = QPainterPath()
        panel_path.addRoundedRect(0.5, 0.5, w - 1, h - 1, 10, 10)
        bg_gradient = QLinearGradient(0, 0, 0, h)
        bg_gradient.setColorAt(0.0, QColor("#FCFDFD"))
        bg_gradient.setColorAt(1.0, QColor("#F3F5F7"))
        painter.fillPath(panel_path, QBrush(bg_gradient))
        painter.setPen(QPen(QColor("#DDE1E6"), 1))
        painter.drawPath(panel_path)
        painter.setClipPath(panel_path)

        margin_x, margin_top, margin_bottom = 16, 28, 46
        plot_w = w - 2 * margin_x
        plot_top = margin_top
        plot_bottom = h - margin_bottom
        plot_h = plot_bottom - plot_top
        if plot_w <= 60 or plot_h <= 60:
            painter.end()
            return

        # ---- timeframe chip, top-left ----
        header_font = painter.font()
        header_font.setBold(True)
        header_font.setPointSize(9)
        painter.setFont(header_font)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#E6F4EA")))
        chip_w = QFontMetrics(header_font).horizontalAdvance(self.timeframe_label) + 16
        painter.drawRoundedRect(10, 8, chip_w, 18, 5, 5)
        painter.setPen(QPen(QColor("#188038")))
        painter.drawText(18, 21, self.timeframe_label)

        if self.is_doji and self.doji_style:
            style_label = self.doji_style.replace("_", " ").title()
            chip_text = f"Doji · {style_label}"
            style_font = painter.font()
            style_font.setBold(True)
            style_font.setPointSize(8)
            painter.setFont(style_font)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor("#FEF7E0")))
            style_x = 10 + chip_w + 6
            style_chip_w = QFontMetrics(style_font).horizontalAdvance(chip_text) + 14
            painter.drawRoundedRect(style_x, 8, style_chip_w, 18, 5, 5)
            painter.setPen(QPen(QColor("#B06000")))
            painter.drawText(style_x + 7, 21, chip_text)
            trade_x = style_x + style_chip_w + 6
        else:
            trade_x = 10 + chip_w + 6

        side = self.preview_trade_side if self.preview_trade_side in ("BUY", "SELL") else ("BUY" if self.is_green else "SELL")
        if self.is_doji:
            chip2 = side
        elif self.wick_side in ("BODY", "BODY_ONLY"):
            chip2 = f"Body · {side}"
        else:
            variant = "Inv." if self.wick_side == "UPPER" else "Classic"
            chip2 = f"{variant} · {side}"
        side_font = painter.font()
        side_font.setBold(True)
        side_font.setPointSize(8)
        painter.setFont(side_font)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#E8F0FE")))
        chip2_w = QFontMetrics(side_font).horizontalAdvance(chip2) + 14
        painter.drawRoundedRect(trade_x, 8, chip2_w, 18, 5, 5)
        painter.setPen(QPen(QColor("#1A73E8")))
        painter.drawText(trade_x + 7, 21, chip2)

        # ---- faint horizontal grid, purely decorative depth ----
        painter.setPen(QPen(QColor("#ECEEF1"), 1))
        for frac in (0.25, 0.5, 0.75):
            gy = int(plot_top + frac * plot_h)
            painter.drawLine(int(margin_x), gy, int(w - margin_x), gy)

        n_lead, n_trail = len(self._lead_candles), len(self._trail_candles)
        total_candles = max(1, n_lead + 1 + n_trail)
        slot_w = plot_w / total_candles
        candle_w = max(4.0, slot_w * 0.5)

        def y_of(frac):
            return plot_top + frac * plot_h

        support_y = y_of(0.86)
        resistance_y = y_of(0.14)
        support_color = QColor("#188038")
        resistance_color = QColor("#B3261E")

        small_font = painter.font()
        small_font.setBold(True)
        small_font.setPointSize(7)
        painter.setFont(small_font)
        fm_small = QFontMetrics(small_font)

        def dashed_line_with_label(y, color, text, text_above):
            pen = QPen(color, 1, Qt.DashLine)
            pen.setDashPattern([4, 3])
            painter.setPen(pen)
            painter.drawLine(int(margin_x), int(y), int(w - margin_x), int(y))
            text_w = fm_small.horizontalAdvance(text)
            tx = int(w - margin_x - text_w)
            ty = int(y - 5) if text_above else int(y + 12)
            painter.setPen(QPen(color))
            painter.drawText(tx, ty, text)

        dashed_line_with_label(support_y, support_color, "Support", text_above=False)
        dashed_line_with_label(resistance_y, resistance_color, "Resistance", text_above=True)

        # ---- indicator preview lines (conceptual — not live market data) ----
        ind_font = painter.font()
        ind_font.setBold(True)
        ind_font.setPointSize(7)
        painter.setFont(ind_font)
        if self.preview_show_vwap:
            vwap_y = y_of(0.72 if self.preview_price_above_vwap else 0.88)
            vwap_pen = QPen(QColor("#1A73E8"), 1.6, Qt.DashLine)
            vwap_pen.setDashPattern([6, 4])
            painter.setPen(vwap_pen)
            painter.drawLine(int(margin_x), int(vwap_y), int(w - margin_x), int(vwap_y))
            painter.setPen(QPen(QColor("#1A73E8")))
            painter.drawText(int(margin_x + 4), int(vwap_y - 4), "VWAP")

        if self.preview_show_st:
            st_y = y_of(0.82 if self.preview_st_bullish else 0.68)
            st_color = QColor("#188038") if self.preview_st_bullish else QColor("#B3261E")
            st_pen = QPen(st_color, 2.0)
            painter.setPen(st_pen)
            painter.drawLine(int(margin_x), int(st_y), int(w - margin_x), int(st_y))
            painter.setPen(QPen(st_color))
            label = "SuperTrend ▲" if self.preview_st_bullish else "SuperTrend ▼"
            painter.drawText(int(margin_x + 4), int(st_y - 4), label)

        # The signal candle (driven by live parameters) keeps real
        # green/red BUY/SELL colouring. Prior N pattern bars (Context tab)
        # are also green/red. Trail-out bars after the signal stay grey.
        down_color = QColor("#EA4335")
        up_color = QColor("#34A853")
        wick_color = QColor("#8A8F98")
        context_color = QColor("#B0B4BA")
        context_counter_color = QColor("#8F949C")

        def draw_candle(cx, top_y, bot_y, cw, color, wick_extend=5, colored_wick=False):
            wick_pen = QPen(color.darker(145) if colored_wick else wick_color, 1.3)
            wick_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(wick_pen)
            painter.drawLine(int(cx), int(top_y - wick_extend), int(cx), int(bot_y + wick_extend))
            body_gradient = QLinearGradient(0, top_y, 0, bot_y)
            body_gradient.setColorAt(0.0, color.lighter(112))
            body_gradient.setColorAt(1.0, color.darker(105))
            painter.setPen(QPen(color.darker(130), 1))
            painter.setBrush(QBrush(body_gradient))
            painter.drawRoundedRect(cx - cw / 2, top_y, cw, max(2.0, bot_y - top_y), 1.5, 1.5)

        x = margin_x + slot_w / 2
        lead_is_buy = self.preview_trade_side != "SELL"
        for center, body_half, is_counter in self._lead_candles:
            top_y = y_of(max(0.05, center - body_half))
            bot_y = y_of(min(0.95, center + body_half))
            # Pattern bars (prior N) use green/red. Trail after the signal stays grey.
            if lead_is_buy:
                candle_color = up_color if is_counter else down_color
            else:
                candle_color = down_color if is_counter else up_color
            draw_candle(x, top_y, bot_y, candle_w, candle_color, colored_wick=True)
            x += slot_w

        # ---- signal candle: shape + colour driven by live parameters ----
        signal_x = x
        signal_center_y = support_y
        total_pct = self.body_pct + self.dominant_pct + self.small_pct
        total_pct = total_pct if total_pct > 0 else 1.0
        signal_h = plot_h * 0.32
        body_h = signal_h * (self.body_pct / total_pct)
        dominant_h = signal_h * (self.dominant_pct / total_pct)
        small_h = signal_h * (self.small_pct / total_pct)

        long_wick_on_bottom = (self.wick_side != "UPPER")
        if long_wick_on_bottom:
            small_top = signal_center_y - signal_h / 2
            body_top = small_top + small_h
            body_bottom = body_top + body_h
            dominant_bottom = body_bottom + dominant_h
        else:
            dominant_top = signal_center_y - signal_h / 2
            body_top = dominant_top + dominant_h
            body_bottom = body_top + body_h
            small_bottom = body_bottom + small_h

        # ---- key trade levels (screen Y: smaller = higher price / toward resistance)
        is_buy = self.preview_trade_side == "BUY"
        if long_wick_on_bottom:
            candle_low_y = dominant_bottom
            candle_high_y = small_top
        else:
            candle_low_y = small_bottom
            candle_high_y = dominant_top

        hammer_open_y = body_bottom if self.is_green else body_top
        hammer_close_y = body_top if self.is_green else body_bottom
        candle_range_px = max(abs(candle_low_y - candle_high_y), 1.0)
        # $1 offset is a visible slice of the hammer range (preview scale, not live ticks).
        px_per_usd = candle_range_px / 20.0

        next_open_y = hammer_close_y
        next_body = max(10.0, body_h * 0.85)
        if is_buy:
            next_close_y = next_open_y - next_body
        else:
            next_close_y = next_open_y + next_body
        next_high_y = min(next_open_y, next_close_y) - max(4.0, small_h * 0.25)
        next_low_y = max(next_open_y, next_close_y) + max(4.0, small_h * 0.25)

        rule = (self.entry_rule or "NEXT_CANDLE_OPEN").upper()
        use_next_bar = rule in ("NEXT_CANDLE_OPEN", "NEXT_CANDLE_CLOSE")
        if rule == "HAMMER_HIGH":
            entry_y = candle_high_y
            entry_flag = "ENTRY high"
        elif rule == "HAMMER_LOW":
            entry_y = candle_low_y
            entry_flag = "ENTRY low"
        elif rule == "HAMMER_CLOSE":
            entry_y = hammer_close_y
            entry_flag = "ENTRY close"
        elif rule == "NEXT_CANDLE_CLOSE":
            entry_y = next_close_y
            entry_flag = "ENTRY next close"
        else:
            entry_y = next_open_y
            entry_flag = "ENTRY next open"

        entry_y -= self.entry_offset * px_per_usd
        if self.entry_offset:
            entry_flag = f"{entry_flag} {self.entry_offset:+.1f}"

        buf_mode = (self.buffer_mode or "PERCENT_OF_RANGE").upper()
        sl_mode = str(getattr(self, "sl_mode", logic.StopLossMode.CANDLE_EXTREME.value))
        fixed_dist = max(0.0, float(getattr(self, "sl_fixed_distance", 0.0) or 0.0))
        if sl_mode == logic.StopLossMode.FIXED_FROM_ENTRY.value:
            buffer_px = fixed_dist * px_per_usd
            if is_buy:
                sl_y = entry_y + buffer_px
            else:
                sl_y = entry_y - buffer_px
            risk_label_usd = fixed_dist
        else:
            if buf_mode == "NONE":
                buffer_px = 0.0
            elif buf_mode == "FLAT_AMOUNT":
                buffer_px = abs(self.sl_buffer_flat) * px_per_usd
            elif buf_mode == "PERCENT_OF_PRICE":
                buffer_px = candle_range_px * (abs(self.sl_buffer_pct) / 100.0)
            else:
                buffer_px = candle_range_px * (abs(self.sl_buffer_pct) / 100.0)
            if is_buy:
                sl_y = candle_low_y + buffer_px
            else:
                sl_y = candle_high_y - buffer_px
            risk_label_usd = self.max_sl_usd

        risk_y_dist = abs(sl_y - entry_y)
        reward_y_dist = risk_y_dist * max(self.rr_multiple, 0.1)
        if is_buy:
            target_y = max(entry_y - reward_y_dist, resistance_y)
        else:
            target_y = min(entry_y + reward_y_dist, support_y)

        zone_left = signal_x - candle_w * 0.9
        zone_right = signal_x + slot_w * (1.85 if use_next_bar else 0.95)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(242, 153, 0, 32)))
        painter.drawRect(QRectF(zone_left, min(entry_y, sl_y), zone_right - zone_left, abs(sl_y - entry_y)))
        reward_fill = QColor(52, 168, 83, 32) if is_buy else QColor(234, 67, 53, 32)
        painter.setBrush(QBrush(reward_fill))
        painter.drawRect(QRectF(zone_left, min(entry_y, target_y), zone_right - zone_left, abs(target_y - entry_y)))

        # soft radial "spotlight" glow behind the signal candle
        glow_r = candle_w * 2.2
        glow = QRadialGradient(QPointF(signal_x, signal_center_y), glow_r)
        if self.is_green:
            glow.setColorAt(0.0, QColor(24, 128, 56, 70))
        else:
            glow.setColorAt(0.0, QColor(179, 38, 30, 70))
        glow.setColorAt(1.0, QColor(24, 128, 56, 0) if self.is_green else QColor(179, 38, 30, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QPointF(signal_x, signal_center_y), glow_r, signal_h * 0.85)

        body_color = up_color if self.is_green else down_color
        if self.is_doji and self.doji_style in ("ANY", "CLASSIC", "LONG_LEGGED"):
            body_color = QColor("#5F6368")

        signal_wick_pen = QPen(wick_color, 2)
        signal_wick_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(signal_wick_pen)
        if long_wick_on_bottom:
            painter.drawLine(int(signal_x), int(small_top), int(signal_x), int(body_top))
            painter.drawLine(int(signal_x), int(body_bottom), int(signal_x), int(dominant_bottom))
        else:
            painter.drawLine(int(signal_x), int(dominant_top), int(signal_x), int(body_top))
            painter.drawLine(int(signal_x), int(body_bottom), int(signal_x), int(small_bottom))

        body_gradient = QLinearGradient(0, body_top, 0, body_bottom)
        body_gradient.setColorAt(0.0, body_color.lighter(115))
        body_gradient.setColorAt(1.0, body_color.darker(105))
        border_color = QColor("#188038") if self.is_green else QColor("#B3261E")
        if self.is_doji and self.doji_style in ("ANY", "CLASSIC", "LONG_LEGGED"):
            border_color = QColor("#5F6368")
        painter.setPen(QPen(border_color, 2))
        painter.setBrush(QBrush(body_gradient))
        painter.drawRoundedRect(signal_x - candle_w / 2, body_top,
                                 candle_w, max(2.0, body_bottom - body_top), 2, 2)

        next_x = signal_x + slot_w
        if use_next_bar:
            # Entry bar is not part of the pattern. Keep it grey so the
            # coloured count matches Context N (plus the signal candle).
            next_color = context_color
            next_wick = QPen(wick_color, 1.6)
            next_wick.setCapStyle(Qt.RoundCap)
            painter.setPen(next_wick)
            painter.drawLine(int(next_x), int(next_high_y), int(next_x), int(next_low_y))
            nb_top = min(next_open_y, next_close_y)
            nb_bot = max(next_open_y, next_close_y)
            next_grad = QLinearGradient(0, nb_top, 0, nb_bot)
            next_grad.setColorAt(0.0, next_color.lighter(115))
            next_grad.setColorAt(1.0, next_color.darker(105))
            painter.setPen(QPen(next_color.darker(120), 1.5))
            painter.setBrush(QBrush(next_grad))
            painter.drawRoundedRect(next_x - candle_w / 2, nb_top,
                                     candle_w, max(2.0, nb_bot - nb_top), 2, 2)

        # ---- ENTRY / SL / TP flags (same rules as logic.py Entry / Exit)
        entry_color = QColor("#1A73E8")
        sl_color = QColor("#F29900")
        tp_color = up_color if is_buy else down_color

        flag_font = painter.font()
        flag_font.setBold(True)
        flag_font.setPointSize(7)
        fm_flag = QFontMetrics(flag_font)

        def draw_flag(fy, color, text):
            line_pen = QPen(color, 1.4, Qt.DashLine)
            line_pen.setDashPattern([3, 2])
            painter.setPen(line_pen)
            painter.drawLine(int(zone_left), int(fy), int(zone_right), int(fy))

            painter.setFont(flag_font)
            text_w = fm_flag.horizontalAdvance(text) + 12
            flag_x = zone_right + 3
            flag_y = fy - 8
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(flag_x, flag_y, text_w, 16, 4, 4)
            painter.setPen(QPen(QColor("#FFFFFF")))
            painter.drawText(int(flag_x + 6), int(flag_y + 12), text)

        draw_flag(target_y, tp_color, "TP")
        draw_flag(entry_y, entry_color, entry_flag)
        draw_flag(sl_y, sl_color, "SL")

        reward_usd = risk_label_usd * self.rr_multiple
        zone_label_font = painter.font()
        zone_label_font.setBold(True)
        zone_label_font.setPointSize(7)
        painter.setFont(zone_label_font)
        risk_mid_y = (entry_y + sl_y) / 2
        reward_mid_y = (entry_y + target_y) / 2
        painter.setPen(QPen(sl_color.darker(115)))
        painter.drawText(int(zone_left + 5), int(risk_mid_y + 3), f"-${risk_label_usd:,.0f}")
        painter.setPen(QPen(tp_color.darker(120)))
        painter.drawText(int(zone_left + 5), int(reward_mid_y + 3), f"+${reward_usd:,.0f}")

        action_label = "BUY" if is_buy else "SELL"
        action_color = support_color if is_buy else resistance_color
        bold_small = painter.font()
        bold_small.setBold(True)
        bold_small.setPointSize(8)
        painter.setFont(bold_small)
        fm_bold = QFontMetrics(bold_small)
        badge_w = fm_bold.horizontalAdvance(action_label) + 14
        badge_x = signal_x - badge_w / 2
        badge_y = plot_bottom + 6
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(action_color))
        painter.drawRoundedRect(badge_x, badge_y, badge_w, 16, 5, 5)
        painter.setPen(QPen(QColor("#FFFFFF")))
        painter.drawText(int(badge_x + 7), int(badge_y + 12), action_label)

        x += slot_w
        trail = self._trail_candles
        if use_next_bar:
            x += slot_w
            trail = self._trail_candles[1:]
        for center, body_half, is_counter in trail:
            top_y = y_of(max(0.05, center - body_half))
            bot_y = y_of(min(0.95, center + body_half))
            candle_color = context_counter_color if is_counter else context_color
            draw_candle(x, top_y, bot_y, candle_w, candle_color)
            x += slot_w

        # ---- profit target label, right-aligned above resistance ----
        painter.setFont(bold_small)
        target_text = "PROFIT TARGET"
        target_w = fm_bold.horizontalAdvance(target_text)
        if is_buy:
            painter.setPen(QPen(support_color))
            painter.drawText(int(w - margin_x - target_w), int(resistance_y - 22), target_text)
        else:
            painter.setPen(QPen(resistance_color))
            painter.drawText(int(w - margin_x - target_w), int(support_y + 14), target_text)

        # ---- RR / SL caption, pill badge bottom-left -- now also
        # showing the breakeven win rate: the minimum win % this RR
        # needs just to not lose money, a number traders actually think
        # in terms of, not just the raw ratio.
        painter.setFont(small_font)
        breakeven_pct = 100.0 / (1.0 + self.rr_multiple) if self.rr_multiple > -1 else 0.0
        caption = (f"RR {self.rr_multiple:.1f}  |  Max SL ${self.max_sl_usd:,.0f}"
                   f"  |  Breakeven {breakeven_pct:.0f}% win rate")
        caption_w = QFontMetrics(small_font).horizontalAdvance(caption) + 16
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#EDEFF2")))
        painter.drawRoundedRect(margin_x - 2, h - 24, caption_w, 17, 5, 5)
        painter.setPen(QPen(QColor("#5F6368")))
        painter.drawText(int(margin_x + 5), int(h - 12), caption)

        # ---- legend, bottom-right: what each flag colour means ----
        legend_font = painter.font()
        legend_font.setBold(False)
        legend_font.setPointSize(7)
        painter.setFont(legend_font)
        fm_legend = QFontMetrics(legend_font)
        legend_items = [("Entry", entry_color), ("SL", sl_color), ("TP", tp_color)]
        legend_y = h - 16
        lx = w - margin_x
        for label, color in reversed(legend_items):
            text_w = fm_legend.horizontalAdvance(label)
            lx -= text_w
            painter.setPen(QPen(QColor("#5F6368")))
            painter.drawText(int(lx), int(legend_y), label)
            lx -= 12
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(lx, legend_y - 3), 3.5, 3.5)
            lx -= 12

        for overlay in self._overlays:
            try:
                overlay(painter, (margin_x, plot_top, plot_w, plot_h))
            except Exception:
                pass

        painter.end()



class BacktestWorker(QObject):
    finished = Signal(dict, str, str)   # tables, output_dir, plots_dir
    failed = Signal(str, str)            # error_msg, traceback_str

    def __init__(self, backtest_config: backtest.BacktestConfig):
        super().__init__()
        self.backtest_config = backtest_config

    def run(self):
        try:
            tables = backtest.run_backtest_and_export(self.backtest_config)

            plots_dir = os.path.join(DEFAULT_PLOTS_DIR, self.backtest_config.run_name)
            plot_config = plotting.PlottingConfig(
                backtest_output_dir=os.path.join(
                    self.backtest_config.output_root, self.backtest_config.run_name),
                output_dir=plots_dir,
                data_root=getattr(self.backtest_config, "data_root", None),
                symbol=getattr(self.backtest_config, "symbol", None),
                timeframes=list(getattr(self.backtest_config, "timeframes_to_test", None) or []),
                market_data=(
                    getattr(self.backtest_config.market_data, "value", None)
                    if getattr(self.backtest_config, "market_data", None) is not None
                    else None
                ),
                start_date=getattr(self.backtest_config, "start_date", None),
                end_date=getattr(self.backtest_config, "end_date", None),
            )
            plotting.generate_all_plots(plot_config)
            plotting.generate_yearly_summary(plot_config)

            output_dir = os.path.join(self.backtest_config.output_root, self.backtest_config.run_name)
            self.finished.emit(tables, output_dir, plots_dir)
        except Exception as e:
            tb = traceback.format_exc()
            self.failed.emit(str(e), tb)


class FetchDataWorker(QThread):
    """Background MT5/history fetch so the UI stays responsive."""

    log_line = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, job: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._job = job
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            result = data_fetcher.run_fetch_job(
                output_root=self._job["output_root"],
                symbols=self._job["symbols"],
                timeframes=self._job["timeframes"],
                update_existing=self._job["update_existing"],
                start_date=self._job.get("start_date"),
                use_mock=bool(self._job.get("use_mock", False)),
                terminal_path=self._job.get("terminal_path") or "",
                login=int(self._job.get("login") or 0),
                password=self._job.get("password") or "",
                server=self._job.get("server") or "",
                market_type=self._job.get("market_type"),
                auto_detect_market=bool(self._job.get("auto_detect_market", True)),
                mt5_module=self._job.get("mt5_module"),
                own_connection=self._job.get("own_connection"),
                log=lambda msg: self.log_line.emit(str(msg)),
                should_stop=lambda: self._stop,
            )
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class FetchDataDialog(QDialog):
    """Fetch / update historical CSVs via fetch.py (menu bar → Fetch)."""

    def __init__(self, dashboard: "BacktestDashboard"):
        super().__init__(dashboard)
        self._dash = dashboard
        self._worker: Optional[FetchDataWorker] = None
        self.setWindowTitle("Fetch market data")
        self.setMinimumSize(640, 520)
        self.resize(720, 580)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Pulls candles from the <b>same MT5 terminal as Live</b> "
            "(Live Settings path / login / server — never a second connection). "
            "Tip: <b>Live → Connect MT5</b> first, or open the symbol on a chart in MT5 "
            "so history is loaded. "
            "<b>Spot</b> and <b>futures</b> are stored separately "
            "(<code>data/spot/…</code> and <code>data/futures/…</code>). "
            "Update mode only downloads newer bars and merges them."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        intro.setObjectName("sectionHint")
        root.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        path_row = QHBoxLayout()
        self.data_root_edit = QLineEdit(self._detected_data_root())
        browse = QPushButton("Browse…")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._browse_data_root)
        path_row.addWidget(self.data_root_edit, 1)
        path_row.addWidget(browse)
        form.addRow("Data folder", path_row)

        self.symbol_edit = QLineEdit(self._detected_symbol())
        self.symbol_edit.editingFinished.connect(self._on_symbol_or_market_changed)
        form.addRow("Symbol", self.symbol_edit)

        self.market_spot = QRadioButton("Spot")
        self.market_futures = QRadioButton("Futures")
        self.market_auto = QRadioButton("Auto-detect (MT5 / name)")
        self.market_auto.setChecked(True)
        self.market_group = QButtonGroup(self)
        self.market_group.addButton(self.market_spot)
        self.market_group.addButton(self.market_futures)
        self.market_group.addButton(self.market_auto)
        market_row = QHBoxLayout()
        market_row.addWidget(self.market_auto)
        market_row.addWidget(self.market_spot)
        market_row.addWidget(self.market_futures)
        market_row.addStretch()
        form.addRow("Market type", market_row)
        self.market_guess_label = QLabel("")
        self.market_guess_label.setObjectName("sectionHint")
        form.addRow("", self.market_guess_label)
        self.market_spot.toggled.connect(self._on_symbol_or_market_changed)
        self.market_futures.toggled.connect(self._on_symbol_or_market_changed)
        self.market_auto.toggled.connect(self._on_symbol_or_market_changed)

        self.mt5_conn_label = QLabel("")
        self.mt5_conn_label.setWordWrap(True)
        self.mt5_conn_label.setObjectName("sectionHint")
        self.mt5_conn_label.setTextFormat(Qt.RichText)
        form.addRow("MT5 connection", self.mt5_conn_label)

        self.mode_update = QRadioButton("Update existing (recommended)")
        self.mode_update.setChecked(True)
        self.mode_update.setToolTip(
            "Detect the latest bar on disk per timeframe and only fetch newer data."
        )
        self.mode_full = QRadioButton("Full re-fetch from start year")
        self.mode_full.setToolTip("Overwrite monthly files from the start year through today.")
        mode_box = QVBoxLayout()
        mode_box.addWidget(self.mode_update)
        mode_box.addWidget(self.mode_full)
        form.addRow("Mode", mode_box)

        self.start_year_edit = QLineEdit("2020")
        self.start_year_edit.setMaximumWidth(80)
        self.start_year_edit.setToolTip("Used when no local data exists, or for full re-fetch.")
        form.addRow("Start year", self.start_year_edit)

        root.addLayout(form)

        tf_box = QGroupBox("Timeframes")
        tf_layout = QHBoxLayout(tf_box)
        self.tf_checks: Dict[str, QCheckBox] = {}
        for folder in data_fetcher.TIMEFRAMES.keys():
            cb = QCheckBox(folder)
            cb.setChecked(True)
            self.tf_checks[folder] = cb
            tf_layout.addWidget(cb)
        tf_layout.addStretch()
        root.addWidget(tf_box)

        detect_row = QHBoxLayout()
        self.detect_label = QLabel("")
        self.detect_label.setObjectName("sectionHint")
        self.detect_label.setWordWrap(True)
        detect_btn = QPushButton("Scan data folder")
        detect_btn.setObjectName("secondaryButton")
        detect_btn.clicked.connect(self._refresh_detect_summary)
        detect_row.addWidget(detect_btn)
        detect_row.addWidget(self.detect_label, 1)
        root.addLayout(detect_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("liveLogConsole")
        self.log_view.setMinimumHeight(180)
        root.addWidget(self.log_view, 1)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start fetch")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.clicked.connect(self._start_fetch)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("secondaryButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_fetch)
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("secondaryButton")
        open_folder.clicked.connect(self._open_data_folder)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("secondaryButton")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(open_folder)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        self._on_symbol_or_market_changed()
        self._refresh_mt5_connection_label()

    def _detected_data_root(self) -> str:
        d = self._dash
        w = d.field_widgets.get("data_root") if hasattr(d, "field_widgets") else None
        if isinstance(w, QLineEdit) and w.text().strip():
            path = backtest.resolve_data_root(w.text().strip(), anchor_dir=APP_DIR)
            # Peel …/spot or …/futures so the dialog always shows the parent data root
            if os.path.basename(path).lower() in data_fetcher.MARKET_TYPES:
                path = os.path.dirname(path) or path
            return path
        return DEFAULT_DATA_DIR

    def _detected_symbol(self) -> str:
        d = self._dash
        w = d.field_widgets.get("symbol") if hasattr(d, "field_widgets") else None
        if isinstance(w, QLineEdit) and w.text().strip():
            return w.text().strip()
        return "XAUUSD"

    def _selected_market_type(self) -> Optional[str]:
        if self.market_spot.isChecked():
            return data_fetcher.MARKET_SPOT
        if self.market_futures.isChecked():
            return data_fetcher.MARKET_FUTURES
        return None  # auto

    def _on_symbol_or_market_changed(self, *_args):
        sym = self.symbol_edit.text().strip() or "XAUUSD"
        guessed = data_fetcher.classify_market_type(sym)
        selected = self._selected_market_type()
        if selected is None:
            self.market_guess_label.setText(
                f"Name guess for <b>{sym}</b>: <b>{guessed.upper()}</b> "
                f"(MT5 will refine this when connected)."
            )
        else:
            note = ""
            if guessed != selected:
                note = f" — name looks like <b>{guessed}</b>, but will save as <b>{selected}</b>"
            self.market_guess_label.setText(
                f"Saving <b>{sym}</b> under <b>{selected.upper()}</b>{note}."
            )
        self.market_guess_label.setTextFormat(Qt.RichText)
        self._refresh_detect_summary()

    def _browse_data_root(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select data folder", self.data_root_edit.text().strip() or DEFAULT_DATA_DIR,
        )
        if path:
            self.data_root_edit.setText(path)
            self._refresh_detect_summary()

    def _open_data_folder(self):
        path = self.data_root_edit.text().strip() or DEFAULT_DATA_DIR
        mt = self._selected_market_type() or data_fetcher.classify_market_type(
            self.symbol_edit.text().strip() or "XAUUSD"
        )
        target = data_fetcher.market_data_root(path, mt)
        os.makedirs(target, exist_ok=True)
        self._dash._open_folder(target)

    def _refresh_detect_summary(self):
        root = self.data_root_edit.text().strip() or DEFAULT_DATA_DIR
        root = backtest.resolve_data_root(root, anchor_dir=APP_DIR)
        if os.path.basename(root).lower() in data_fetcher.MARKET_TYPES:
            root = os.path.dirname(root) or root
        self.data_root_edit.setText(root)
        sym = self.symbol_edit.text().strip() or "XAUUSD"
        if not os.path.isdir(root):
            self.detect_label.setText(f"Folder does not exist yet — will be created: {root}")
            return
        inv = data_fetcher.detect_market_inventory(root)
        bits = [
            f"spot: {', '.join(inv['spot']) or '—'} | "
            f"futures: {', '.join(inv['futures']) or '—'} | "
            f"legacy: {', '.join(inv['legacy_spot']) or '—'}"
        ]
        mt = self._selected_market_type() or data_fetcher.classify_market_type(sym)
        write_root = data_fetcher.resolve_write_root(root, sym, mt)
        for tf in ("1hour", "15min", "1min"):
            latest = data_fetcher.detect_latest_bar(sym, tf, output_root=write_root)
            if latest is not None:
                bits.append(f"{mt}/{sym}/{tf} → {latest}")
        self.detect_label.setText(" · ".join(bits[:6]) + (" …" if len(bits) > 6 else ""))

    def _append_log(self, line: str):
        self.log_view.appendPlainText(line)
        bar = self.log_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def _refresh_mt5_connection_label(self) -> None:
        d = self._dash
        creds = d._live_broker_credentials() if hasattr(d, "_live_broker_credentials") else None
        path = (creds.terminal_path if creds else "") or "(nearest running MT5)"
        login = creds.login if creds else 0
        server = (creds.server if creds else "") or "—"
        broker = getattr(d, "mt5_broker", None)
        if broker is not None and broker.is_connected:
            info = broker.account_info_dict()
            self.mt5_conn_label.setText(
                f"<b>Reusing Live connection</b> — account {info.get('login', '?')} "
                f"@ {info.get('server', '?')}<br>"
                f"Terminal setting: {path} · will <b>not</b> shut down after fetch"
            )
        else:
            login_txt = str(login) if login else "(already logged in)"
            self.mt5_conn_label.setText(
                f"Will connect with <b>Live Settings</b> "
                f"(same as Connect MT5)<br>"
                f"Terminal: {path} · Login: {login_txt} · Server: {server}"
            )

    def _start_fetch(self):
        if self._worker is not None and self._worker.isRunning():
            return
        d = self._dash
        # Never fight Live for the process-global MetaTrader5 connection
        if hasattr(d, "_live_worker_running") and d._live_worker_running():
            QMessageBox.warning(
                self,
                "Live is running",
                "Stop live trading before fetching history.\n\n"
                "MT5 allows only one Python connection — Fetch and Live must "
                "not compete for the same terminal.",
            )
            return

        root = self.data_root_edit.text().strip() or DEFAULT_DATA_DIR
        root = backtest.resolve_data_root(root, anchor_dir=APP_DIR)
        if os.path.basename(root).lower() in data_fetcher.MARKET_TYPES:
            root = os.path.dirname(root) or root
        symbol = self.symbol_edit.text().strip() or "XAUUSD"
        tfs = [tf for tf, cb in self.tf_checks.items() if cb.isChecked()]
        if not tfs:
            QMessageBox.warning(self, "Fetch", "Select at least one timeframe.")
            return
        try:
            year = int(self.start_year_edit.text().strip() or "2020")
            year = max(1990, min(datetime.now().year, year))
        except ValueError:
            year = 2020
        start_date = datetime(year, 1, 1, tzinfo=timezone.utc)
        update = self.mode_update.isChecked()
        market = self._selected_market_type()
        creds = d._live_broker_credentials()

        shared_mt5 = None
        own_connection = True
        broker = getattr(d, "mt5_broker", None)
        if broker is not None and broker.is_connected and broker.raw_mt5 is not None:
            shared_mt5 = broker.raw_mt5
            own_connection = False

        job = {
            "output_root": root,
            "symbols": [symbol],
            "timeframes": tfs,
            "update_existing": update,
            "start_date": start_date,
            "terminal_path": creds.terminal_path,
            "login": creds.login,
            "password": creds.password,
            "server": creds.server,
            "use_mock": False,
            "market_type": market,
            "auto_detect_market": True,
            "mt5_module": shared_mt5,
            "own_connection": own_connection,
        }
        self.log_view.clear()
        mt_label = market or "auto-detect"
        if shared_mt5 is not None:
            self._append_log(f"Starting fetch ({mt_label}) via shared Live MT5 connection…")
        else:
            self._append_log(
                f"Starting fetch ({mt_label}) using Live Settings credentials "
                f"(single connection; will close when done)…"
            )
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._worker = FetchDataWorker(job, self)
        self._worker.log_line.connect(self._append_log)
        self._worker.finished_ok.connect(self._on_fetch_ok)
        self._worker.failed.connect(self._on_fetch_failed)
        self._worker.finished.connect(self._on_fetch_thread_finished)
        self._worker.start()

    def _stop_fetch(self):
        if self._worker is not None:
            self._worker.request_stop()
            self._append_log("[STOP] Stop requested — finishing current chunk…")

    def _on_fetch_ok(self, result: dict):
        total = result.get("total_saved", 0)
        up_to_date = int(result.get("up_to_date_count") or 0)
        market = result.get("market_type") or "spot"
        write_root = result.get("write_root") or result.get("output_root") or ""
        self._append_log(
            f"Fetch finished ({market}). Candles written: {total} → {write_root}"
        )
        self._refresh_detect_summary()
        # Point Parameters → data_root at the market folder used (spot or futures)
        w = self._dash.field_widgets.get("data_root") if hasattr(self._dash, "field_widgets") else None
        if isinstance(w, QLineEdit) and write_root:
            w.setText(write_root)
        if total == 0 and up_to_date > 0:
            detail = (
                f"Done — data already current ({up_to_date} timeframe(s) up to date).\n\n"
                f"Market: {market}\nFolder: {write_root}\n\n"
                "To force a full download, choose "
                "'Full re-fetch from start year' and run again."
            )
        elif total == 0:
            detail = (
                f"Done — 0 candles written.\n\n"
                f"Market: {market}\nFolder: {write_root}\n\n"
                "Try:\n"
                "• Use the exact symbol from MT5 Market Watch (e.g. XAUUSDm)\n"
                "• Open that symbol on a chart in MT5 and wait for history\n"
                "• Live → Connect MT5 first, then Fetch again\n"
                "• 'Full re-fetch from start year' instead of Update\n"
                "• Uncheck 3min/10min if your broker does not offer them"
            )
        else:
            detail = (
                f"Done.\n\nMarket: {market}\nCandles written: {total}\n\n"
                f"Backtest data folder set to:\n{write_root}"
            )
        QMessageBox.information(self, "Fetch complete", detail)

    def _on_fetch_failed(self, detail: str):
        self._append_log(detail)
        QMessageBox.critical(
            self, "Fetch failed",
            "Could not fetch data. On Windows, open MT5 and log in first.\n\n"
            f"{detail[:800]}",
        )

    def _on_fetch_thread_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._worker = None

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(3000)
        super().closeEvent(event)


class LiveNotificationsDialog(QDialog):
    """Telegram alerts — opened from menu bar Notifications."""

    def __init__(self, dashboard: "BacktestDashboard"):
        super().__init__(dashboard)
        self._dash = dashboard
        self.setWindowTitle("Telegram notifications")
        self.setMinimumSize(500, 340)
        self.resize(540, 380)

        d = dashboard
        root = QVBoxLayout(self)
        intro = QLabel(
            "Get a Telegram message when the bot places an order, runs dry run, "
            "fails an order, or hits a safety block. Retries automatically if Telegram is slow."
        )
        intro.setWordWrap(True)
        intro.setObjectName("sectionHint")
        root.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        widgets = d._live_notifications_field_widgets()
        for w in widgets:
            w.blockSignals(True)
        try:
            d.live_telegram_enabled.setText("Send Telegram alerts for all order events")
            form.addRow(d.live_telegram_enabled)
            form.addRow("Bot token", d.live_telegram_token)
            form.addRow("Chat ID", d.live_telegram_chat_id)
        finally:
            for w in widgets:
                w.blockSignals(False)

        if not hasattr(d, "live_telegram_status"):
            d.live_telegram_status = QLabel("Off")
        d.live_telegram_status.setObjectName("liveNotifyStatus")
        d._refresh_live_telegram_status()
        root.addWidget(d.live_telegram_status)

        self._test_btn = QPushButton("Send test notification")
        self._test_btn.setObjectName("secondaryButton")
        self._test_btn.setToolTip("Verify bot token and chat id (with retries).")
        self._test_btn.clicked.connect(self._on_test_clicked)
        root.addWidget(self._test_btn)

        hint = QLabel(
            "Create a bot with <b>@BotFather</b>, paste the token, then message your bot once. "
            "Get your numeric chat id from <b>@userinfobot</b>."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        hint.setObjectName("sectionHint")
        root.addWidget(hint)
        root.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self._on_cancel)
        root.addWidget(buttons)

    def _on_test_clicked(self):
        self._dash._test_telegram_notification(test_button=self._test_btn)

    def _release_widgets(self):
        self._dash._stash_live_notifications_widgets()

    def _on_cancel(self):
        self._dash._cancel_telegram_test_worker()
        self._release_widgets()
        self.reject()

    def _on_save(self):
        self._dash._save_telegram_settings()
        self._dash._apply_live_telegram_settings()
        self._dash._refresh_live_telegram_status()
        self._dash._live_log("Telegram notification settings saved.")
        self._release_widgets()
        self.accept()

    def closeEvent(self, event):
        self._dash._cancel_telegram_test_worker()
        self._release_widgets()
        super().closeEvent(event)


class LiveSettingsDialog(QDialog):
    """All Live tab configuration: MT5, execution, order type, safety, performance."""

    def __init__(self, dashboard: "BacktestDashboard"):
        super().__init__(dashboard)
        self._dash = dashboard
        self.setWindowTitle("Live trading settings")
        self.setMinimumSize(640, 520)
        self.resize(720, 580)

        root = QVBoxLayout(self)
        intro = QLabel(
            "<b>Strategy rules</b> (pattern, RR, SL, indicators) come from <b>Backtest Parameters</b> "
            "or a loaded preset — not from this dialog.<br>"
            "Here you set MT5, how orders are sent, and risk limits. "
            "<b>Dry run</b> is on the main Live panel. Click <b>Save</b> when done."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        intro.setObjectName("sectionHint")
        root.addWidget(intro)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        d = dashboard

        # —— Strategy (read-only + shortcuts) ——
        strat_tab = QWidget()
        strat_layout = QVBoxLayout(strat_tab)
        if not hasattr(d, "live_settings_strategy_info"):
            d.live_settings_strategy_info = QLabel("Loading…")
            d.live_settings_strategy_info.setWordWrap(True)
            d.live_settings_strategy_info.setTextFormat(Qt.RichText)
            d.live_settings_strategy_info.setObjectName("liveSummaryCard")
        strat_layout.addWidget(d.live_settings_strategy_info)
        strat_btn_row = QHBoxLayout()
        refresh_strat = QPushButton("Refresh strategy summary")
        refresh_strat.setObjectName("secondaryButton")
        refresh_strat.clicked.connect(d._refresh_live_settings_strategy_tab)
        sync_exec = QPushButton("Sync symbol & timeframe from backtest")
        sync_exec.setObjectName("secondaryButton")
        sync_exec.setToolTip("Sets Live symbol from Run Settings and timeframe from the first included backtest TF.")
        sync_exec.clicked.connect(d._sync_live_execution_from_backtest)
        show_params = QPushButton("Show Backtest Parameters")
        show_params.setObjectName("secondaryButton")
        show_params.clicked.connect(d._focus_backtest_params_dock)
        strat_btn_row.addWidget(refresh_strat)
        strat_btn_row.addWidget(sync_exec)
        strat_btn_row.addWidget(show_params)
        strat_btn_row.addStretch()
        strat_layout.addLayout(strat_btn_row)
        strat_note = QLabel(
            "Live uses the <b>current Parameters panel</b> at Start (pattern, shape, entry/exit, "
            "timeframe RR/SL, indicators) — same objects as backtest Run. "
            "Adjust parameters there, optionally save/load a preset, then Start live."
        )
        strat_note.setWordWrap(True)
        strat_note.setObjectName("sectionHint")
        strat_layout.addWidget(strat_note)
        strat_layout.addStretch()
        tabs.addTab(strat_tab, "Strategy (read-only)")

        util_row = QHBoxLayout()
        open_journal = QPushButton("Open live log folder")
        open_journal.setObjectName("secondaryButton")
        open_journal.setToolTip(f"Opens {LIVE_JOURNAL_DIR} (session log + live_trades.csv)")
        open_journal.clicked.connect(d._open_live_journal_folder)
        util_row.addWidget(open_journal)
        util_row.addStretch()

        # —— MT5 ——
        conn_tab = QWidget()
        conn_form = QFormLayout(conn_tab)
        conn_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        conn_form.addRow("Terminal (.exe)", d.live_mt5_path)
        conn_form.addRow("Login", d.live_login)
        conn_form.addRow("Password", d.live_password)
        conn_form.addRow("Server", d.live_server)
        hint = QLabel("Leave terminal path empty if MT5 is already running and logged in.")
        hint.setWordWrap(True)
        hint.setObjectName("sectionHint")
        conn_form.addRow(hint)
        tabs.addTab(conn_tab, "MT5 connection")

        # —— Execution ——
        exec_tab = QWidget()
        exec_form = QFormLayout(exec_tab)
        exec_form.addRow("Symbol", d.live_symbol)
        exec_form.addRow("Timeframe", d.live_timeframe)
        exec_form.addRow("Lot size", d.live_volume)
        exec_form.addRow("Magic number", d.live_magic)
        exec_form.addRow("Max open positions", d.live_max_positions)
        exec_form.addRow("Poll interval (sec)", d.live_poll_sec)
        exec_form.addRow("History bars (signal)", d.live_history_bars)
        tabs.addTab(exec_tab, "Execution")

        # —— Orders ——
        order_tab = QWidget()
        order_form = QFormLayout(order_tab)
        order_form.addRow("Order type", d.live_order_mode)
        off_hint = QLabel(
            "Offset applies in points (symbol point size). "
            "Buy limit: entry − offset · Sell limit: entry + offset."
        )
        off_hint.setWordWrap(True)
        off_hint.setObjectName("sectionHint")
        order_form.addRow("Limit offset (points)", d.live_limit_offset)
        order_form.addRow(off_hint)
        order_form.addRow("Max entry vs market (points)", d.live_max_entry_deviation)
        order_form.addRow(d.live_limit_offset_from_market)
        order_form.addRow("Max slippage / deviation (pts)", d.live_deviation)
        order_form.addRow("Order comment", d.live_order_comment)
        order_form.addRow(d.live_fallback_market)
        order_form.addRow(d.live_warn_tf_mismatch)
        order_note = QLabel(
            "<b>Recommended:</b> Market for bar-close strategies (signal → enter on new bar).<br>"
            "<b>Limit</b> pending orders must be on the correct side of bid/ask or MT5 rejects them.<br>"
            "Enable Algo Trading in the MT5 toolbar before live orders."
        )
        order_note.setWordWrap(True)
        order_note.setTextFormat(Qt.RichText)
        order_note.setObjectName("sectionHint")
        order_form.addRow(order_note)
        tabs.addTab(order_tab, "Order type")

        # —— Safety ——
        safety_tab = QWidget()
        safety_form = QFormLayout(safety_tab)
        safety_form.addRow(d.live_demo_only)
        safety_form.addRow("Max trades / day", d.live_max_daily_trades)
        safety_form.addRow("Max daily loss ($)", d.live_max_daily_loss)
        safety_form.addRow("Cooldown (minutes)", d.live_min_minutes_between)
        safety_form.addRow("Max spread (points)", d.live_max_spread)
        safety_form.addRow("Max lot cap", d.live_max_lot_cap)
        cap_h = QLabel("Lot cap 0 = only use lot size above.")
        cap_h.setObjectName("sectionHint")
        safety_form.addRow(cap_h)
        testing_btn = QPushButton("Apply relaxed limits for demo testing")
        testing_btn.setObjectName("secondaryButton")
        testing_btn.setToolTip("Sets spread 150 pts, cooldown 0 min — useful while validating signals.")
        testing_btn.clicked.connect(d._apply_live_demo_testing_limits)
        safety_form.addRow(testing_btn)
        tabs.addTab(safety_tab, "Safety")

        # —— Advanced ——
        adv_tab = QWidget()
        adv_layout = QVBoxLayout(adv_tab)
        if not hasattr(d, "live_adv_thread_pool"):
            d.live_adv_thread_pool = QCheckBox("Thread pool for signal CPU (recommended)")
            d.live_adv_ray = QCheckBox("Use Ray if installed (optional)")
        d.live_adv_thread_pool.setChecked(d._live_thread_pool_checked())
        d.live_adv_ray.setChecked(d._live_ray_checked())
        adv_layout.addWidget(d.live_adv_thread_pool)
        adv_layout.addWidget(d.live_adv_ray)
        adv_layout.addStretch()
        tabs.addTab(adv_tab, "Advanced")

        root.addLayout(util_row)

        d._refresh_live_settings_strategy_tab()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self._on_cancel)
        root.addWidget(buttons)

    def _release_widgets(self):
        self._dash._stash_live_settings_widgets()

    def _on_cancel(self):
        self._release_widgets()
        self.reject()

    def _on_save(self):
        dash = self._dash
        if hasattr(dash, "_live_action_thread_pool"):
            dash._live_action_thread_pool.setChecked(dash.live_adv_thread_pool.isChecked())
        if hasattr(dash, "_live_action_ray"):
            dash._live_action_ray.setChecked(dash.live_adv_ray.isChecked())
        dash._save_live_settings()
        dash._refresh_live_config_summary()
        dash._refresh_live_strategy_summary()
        if dash._live_worker_running():
            dash._live_log(
                "[SETTINGS] Symbol or execution changed — Stop live and Start again for orders to use the new symbol."
            )
        dash._live_log("Live settings saved.")
        self._release_widgets()
        self.accept()

    def closeEvent(self, event):
        self._release_widgets()
        super().closeEvent(event)


class LiveLogBridge(QObject):
    """Thread-safe bridge: live worker calls log(); UI appends on the main thread."""

    line_ready = Signal(str)


class LiveTradingThread(QThread):
    """Runs the MT5 poll loop off the UI thread so the dashboard stays responsive."""

    finished_cleanly = Signal()
    crashed = Signal(str)

    def __init__(self, engine: live_trading.LiveTradingEngine):
        super().__init__()
        self.engine = engine

    def run(self):
        try:
            self.engine.run()
        except Exception as e:
            self.crashed.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            self.finished_cleanly.emit()

    def request_stop(self):
        self.engine.request_stop()


# ============================================================================
# MAIN WINDOW
# ============================================================================
class BacktestDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        try:
            from version import __version__ as _app_ver
            self.setWindowTitle(f"Hammer · XAUUSD Backtest & Live  v{_app_ver}")
        except Exception:
            self.setWindowTitle("Hammer · XAUUSD Backtest & Live")

        # QSettings persists window geometry + dock layout across runs
        # (client request: "make sure it saves the state where it is
        # saved"). Uses the platform-native store (plist on macOS, ini
        # elsewhere) -- no file for us to manage.
        self._settings = QSettings("HammerDashboard", "BacktestDashboard")

        # A previous crash report showed Qt trying to restore a window
        # position from an old multi-monitor session that was now
        # off-screen ("Window position ... outside any known screen").
        # Always start from a safe, clamped, on-screen geometry first;
        # a saved geometry (if any) is only applied afterward, and only
        # if it's still valid for the current screen setup.
        self.setMinimumSize(1200, 800)
        self._apply_safe_startup_geometry()

        self.field_widgets: Dict[str, QWidget] = {}
        self.field_labels: Dict[str, QLabel] = {}
        self.timeframe_widgets: Dict[str, Dict[str, QLineEdit]] = {}
        self.timeframe_enabled_widgets: Dict[str, QCheckBox] = {}
        self.session_enabled_widgets: Dict[str, QCheckBox] = {}
        self.added_indicator_ids: set = set()
        self.indicator_group_boxes: Dict[str, QGroupBox] = {}
        self.indicator_combine_row: Optional[QWidget] = None
        self.last_output_dir: Optional[str] = None
        self.last_plots_dir: Optional[str] = None
        self.last_trade_ledger_path: Optional[str] = None
        self.last_tables: Optional[dict] = None
        self.last_backtest_config = None
        self._trade_ledger_full_df = None
        self._trade_ledger_display_df = None
        self._worker_thread: Optional[threading.Thread] = None
        self.mt5_broker = MT5Broker()
        self._live_log_bridge = LiveLogBridge()
        self._live_log_bridge.line_ready.connect(self._append_live_log_line)
        try:
            os.makedirs(LIVE_JOURNAL_DIR, exist_ok=True)
        except OSError:
            pass
        self._live_engine: Optional[live_trading.LiveTradingEngine] = None
        self._live_thread: Optional[LiveTradingThread] = None
        self._telegram_test_worker: Optional[TelegramTestWorker] = None
        self._active_live_journal_dir: Optional[str] = None
        self._live_health_timer = QTimer(self)
        self._live_health_timer.setInterval(8000)
        self._live_health_timer.timeout.connect(self._check_live_worker_health)

        self._live_settings_host = QWidget(self)
        self._live_settings_host.setVisible(False)
        self._live_notifications_host = QWidget(self)
        self._live_notifications_host.setVisible(False)

        try:
            self.run_db: Optional[RunDatabase] = RunDatabase(RUN_DATABASE_PATH)
        except Exception as e:
            self.run_db = None
            print(f"Run history database unavailable: {e}")

        self._build_ui()
        self._build_shortcuts()
        self._dock_layout_busy = False
        QTimer.singleShot(0, self._restore_saved_layout)
        self._refresh_run_history_table()
        QTimer.singleShot(0, self._restore_live_settings)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_layout_sanitize_done", False):
            self._layout_sanitize_done = True
            QTimer.singleShot(100, self._post_show_layout_setup)

    def _post_show_layout_setup(self):
        """After the main window is visible — safe to resize docks and draw previews (macOS)."""
        try:
            self._apply_default_dock_sizes()
            if hasattr(self, "live_dock"):
                self.live_dock.raise_()
            self._sync_preview_toolbar_for_pattern()
            self._redraw_candle_preview()
        except Exception:
            pass

    def _frame_off_screen(self, frame, available) -> bool:
        if frame.isNull():
            return True
        margin = 80
        if frame.right() < available.left() + margin:
            return True
        if frame.bottom() < available.top() + margin:
            return True
        if frame.left() > available.right() - margin:
            return True
        if frame.top() > available.bottom() - margin:
            return True
        return False

    def _sanitize_window_and_docks(self):
        """Re-dock panels that floated off (Windows). macOS docks are not floatable."""
        if sys.platform == "darwin":
            return
        try:
            self._clamp_window_to_screen()
            any_floating = False
            for dock in self.findChildren(QDockWidget):
                if dock.isFloating():
                    any_floating = True
                    dock.setFloating(False)
            if any_floating:
                self._place_docks_default_layout()
                self._apply_default_dock_sizes()
        except Exception:
            pass

    def _dock_all_panels_before_save(self):
        """Persist layout with every panel docked — floating off-screen coords crash on restore."""
        for dock in self.findChildren(QDockWidget):
            try:
                if dock.isFloating():
                    dock.setFloating(False)
            except RuntimeError:
                pass

    def _apply_safe_startup_geometry(self):
        target_w, target_h = 1600, 980
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(target_w, available.width())
            height = min(target_h, available.height())
            x = available.x() + max(0, (available.width() - width) // 2)
            y = available.y() + max(0, (available.height() - height) // 2)
            self.setGeometry(x, y, width, height)
        else:
            self.resize(target_w, target_h)

    def _clamp_window_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        geo = self.geometry()
        w = min(geo.width(), available.width())
        h = min(geo.height(), available.height())
        x = max(available.x(), min(geo.x(), available.x() + available.width() - w))
        y = max(available.y(), min(geo.y(), available.y() + available.height() - h))
        if geo.x() < available.x() - 50 or geo.y() < available.y() - 50:
            x = available.x() + max(0, (available.width() - w) // 2)
            y = available.y() + max(0, (available.height() - h) // 2)
        self.setGeometry(x, y, w, h)

    def _clamp_floating_docks_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        for dock in self.findChildren(QDockWidget):
            if not dock.isFloating():
                continue
            g = dock.geometry()
            w = min(max(g.width(), 320), available.width())
            h = min(max(g.height(), 220), available.height())
            x = max(available.x(), min(g.x(), available.x() + available.width() - w))
            y = max(available.y(), min(g.y(), available.y() + available.height() - h))
            if g.x() < available.x() - 100 or g.y() < available.y() - 100:
                x = available.x() + 40
                y = available.y() + 40
            dock.setGeometry(x, y, w, h)

    def _restore_saved_layout(self):
        """Reapply saved geometry/docks only when compatible — bad dockState can crash Qt."""
        try:
            if sys.platform == "darwin":
                self._settings.remove("window/dockState")
                geometry = self._settings.value("window/geometry")
                if geometry is not None:
                    self.restoreGeometry(geometry)
                self._clamp_window_to_screen()
                return

            saved_ver = self._settings.value("window/dockLayoutVersion", 0, type=int)
            if saved_ver != DOCK_LAYOUT_VERSION:
                self._settings.remove("window/dockState")

            geometry = self._settings.value("window/geometry")
            if geometry is not None:
                self.restoreGeometry(geometry)
            self._clamp_window_to_screen()

            dock_state = self._settings.value("window/dockState")
            restored = False
            if dock_state is not None and saved_ver == DOCK_LAYOUT_VERSION:
                restored = bool(self.restoreState(dock_state))
            if not restored and getattr(self, "_default_dock_state", None) is not None:
                self.restoreState(self._default_dock_state)

            QTimer.singleShot(0, self._sanitize_window_and_docks)
            QTimer.singleShot(100, self._ensure_pattern_preview_linked)
        except Exception:
            if getattr(self, "_default_dock_state", None) is not None:
                try:
                    self.restoreState(self._default_dock_state)
                except Exception:
                    pass
            QTimer.singleShot(0, self._sanitize_window_and_docks)

    def closeEvent(self, event):
        self._stop_live_trading()
        self.mt5_broker.disconnect()
        try:
            self._dock_all_panels_before_save()
            self._clamp_window_to_screen()
            self._settings.setValue("window/geometry", self.saveGeometry())
            if sys.platform == "darwin":
                if getattr(self, "_default_dock_state", None) is not None:
                    self._settings.setValue("window/dockState", self._default_dock_state)
            else:
                self._settings.setValue("window/dockState", self.saveState())
            self._settings.setValue("window/dockLayoutVersion", DOCK_LAYOUT_VERSION)
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # TOP-LEVEL LAYOUT
    # ------------------------------------------------------------------
    # Parameters / Preview / Results are QDockWidgets: drag a title bar
    # to float a panel into its own window, dock it to any edge, or tab
    # it together with another panel -- every boundary resizes freely.
    # Default sizes below are picked to match what each panel actually
    # needs (not an arbitrary % of the window), so nothing opens either
    # mostly-empty or needing immediate scrolling.
    # ------------------------------------------------------------------
    def _build_ui(self):
        if sys.platform == "darwin":
            # Tab/stack inside the main window OK; floating separate windows crashes on macOS 15.
            self.setDockOptions(
                QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks
            )
        else:
            dock_opts = (
                QMainWindow.AnimatedDocks
                | QMainWindow.AllowNestedDocks
                | QMainWindow.AllowTabbedDocks
                | QMainWindow.GroupedDragging
            )
            self.setDockOptions(dock_opts)

        self.params_dock = self._make_dock("Backtest Parameters", self._build_parameter_panel())
        self.preview_dock = self._make_dock("Pattern & Preview", self._build_preview_panel())
        self.results_dock = self._make_dock("Results", self._build_results_panel())
        self.live_dock = self._make_dock("Live Trading", self._build_live_panel())

        # Client layout (reference): Pattern & Preview left | Live Trading right;
        # Results + Backtest Parameters as tabs on the bottom of the right stack.
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)

        self.addDockWidget(Qt.LeftDockWidgetArea, self.preview_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.live_dock)
        self.splitDockWidget(self.preview_dock, self.live_dock, Qt.Horizontal)
        self.tabifyDockWidget(self.live_dock, self.results_dock)
        self.tabifyDockWidget(self.live_dock, self.params_dock)
        self.live_dock.raise_()

        self._apply_default_dock_sizes()

        self._build_menu_bar()
        # Snapshot the layout we just built so "Reset Layout" can restore
        # it after panels get dragged around / floated out.
        self._default_dock_state = self.saveState()
        self._setup_status_bar()

    def _on_preview_dock_visibility_changed(self, visible: bool):
        if visible:
            self._sync_preview_toolbar_for_pattern()
            self._redraw_candle_preview()

    def _ensure_pattern_preview_linked(self):
        """Keep Pattern & Preview visible beside Results (Windows float recovery)."""
        if not hasattr(self, "preview_dock"):
            return
        if sys.platform == "darwin":
            if not self.preview_dock.isVisible():
                self.preview_dock.show()
            self._apply_default_dock_sizes()
            self._sync_preview_toolbar_for_pattern()
            QTimer.singleShot(0, self._redraw_candle_preview)
            return
        floating = any(
            d.isFloating()
            for d in (self.params_dock, self.preview_dock, self.results_dock, self.live_dock)
            if d is not None
        )
        if floating:
            self._place_docks_default_layout()
        else:
            for dock in (self.params_dock, self.preview_dock, self.results_dock, self.live_dock):
                if dock is not None and not dock.isVisible():
                    dock.show()
            self._apply_default_dock_sizes()
        self._sync_preview_toolbar_for_pattern()
        QTimer.singleShot(0, self._redraw_candle_preview)

    def _on_dock_top_level_changed(self, floating: bool):
        """Detached dock window (Windows only — macOS has no floatable docks)."""
        if sys.platform == "darwin":
            return
        if not floating:
            return
        QTimer.singleShot(0, self._place_docks_default_layout)
        QTimer.singleShot(30, self._apply_default_dock_sizes)
        QTimer.singleShot(50, self._redraw_candle_preview)

    def _setup_status_bar(self):
        bar = self.statusBar()
        bar.setObjectName("appStatusBar")
        self._status_bar = bar
        bar.showMessage("Ready — configure parameters and press Run Backtest (F5).")
        hint = QLabel("F2 layout · F1 help · F3–F4/F8–F9 panels")
        hint.setObjectName("sectionHint")
        bar.addPermanentWidget(hint)

    def _set_app_status(self, message: str, tooltip: str = ""):
        if hasattr(self, "status_label"):
            self.status_label.setText(message)
            self.status_label.setToolTip(tooltip or "")
        if hasattr(self, "_status_bar"):
            self._status_bar.showMessage(message, 0)

    def _apply_default_dock_sizes(self):
        try:
            # Preview column ~400px; right stack (Live / Results / Params tabs) gets the rest.
            self.resizeDocks([self.preview_dock, self.live_dock], [400, 960], Qt.Horizontal)
        except Exception:
            pass

    def _place_docks_default_layout(self):
        """Re-attach panels: Preview left | Live right; Results & Params tabbed with Live."""
        if getattr(self, "_dock_layout_busy", False):
            return
        if sys.platform == "darwin" and not self.isVisible():
            QTimer.singleShot(50, self._place_docks_default_layout)
            return
        self._dock_layout_busy = True
        docks = (self.params_dock, self.preview_dock, self.results_dock, self.live_dock)
        try:
            for dock in docks:
                if dock is None:
                    continue
                dock.blockSignals(True)
            for dock in docks:
                if dock is None:
                    continue
                try:
                    self.removeDockWidget(dock)
                except Exception:
                    pass
                # Never setFloating on macOS — DockWidgetFloatable is off and it bus-errors.
                if sys.platform != "darwin" and dock.isFloating():
                    dock.setFloating(False)
                dock.show()

            self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
            self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
            self.addDockWidget(Qt.LeftDockWidgetArea, self.preview_dock)
            self.addDockWidget(Qt.RightDockWidgetArea, self.live_dock)
            self.splitDockWidget(self.preview_dock, self.live_dock, Qt.Horizontal)
            self.tabifyDockWidget(self.live_dock, self.results_dock)
            self.tabifyDockWidget(self.live_dock, self.params_dock)
            self.live_dock.raise_()
            self._apply_default_dock_sizes()
        finally:
            for dock in docks:
                if dock is not None:
                    dock.blockSignals(False)
            self._dock_layout_busy = False

    def _make_dock(self, title: str, content: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(title.replace(" ", "").replace("&", "And"))
        if sys.platform == "darwin":
            # Movable tabs inside the main window only — no float (separate windows break layout/preview).
            features = QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        else:
            features = (
                QDockWidget.DockWidgetMovable
                | QDockWidget.DockWidgetClosable
                | QDockWidget.DockWidgetFloatable
            )
        dock.setFeatures(features)
        dock.setAllowedAreas(
            Qt.LeftDockWidgetArea
            | Qt.RightDockWidgetArea
            | Qt.TopDockWidgetArea
            | Qt.BottomDockWidgetArea
        )
        dock.setMinimumSize(320, 280)
        dock.setWidget(content)
        if sys.platform != "darwin":
            dock.topLevelChanged.connect(self._on_dock_float_changed)
        return dock

    def _on_dock_float_changed(self, floating: bool):
        if not floating:
            return
        dock = self.sender()
        if not isinstance(dock, QDockWidget):
            return
        # Resizing here directly, synchronously inside this signal,
        # races the platform's own window-reparenting transition (this
        # is the same class of bug as an earlier paint-corruption issue
        # in this app -- a resize firing mid-transition instead of
        # after it). Deferring to the next event-loop tick lets the
        # transition finish first. The RuntimeError guard covers the
        # dock having been closed or redocked again before the timer fires.
        QTimer.singleShot(0, lambda d=dock: self._resize_floated_dock(d))

    def _resize_floated_dock(self, dock: QDockWidget):
        try:
            if dock.isFloating():
                dock.resize(620, 680)
                screen = self.screen() or QApplication.primaryScreen()
                if screen is not None:
                    available = screen.availableGeometry()
                    g = dock.geometry()
                    if self._frame_off_screen(g, available):
                        dock.move(available.x() + 40, available.y() + 40)
        except RuntimeError:
            pass

    def _build_menu_bar(self):
        menu_bar = self.menuBar()
        view_menu = menu_bar.addMenu("View")

        self._dock_toggle_actions = []
        for dock in (self.params_dock, self.preview_dock, self.results_dock, self.live_dock):
            action = dock.toggleViewAction()
            view_menu.addAction(action)
            self._dock_toggle_actions.append(action)

        view_menu.addSeparator()
        self._reset_action = QAction("Reset Layout", self)
        self._reset_action.setToolTip(
            "Snaps panels back to the default arrangement. On Mac, drag one panel title "
            "onto another to tab them together; drag to screen edges to dock side-by-side."
        )
        self._reset_action.triggered.connect(self._reset_dock_layout)
        view_menu.addAction(self._reset_action)

    def _build_live_menu(self):
        """Advanced live toggles — kept off the main Live tab for client simplicity."""
        menu_bar = self.menuBar()
        live_menu = menu_bar.addMenu("Live")
        open_settings = QAction("Settings…", self)
        open_settings.setToolTip("MT5, execution, market/limit orders, safety, advanced")
        open_settings.triggered.connect(self._open_live_settings)
        live_menu.addAction(open_settings)
        live_menu.addSeparator()
        perf_menu = live_menu.addMenu("Performance")

        self._live_action_dry_run = QAction("Dry run (log signals only — no orders)", self)
        self._live_action_dry_run.setCheckable(True)
        self._live_action_dry_run.setChecked(True)
        self._live_action_dry_run.setToolTip(
            "Leave on while testing. Turn off only when you intend to send real orders to MT5."
        )
        self._live_action_dry_run.triggered.connect(self._on_live_performance_menu_changed)
        perf_menu.addAction(self._live_action_dry_run)

        self._live_action_thread_pool = QAction("Thread pool for signal CPU", self)
        self._live_action_thread_pool.setCheckable(True)
        self._live_action_thread_pool.setChecked(True)
        self._live_action_thread_pool.setToolTip("Keeps heavy signal math off the live poll thread.")
        self._live_action_thread_pool.triggered.connect(self._on_live_performance_menu_changed)
        perf_menu.addAction(self._live_action_thread_pool)

        self._live_action_ray = QAction("Use Ray if installed", self)
        self._live_action_ray.setCheckable(True)
        self._live_action_ray.setChecked(False)
        self._live_action_ray.setToolTip("Optional: pip install ray — falls back to thread pool if unavailable.")
        self._live_action_ray.triggered.connect(self._on_live_performance_menu_changed)
        perf_menu.addAction(self._live_action_ray)

    def _build_notifications_menu(self):
        """Top-level Notifications menu — separate from Live settings."""
        menu_bar = self.menuBar()
        notify_menu = menu_bar.addMenu("Notifications")
        open_tg = QAction("Telegram alerts…", self)
        open_tg.setToolTip(
            "Configure Telegram alerts for placed orders, dry run, failures, and safety blocks"
        )
        open_tg.triggered.connect(self._open_live_notifications)
        notify_menu.addAction(open_tg)

    def _build_fetch_menu(self):
        """Top-level Fetch menu — pulls history from nearest MT5 into data/."""
        menu_bar = self.menuBar()
        fetch_menu = menu_bar.addMenu("Fetch")
        open_fetch = QAction("Fetch / update data from MT5…", self)
        open_fetch.setToolTip(
            "Detect the data folder, connect to the nearest MT5 terminal, "
            "and update CSVs with newer bars (or full re-fetch)."
        )
        open_fetch.triggered.connect(self._open_fetch_data_dialog)
        fetch_menu.addAction(open_fetch)
        open_data = QAction("Open data folder", self)
        open_data.triggered.connect(self._open_data_folder_menu)
        fetch_menu.addAction(open_data)

    def _open_fetch_data_dialog(self):
        if self._live_worker_running():
            QMessageBox.warning(
                self,
                "Live is running",
                "Stop live trading before opening Fetch.\n\n"
                "History download and live trading share one MT5 Python connection.",
            )
            return
        dlg = FetchDataDialog(self)
        dlg.exec()

    def _open_data_folder_menu(self):
        w = self.field_widgets.get("data_root") if hasattr(self, "field_widgets") else None
        path = w.text().strip() if isinstance(w, QLineEdit) and w.text().strip() else DEFAULT_DATA_DIR
        path = backtest.resolve_data_root(path, anchor_dir=APP_DIR)
        os.makedirs(path, exist_ok=True)
        self._open_folder(path)

    def _live_dry_run_checked(self) -> bool:
        if hasattr(self, "live_dry_run_cb"):
            return self.live_dry_run_cb.isChecked()
        if hasattr(self, "_live_action_dry_run"):
            return self._live_action_dry_run.isChecked()
        return True

    def _live_thread_pool_checked(self) -> bool:
        if hasattr(self, "_live_action_thread_pool"):
            return self._live_action_thread_pool.isChecked()
        return True

    def _live_ray_checked(self) -> bool:
        if hasattr(self, "_live_action_ray"):
            return self._live_action_ray.isChecked()
        return False

    def _on_live_performance_menu_changed(self):
        if hasattr(self, "live_dry_run_cb") and hasattr(self, "_live_action_dry_run"):
            self.live_dry_run_cb.blockSignals(True)
            self.live_dry_run_cb.setChecked(self._live_action_dry_run.isChecked())
            self.live_dry_run_cb.blockSignals(False)
        self._save_live_settings()
        self._refresh_live_perf_hint()

    def _on_live_dry_run_checkbox(self, checked: bool):
        if hasattr(self, "_live_action_dry_run"):
            self._live_action_dry_run.blockSignals(True)
            self._live_action_dry_run.setChecked(checked)
            self._live_action_dry_run.blockSignals(False)
        self._save_live_settings()
        self._refresh_live_perf_hint()
        self._live_log("Dry run ON — orders will not be sent." if checked else "Dry run OFF — orders will be sent when signals fire.")
        self._refresh_live_config_summary()

    def _refresh_live_perf_hint(self):
        self._refresh_live_config_summary()

    def _reset_dock_layout(self):
        self._place_docks_default_layout()
        if sys.platform != "darwin" and getattr(self, "_default_dock_state", None) is not None:
            try:
                self.restoreState(self._default_dock_state)
            except Exception:
                pass
        QTimer.singleShot(0, self._apply_default_dock_sizes)
        if hasattr(self, "live_dock"):
            QTimer.singleShot(0, self.live_dock.raise_)
        QTimer.singleShot(0, self._ensure_pattern_preview_linked)

    # ------------------------------------------------------------------
    # KEYBOARD SHORTCUTS -- function keys for the actions you'd
    # otherwise have to reach for the mouse to click. All of them also
    # show up (with their key) in the Run / View / Help menus, so
    # they're discoverable, not just memorized.
    #
    # IMPORTANT: every shortcut below is explicitly given
    # Qt.ApplicationShortcut context. By default a shortcut only fires
    # when the window that owns it has keyboard focus -- but a floated
    # dock (e.g. "Pattern & Preview" popped out into its own window, as
    # in the reported case) becomes its own separate top-level window.
    # Without ApplicationShortcut context, F2 would do nothing while
    # that floated window is focused, which is exactly the "I can't get
    # my panel back" problem. With it, every shortcut below works no
    # matter which of the app's windows currently has focus.
    # ------------------------------------------------------------------
    def _build_shortcuts(self):
        menu_bar = self.menuBar()
        self._build_live_menu()
        self._build_notifications_menu()
        self._build_fetch_menu()
        run_menu = menu_bar.addMenu("Run")

        run_action = QAction("Run Backtest", self)
        run_action.setShortcut(QKeySequence(Qt.Key_F5))
        run_action.setShortcutContext(Qt.ApplicationShortcut)
        run_action.triggered.connect(lambda: self.run_button.click() if self.run_button.isEnabled() else None)
        run_menu.addAction(run_action)

        output_action = QAction("Open Output Folder", self)
        output_action.setShortcut(QKeySequence(Qt.Key_F6))
        output_action.setShortcutContext(Qt.ApplicationShortcut)
        output_action.triggered.connect(self._open_output_folder)
        run_menu.addAction(output_action)

        charts_action = QAction("Open Charts Folder", self)
        charts_action.setShortcut(QKeySequence(Qt.Key_F7))
        charts_action.setShortcutContext(Qt.ApplicationShortcut)
        charts_action.triggered.connect(self._open_plots_folder)
        run_menu.addAction(charts_action)

        # All of F1-F8 are already spoken for above, so this one uses a
        # modifier combo instead -- Ctrl+E for "Export".
        export_action = QAction("Export Run History to Excel", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.setShortcutContext(Qt.ApplicationShortcut)
        export_action.triggered.connect(self._export_run_history_to_excel)
        run_menu.addAction(export_action)

        verify_dir_action = QAction("Verify hammer direction rules (self-test)", self)
        verify_dir_action.triggered.connect(self._verify_hammer_direction_rules)
        run_menu.addAction(verify_dir_action)

        # F2 snaps every panel -- docked, floated, or closed -- back to
        # its default docked position. This is the answer to "how do I
        # get a detached window back": press F2 from anywhere.
        self._reset_action.setText("Snap All Panels Back (Reset Layout)")
        self._reset_action.setShortcut(QKeySequence(Qt.Key_F2))
        self._reset_action.setShortcutContext(Qt.ApplicationShortcut)
        self._reset_action.setToolTip(
            "Docks every panel back into the main window, even ones that "
            "were floated out into their own window. Works from any window -- press F2."
        )

        # F3/F4/F8 to toggle each panel -- deliberately avoiding F9-F12,
        # which several OSes reserve for window/desktop management and
        # may never reach the app.
        panel_keys = [Qt.Key_F3, Qt.Key_F4, Qt.Key_F8, Qt.Key_F9]
        for action, key in zip(self._dock_toggle_actions, panel_keys):
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ApplicationShortcut)

        help_menu = menu_bar.addMenu("Help")

        update_action = QAction("Check for Updates…", self)
        update_action.setToolTip(
            "Check GitHub Releases for a newer Hammer version and install it."
        )
        update_action.triggered.connect(self._check_for_updates)
        help_menu.addAction(update_action)

        token_action = QAction("GitHub Update Token…", self)
        token_action.setToolTip(
            "Save a GitHub Personal Access Token for private release updates."
        )
        token_action.triggered.connect(self._show_github_token_dialog)
        help_menu.addAction(token_action)
        help_menu.addSeparator()

        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.setShortcut(QKeySequence(Qt.Key_F1))
        shortcuts_action.setShortcutContext(Qt.ApplicationShortcut)
        shortcuts_action.triggered.connect(self._show_shortcuts_help)
        help_menu.addAction(shortcuts_action)

        # The title bar is intentionally left blank -- this is where the
        # app's name/version lives instead.
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

        guide_action = QAction("Parameter Guide (what each tab does)", self)
        guide_action.triggered.connect(self._show_parameters_guide)
        help_menu.addAction(guide_action)

        # Keep references so the actions (and their shortcuts) aren't
        # garbage-collected once this method returns.
        self._shortcut_actions = [
            run_action, output_action, charts_action, export_action,
            shortcuts_action, update_action, token_action, about_action, guide_action,
            verify_dir_action,
        ]

    def _show_parameters_guide(self):
        box = QMessageBox(self)
        box.setWindowTitle("Parameter guide")
        box.setTextFormat(Qt.RichText)
        box.setText(PARAMETERS_GUIDE_TEXT)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    def _show_shortcuts_help(self):
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            "F2   Snap all panels back to the default layout\n"
            "        (use if Pattern & Preview opened in its own window)\n\n"
            "Arrange panels: drag a panel title onto another to tab them; "
            "on Mac, panels stay inside this window (no separate float window).\n\n"
            "F5   Run Backtest\n"
            "F6   Open Output Folder\n"
            "F7   Open Charts Folder\n"
            "Ctrl+E   Export Run History to Excel\n"
            "F3   Toggle Backtest Parameters panel\n"
            "F4   Toggle Pattern & Preview panel\n"
            "F8   Toggle Results panel\n"
            "F9   Toggle Live Trading panel\n"
            "Notifications → Telegram alerts   Order notifications\n"
            "Fetch → Fetch / update data from MT5   History CSVs\n"
            "Live → Performance   Dry run, thread pool, Ray (advanced)\n"
            "F1   This help\n\n"
            "Help → Parameter Guide — which tabs affect signals vs backtest sizing vs live.\n\n"
            "All shortcuts work while the app has focus.",
        )

    def _check_for_updates(self):
        try:
            from update_window import open_update_window
        except Exception as e:
            QMessageBox.warning(
                self,
                "Updates unavailable",
                f"Could not open the update dialog:\n{e}",
            )
            return
        open_update_window(self)

    def _show_github_token_dialog(self):
        import updater

        dlg = QDialog(self)
        dlg.setWindowTitle("GitHub Update Token")
        dlg.setMinimumWidth(460)
        layout = QVBoxLayout(dlg)

        intro = QLabel(
            "For private GitHub Releases, paste a Personal Access Token "
            "(classic with <b>repo</b> scope, or fine-grained with read access "
            "to this repo). It is saved as a plain text file next to the app — "
            "not inside the exe."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        layout.addWidget(intro)

        form = QFormLayout()
        token_edit = QLineEdit()
        token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        token_edit.setPlaceholderText("ghp_… or github_pat_…")
        if updater.GITHUB_TOKEN:
            token_edit.setText(updater.GITHUB_TOKEN)
        form.addRow("Token:", token_edit)
        layout.addLayout(form)

        path_label = QLabel(f"Will save to:\n{updater._primary_token_path()}")
        path_label.setWordWrap(True)
        path_label.setStyleSheet("color: #666666; font-size: 11px;")
        layout.addWidget(path_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            saved_path = updater.save_github_token(token_edit.text())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid token", str(e))
            return
        except OSError as e:
            QMessageBox.warning(
                self,
                "Could not save token",
                f"Could not write the token file:\n{e}",
            )
            return

        QMessageBox.information(
            self,
            "Token saved",
            f"GitHub token saved to:\n{saved_path}\n\n"
            "Try Help → Check for Updates…",
        )

    def _show_about_dialog(self):
        try:
            from version import __version__ as app_version
        except Exception:
            app_version = "?"
        QMessageBox.about(
            self,
            "About",
            "<b>Hammer Candle Backtest Dashboard</b><br>"
            f"Version {app_version}<br><br>"
            "Hammer & Doji backtesting, indicators, run history, compare, and "
            "optional MT5 live trading (Windows).<br><br>"
            "<b>Updates:</b> Help → Check for Updates…<br>"
            "Private repo: Help → GitHub Update Token… (or a plain text file "
            "<code>.hammer_github_token</code> / <code>hammer_github_token.txt</code> "
            "next to the exe). Classic token needs <code>repo</code> scope.<br>"
            "You must also publish a GitHub <b>Release</b> with a <code>.zip</code> "
            "asset for updates to appear.<br><br>"
            "Press <b>F1</b> for keyboard shortcuts.",
        )

    # ------------------------------------------------------------------
    # RUN CONTROLS: Run button + status + progress bar. Used inline next
    # to the Candle Pattern dropdown instead of a dedicated full-width
    # top bar, to reclaim that strip of vertical space.
    # ------------------------------------------------------------------
    def _build_run_controls(self) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.run_button = QPushButton("Run Backtest")
        self.run_button.setObjectName("primaryRunButton")
        self.run_button.setMinimumWidth(150)
        self.run_button.setToolTip("Run Backtest  (F5)")
        self.run_button.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_button)

        status_col = QVBoxLayout()
        status_col.setSpacing(2)
        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("appStatus")
        status_col.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setMinimumWidth(140)
        status_col.addWidget(self.progress_bar)

        status_wrap = QWidget()
        status_wrap.setLayout(status_col)
        layout.addWidget(status_wrap)

        return wrap

    # ------------------------------------------------------------------
    # PARAMETER PANEL: horizontal tabs (client's requested "horizontal
    # not vertical" structure) instead of one long scrolling column.
    # ------------------------------------------------------------------
    def _build_parameter_panel(self) -> QWidget:
        wrap = QWidget()
        wrap.setMinimumHeight(360)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QFrame()
        header.setObjectName("paramsHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 6)
        pat_label = QLabel("Strategy")
        pat_label.setObjectName("fieldLabel")
        header_layout.addWidget(pat_label)
        self.pattern_combo = QComboBox()
        self.pattern_combo.setMinimumWidth(160)
        self.pattern_combo.addItems(list(PATTERN_REGISTRY.keys()))
        self.pattern_combo.currentTextChanged.connect(self._on_pattern_changed)
        header_layout.addWidget(self.pattern_combo)
        header_layout.addStretch()
        header_layout.addWidget(self._build_run_controls())
        layout.addWidget(header)

        tabs = QTabWidget()
        tabs.setObjectName("mainParamTabs")
        tabs.setMinimumHeight(300)
        self.parameter_tabs = tabs
        layout.addWidget(tabs, 1)

        defaults_shape = logic.HammerRatioConfig()
        defaults_doji_shape = doji_logic.DojiRatioConfig()
        defaults_strategy = logic.StrategyConfig()
        defaults_doji_strategy = doji_logic.DojiStrategyConfig()
        defaults_backtest = backtest.BacktestConfig()
        defaults_backtest.data_root = DEFAULT_DATA_DIR
        defaults_context = hammer_context_logic.HammerContextConfig()

        tabs.addTab(self._make_body_tab(
            defaults_shape, defaults_doji_shape, defaults_context,
        ), "Body")
        tabs.addTab(self._make_wicks_tab(
            defaults_shape, defaults_doji_shape, defaults_strategy, defaults_doji_strategy,
            defaults_context,
        ), "Wicks")
        self._param_tab_context = tabs.count()
        tabs.addTab(self._make_field_tab(
            CONTEXT_PATTERN_FIELDS, defaults_context, live_preview=True,
        ), "Context")
        context_hint = QLabel(
            "<b>Hammer with candles — context rule:</b><br>"
            "• <b>BUY</b> — green signal candle; prior N closes must stay at or above candle low.<br>"
            "• <b>SELL</b> — red signal candle; prior N closes must stay at or below candle high.<br>"
            "N = 0 means only the signal candle (no prior bars). Pattern In Context colours "
            "those N bars plus the signal; later bars stay grey.<br>"
            "Wick shape is optional: uncheck it on the <b>Wicks</b> tab to detect body size + color only "
            "(set Body % to 10 for a 10% max). Wick up or down is ignored.<br>"
            "Entry / stop per side on <b>Entry / Exit</b>."
        )
        context_hint.setObjectName("sectionHint")
        context_hint.setWordWrap(True)
        ctx_tab = self.parameter_tabs.widget(self._param_tab_context)
        if isinstance(ctx_tab, QScrollArea):
            inner = ctx_tab.widget()
            if inner is not None and inner.layout() is not None:
                inner.layout().addWidget(context_hint)
        self._param_tab_hammer_direction = tabs.count()
        tabs.addTab(self._make_field_tab(
            DIRECTION_FIELDS,
            defaults_strategy, live_preview=True,
        ), "Direction")
        dir_hint = QLabel(
            "<b>Four choices — each is BUY, SELL, or NO:</b><br>"
            "• <b>Classic</b> = long lower wick. <b>Inverted</b> = long upper wick.<br>"
            "• <b>Green / red</b> = the signal candle’s own color (not the next entry bar).<br>"
            "• <b>NO</b> = skip that combination. Set both colors of a type to NO to turn that type off.<br>"
            "Defaults: Classic green BUY, Classic red SELL, Inverted green BUY, Inverted red SELL."
        )
        dir_hint.setObjectName("sectionHint")
        dir_hint.setWordWrap(True)
        dir_tab_idx = self._param_tab_hammer_direction
        dir_tab = self.parameter_tabs.widget(dir_tab_idx)
        if isinstance(dir_tab, QScrollArea):
            inner = dir_tab.widget()
            if inner is not None and inner.layout() is not None:
                inner.layout().addWidget(dir_hint)
        self._param_tab_doji_direction = tabs.count()
        tabs.addTab(self._make_field_tab(
            DOJI_DIRECTION_FIELDS + DOJI_CANDLE_COLOR_FIELDS,
            defaults_doji_strategy, live_preview=True,
        ), "Doji Direction")
        doji_hint = QLabel(
            "<b>Doji rules (same in backtest and live):</b><br>"
            "1) <b>Style</b> — ANY, Classic, Dragonfly, Gravestone, or Long-legged (shape on Body/Wicks tabs).<br>"
            "2) <b>Direction mode</b> — wick bias, candle color, fixed buy/sell, or next candle color.<br>"
            "3) Use <b>Pattern & Preview</b> BUY / SELL toggles; Pattern In Context follows the same preview."
        )
        doji_hint.setObjectName("sectionHint")
        doji_hint.setWordWrap(True)
        doji_tab = self.parameter_tabs.widget(self._param_tab_doji_direction)
        if isinstance(doji_tab, QScrollArea):
            inner = doji_tab.widget()
            if inner is not None and inner.layout() is not None:
                inner.layout().addWidget(doji_hint)
        tabs.addTab(self._make_indicator_tab(), "Indicators")
        tabs.addTab(self._make_entry_exit_tab(defaults_strategy), "Entry / Exit")
        tabs.addTab(
            self._make_combined_timeframes_tab(
                defaults_strategy, defaults_context, defaults_doji_strategy,
            ),
            "Timeframes",
        )
        tabs.addTab(self._make_run_settings_tab(defaults_backtest), "Run Settings")

        QTimer.singleShot(0, self._apply_pattern_field_visibility)
        QTimer.singleShot(0, self._wire_symmetric_tolerance_fields)
        QTimer.singleShot(0, self._wire_wick_requirement_fields)
        QTimer.singleShot(0, self._wire_sl_mode_fields)
        # Scroll wrapper keeps the panel resizable below its natural width
        # (the wide tab bar otherwise locks the dock divider in place).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(wrap)
        return scroll

    def _on_pattern_changed(self, _pattern_name: str = ""):
        self._apply_pattern_field_visibility()
        if self.pattern_combo.currentText() == "Doji":
            self._fill_empty_doji_widget_defaults()
        self._sync_preview_toolbar_for_pattern()
        self._redraw_candle_preview()
        self._sync_live_pattern_from_dashboard()
        self._refresh_live_strategy_summary()

    def _fill_empty_doji_widget_defaults(self):
        """If doji fields were left blank (older builds), seed from DojiRatioConfig."""
        shape_defaults = doji_logic.DojiRatioConfig()
        strategy_defaults = doji_logic.DojiStrategyConfig()
        for name in DOJI_ONLY_FIELD_NAMES:
            widget = self.field_widgets.get(name)
            if not isinstance(widget, QLineEdit):
                continue
            if widget.text().strip():
                continue
            val = default_value_for_field(name, [shape_defaults, strategy_defaults])
            if val != "" and val is not None:
                if isinstance(val, Enum):
                    val = val.value
                widget.setText(str(val))

    def _apply_pattern_field_visibility(self):
        """Show hammer / doji / hammer-context fields depending on the selected pattern."""
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else "Hammer"
        is_doji = pattern == "Doji"
        is_hammer_with_candles = pattern == "Hammer with candles"
        is_plain_hammer = pattern == "Hammer"

        for name in HAMMER_ONLY_FIELD_NAMES:
            if name in DIRECTION_FIELD_NAMES:
                visible = is_plain_hammer
            elif name in HAMMER_RATIO_FIELD_NAMES:
                visible = is_plain_hammer
            else:
                visible = is_plain_hammer or is_hammer_with_candles
            if name in self.field_widgets:
                self.field_widgets[name].setVisible(visible)
            if name in self.field_labels:
                self.field_labels[name].setVisible(visible)

        for name in DOJI_ONLY_FIELD_NAMES:
            visible = is_doji
            if name in self.field_widgets:
                self.field_widgets[name].setVisible(visible)
            if name in self.field_labels:
                self.field_labels[name].setVisible(visible)

        for name in HAMMER_WITH_CANDLES_SHAPE_FIELD_NAMES:
            visible = is_hammer_with_candles
            if name in self.field_widgets:
                self.field_widgets[name].setVisible(visible)
            if name in self.field_labels:
                self.field_labels[name].setVisible(visible)

        for name in CONTEXT_PATTERN_FIELD_NAMES:
            visible = is_hammer_with_candles
            if name in self.field_widgets:
                self.field_widgets[name].setVisible(visible)
            if name in self.field_labels:
                self.field_labels[name].setVisible(visible)

        if hasattr(self, "tolerance_hint_label"):
            if is_doji:
                self.tolerance_hint_label.setText(
                    "Loosest and tightest candles your current doji tolerance "
                    "settings will still accept."
                )
            elif is_hammer_with_candles:
                self.tolerance_hint_label.setText(
                    "With wick required: classic BUY / inverted SELL shape. "
                    "With wick off: Body % max only; direction from previous candle "
                    "(red→BUY, green→SELL) — signal color ignored."
                )
            else:
                self.tolerance_hint_label.setText(
                    "Loosest and tightest candles your current tolerance "
                    "settings will still accept as a valid hammer."
                )

        if hasattr(self, "parameter_tabs"):
            self.parameter_tabs.setTabVisible(self._param_tab_hammer_direction, is_plain_hammer)
            self.parameter_tabs.setTabVisible(self._param_tab_doji_direction, is_doji)
            if hasattr(self, "_param_tab_context"):
                self.parameter_tabs.setTabVisible(self._param_tab_context, is_hammer_with_candles)
        if hasattr(self, "_entry_exit_inverted_box"):
            self._entry_exit_inverted_box.setVisible(is_plain_hammer or is_hammer_with_candles)
        if hasattr(self, "_entry_exit_classic_box"):
            if is_doji:
                self._entry_exit_classic_box.setTitle("Entry / stop")
            elif not is_hammer_with_candles:
                self._entry_exit_classic_box.setTitle("Classic hammer")
                self._entry_exit_inverted_box.setTitle("Inverted hammer")
        if hasattr(self, "_entry_exit_hint"):
            if is_doji:
                self._entry_exit_hint.setText(
                    "Doji uses these entry and stop settings for every signal."
                )
            elif not is_hammer_with_candles:
                self._entry_exit_hint.setText(
                    "Classic (long lower wick) and inverted (long upper wick) have their own entry and stop. "
                    "HAMMER_HIGH on classic is the short-wick side; on inverted it is the long-wick tip."
                )
        if is_hammer_with_candles:
            self._refresh_hwc_copy_for_wick_mode()
        else:
            for fname in ("entry_rule", "inverted_entry_rule"):
                self._apply_entry_rule_combo_labels(fname, wick_required=True)

        hwc_boxes = (
            getattr(self, "_hwc_body_hint", None),
            getattr(self, "_hwc_body_buy_box", None),
            getattr(self, "_hwc_body_sell_box", None),
            getattr(self, "_hwc_wicks_hint", None),
            getattr(self, "_hwc_wicks_buy_box", None),
            getattr(self, "_hwc_wicks_sell_box", None),
        )
        for box in hwc_boxes:
            if box is not None:
                box.setVisible(is_hammer_with_candles)

        if hasattr(self, "_plain_body_box"):
            self._plain_body_box.setVisible(is_plain_hammer or is_doji)
        if hasattr(self, "_plain_wicks_box"):
            self._plain_wicks_box.setVisible(is_plain_hammer or is_doji)

    def _make_body_tab(self, defaults_shape, defaults_doji_shape, defaults_context) -> QWidget:
        """Body tab: separate BUY/SELL boxes for Hammer with candles; plain fields for Hammer/Doji."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        hwc_hint = QLabel(
            "<b>Hammer with candles — body size</b><br>"
            "Two independent sections: BUY and SELL.<br>"
            "When wick is <b>on</b>: Body % is a target with +/- tolerance (hammer shape).<br>"
            "When wick is <b>off</b>: Body % is a hard maximum (e.g. 10 = body ≤ 10%). "
            "Wick shape and <b>signal candle color are ignored</b>. "
            "Direction comes from the previous candle: red → BUY, green → SELL."
        )
        hwc_hint.setObjectName("sectionHint")
        hwc_hint.setWordWrap(True)
        layout.addWidget(hwc_hint)
        self._hwc_body_hint = hwc_hint

        buy_box = QGroupBox("BUY — Classic green hammer")
        buy_grid = QGridLayout(buy_box)
        buy_grid.setContentsMargins(12, 16, 12, 12)
        buy_grid.setHorizontalSpacing(20)
        buy_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            buy_grid, BUY_CONTEXT_BODY_FIELDS, defaults_context,
            live_preview=True, columns_per_row=2,
        )
        layout.addWidget(buy_box)
        self._hwc_body_buy_box = buy_box

        sell_box = QGroupBox("SELL — Inverted red hammer")
        sell_grid = QGridLayout(sell_box)
        sell_grid.setContentsMargins(12, 16, 12, 12)
        sell_grid.setHorizontalSpacing(20)
        sell_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            sell_grid, SELL_CONTEXT_BODY_FIELDS, defaults_context,
            live_preview=True, columns_per_row=2,
        )
        layout.addWidget(sell_box)
        self._hwc_body_sell_box = sell_box

        plain_box = QGroupBox("Hammer / Doji body settings")
        plain_grid = QGridLayout(plain_box)
        plain_grid.setContentsMargins(12, 16, 12, 12)
        plain_grid.setHorizontalSpacing(20)
        plain_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            plain_grid, BODY_FIELDS + DOJI_BODY_FIELDS,
            defaults_shape, defaults_doji_shape,
            live_preview=True, columns_per_row=3,
        )
        layout.addWidget(plain_box)
        self._plain_body_box = plain_box

        layout.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _make_wicks_tab(
        self,
        defaults_shape,
        defaults_doji_shape,
        defaults_strategy,
        defaults_doji_strategy,
        defaults_context,
    ) -> QWidget:
        """Wicks tab: separate BUY/SELL boxes for Hammer with candles; plain fields for Hammer/Doji."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        hwc_hint = QLabel(
            "<b>Hammer with candles — wick size</b><br>"
            "<b>Unchecked (body-only mode):</b> Dominant Wick % is <b>not used</b>. "
            "Only Body % counts. Signal color does not matter. "
            "Previous candle: <b>red → BUY</b>, <b>green → SELL</b>.<br>"
            "<b>Checked (default):</b> classic long lower wick for BUY, inverted long upper wick for SELL "
            "(that is when the 60% ± 18% band applies)."
        )
        hwc_hint.setObjectName("sectionHint")
        hwc_hint.setWordWrap(True)
        layout.addWidget(hwc_hint)
        self._hwc_wicks_hint = hwc_hint

        buy_box = QGroupBox("BUY — Classic green hammer")
        buy_grid = QGridLayout(buy_box)
        buy_grid.setContentsMargins(12, 16, 12, 12)
        buy_grid.setHorizontalSpacing(20)
        buy_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            buy_grid, BUY_WICK_REQUIRE_FIELDS + BUY_CONTEXT_WICK_FIELDS, defaults_context,
            live_preview=True, columns_per_row=2,
        )
        layout.addWidget(buy_box)
        self._hwc_wicks_buy_box = buy_box

        sell_box = QGroupBox("SELL — Inverted red hammer")
        sell_grid = QGridLayout(sell_box)
        sell_grid.setContentsMargins(12, 16, 12, 12)
        sell_grid.setHorizontalSpacing(20)
        sell_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            sell_grid, SELL_WICK_REQUIRE_FIELDS + SELL_CONTEXT_WICK_FIELDS, defaults_context,
            live_preview=True, columns_per_row=2,
        )
        layout.addWidget(sell_box)
        self._hwc_wicks_sell_box = sell_box

        plain_box = QGroupBox("Hammer / Doji wick settings")
        plain_grid = QGridLayout(plain_box)
        plain_grid.setContentsMargins(12, 16, 12, 12)
        plain_grid.setHorizontalSpacing(20)
        plain_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(
            plain_grid, WICK_FIELDS + DOJI_WICK_FIELDS,
            defaults_shape, defaults_doji_shape, defaults_strategy, defaults_doji_strategy,
            live_preview=True, columns_per_row=3,
        )
        layout.addWidget(plain_box)
        self._plain_wicks_box = plain_box

        layout.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _wire_symmetric_tolerance_fields(self):
        """Enable +/- tolerance when Symmetric is on; enable lower/upper when off."""
        prefixes = ("", "buy_", "sell_")
        for prefix in prefixes:
            for sym_name, tol_name, lo_name, hi_name in SYMMETRIC_TOLERANCE_GROUPS:
                self._wire_one_symmetric_group(
                    f"{prefix}{sym_name}",
                    f"{prefix}{tol_name}",
                    f"{prefix}{lo_name}",
                    f"{prefix}{hi_name}",
                )

    def _wire_one_symmetric_group(
        self, sym_name: str, tol_name: str, lo_name: str, hi_name: str,
    ) -> None:
        sym_widget = self.field_widgets.get(sym_name)
        tol_widget = self.field_widgets.get(tol_name)
        lo_widget = self.field_widgets.get(lo_name)
        hi_widget = self.field_widgets.get(hi_name)
        if sym_widget is None:
            return

        def refresh():
            symmetric = sym_widget.isChecked()
            if tol_widget is not None:
                tol_widget.setEnabled(symmetric)
                label = self.field_labels.get(tol_name)
                if label is not None:
                    label.setEnabled(symmetric)
            for widget, name in ((lo_widget, lo_name), (hi_widget, hi_name)):
                if widget is not None:
                    widget.setEnabled(not symmetric)
                label = self.field_labels.get(name)
                if label is not None:
                    label.setEnabled(not symmetric)

        sym_widget.stateChanged.connect(lambda _state: refresh())
        refresh()

    def _wire_wick_requirement_fields(self):
        """Grey out wick size fields when Require wick shape? is off."""
        if getattr(self, "_wick_req_wired", False):
            for side in ("buy", "sell"):
                self._refresh_side_wick_fields_enabled(side)
            return
        self._wick_req_wired = True
        for side in ("buy", "sell"):
            chk = self.field_widgets.get(f"{side}_require_wick")
            if chk is None:
                continue
            chk.stateChanged.connect(
                lambda _state, s=side: self._refresh_side_wick_fields_enabled(s)
            )
            chk.stateChanged.connect(self._redraw_candle_preview)
            self._refresh_side_wick_fields_enabled(side)

    def _refresh_side_wick_fields_enabled(self, side: str):
        chk = self.field_widgets.get(f"{side}_require_wick")
        required = True if chk is None else chk.isChecked()
        field_defs = BUY_CONTEXT_WICK_FIELDS if side == "buy" else SELL_CONTEXT_WICK_FIELDS
        for name, _, _, _ in field_defs:
            widget = self.field_widgets.get(name)
            label = self.field_labels.get(name)
            if widget is not None:
                widget.setEnabled(required)
            if label is not None:
                label.setEnabled(required)
        if not required:
            self._refresh_side_body_fields_for_wick(side, wick_required=False)
            self._refresh_hwc_copy_for_wick_mode()
            return
        self._refresh_side_body_fields_for_wick(side, wick_required=True)
        prefix = f"{side}_"
        for sym_name, tol_name, lo_name, hi_name in SYMMETRIC_TOLERANCE_GROUPS:
            if not (sym_name.startswith("dominant_") or sym_name.startswith("small_")):
                continue
            self._apply_one_symmetric_enabled(
                f"{prefix}{sym_name}",
                f"{prefix}{tol_name}",
                f"{prefix}{lo_name}",
                f"{prefix}{hi_name}",
            )
        self._refresh_hwc_copy_for_wick_mode()

    def _require_wick_checked(self, side: str) -> bool:
        chk = self.field_widgets.get(f"{side}_require_wick")
        return True if chk is None else bool(chk.isChecked())

    def _apply_entry_rule_combo_labels(self, field_name: str, wick_required: bool) -> None:
        """Rename HAMMER_* entry options for body-only (wick off) vs hammer shape (wick on)."""
        combo = self.field_widgets.get(field_name)
        if not isinstance(combo, QComboBox):
            return
        labels = ENTRY_RULE_LABELS_WICK_ON if wick_required else ENTRY_RULE_LABELS_WICK_OFF
        current = combo.currentData()
        if current is None:
            current = combo.currentText()
        combo.blockSignals(True)
        for i in range(combo.count()):
            val = combo.itemData(i)
            if val is None:
                val = combo.itemText(i)
            combo.setItemText(i, labels.get(str(val), str(val)))
        idx = combo.findData(current)
        if idx < 0 and current is not None:
            idx = combo.findData(str(current))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh_hwc_copy_for_wick_mode(self):
        """Entry/Exit and Body/Wicks titles: 'hammer' only when wick shape is required."""
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else ""
        if pattern != "Hammer with candles":
            return
        buy_wick = self._require_wick_checked("buy")
        sell_wick = self._require_wick_checked("sell")
        buy_title = (
            "BUY — classic green hammer" if buy_wick else "BUY — prev red → signal (body only)"
        )
        sell_title = (
            "SELL — inverted red hammer" if sell_wick else "SELL — prev green → signal (body only)"
        )
        for attr, title in (
            ("_hwc_body_buy_box", buy_title),
            ("_hwc_wicks_buy_box", buy_title),
            ("_entry_exit_classic_box", buy_title),
            ("_hwc_body_sell_box", sell_title),
            ("_hwc_wicks_sell_box", sell_title),
            ("_entry_exit_inverted_box", sell_title),
        ):
            box = getattr(self, attr, None)
            if box is not None:
                box.setTitle(title)
        self._apply_entry_rule_combo_labels("entry_rule", buy_wick)
        self._apply_entry_rule_combo_labels("inverted_entry_rule", sell_wick)
        if hasattr(self, "_entry_exit_hint"):
            if not buy_wick and not sell_wick:
                self._entry_exit_hint.setText(
                    "Wick off: signal color is ignored. Direction = previous candle "
                    "(red → BUY, green → SELL). Body % only on the signal bar. "
                    "BUY uses this box (SL at signal low + buffer by default). "
                    "SELL uses the box below (SL at signal high + buffer). "
                    "Entry Rule options say <i>Signal candle</i> instead of <i>Hammer candle</i>."
                )
            elif buy_wick and sell_wick:
                self._entry_exit_hint.setText(
                    "Classic green hammer (BUY) and inverted red hammer (SELL) each have their own entry and stop. "
                    "Entry Rule lists <i>Hammer candle</i> close / high / low for entries on the signal bar."
                )
            else:
                wick_bits = []
                if buy_wick:
                    wick_bits.append("BUY: hammer shape — Entry Rule shows Hammer candle …")
                else:
                    wick_bits.append("BUY: body only — Entry Rule shows Signal candle …")
                if sell_wick:
                    wick_bits.append("SELL: hammer shape — Entry Rule shows Hammer candle …")
                else:
                    wick_bits.append("SELL: body only — Entry Rule shows Signal candle …")
                self._entry_exit_hint.setText(" ".join(wick_bits))

    def _wire_sl_mode_fields(self):
        """Enable buffer fields for candle SL; fixed distance for fixed-from-entry SL."""
        if getattr(self, "_sl_mode_wired", False):
            self._refresh_sl_mode_fields("classic")
            self._refresh_sl_mode_fields("inverted")
            return
        self._sl_mode_wired = True
        for mode_name, side in (("sl_mode", "classic"), ("inverted_sl_mode", "inverted")):
            combo = self.field_widgets.get(mode_name)
            if isinstance(combo, QComboBox):
                self._apply_stop_loss_combo_labels(mode_name)
                combo.currentTextChanged.connect(
                    lambda _t, s=side: self._refresh_sl_mode_fields(s)
                )
                combo.currentTextChanged.connect(self._redraw_candle_preview)
        for fname in ("sl_fixed_distance", "inverted_sl_fixed_distance"):
            w = self.field_widgets.get(fname)
            if isinstance(w, QLineEdit):
                w.textChanged.connect(self._redraw_candle_preview)
        self._refresh_sl_mode_fields("classic")
        self._refresh_sl_mode_fields("inverted")

    def _apply_stop_loss_combo_labels(self, field_name: str) -> None:
        labels = {
            logic.StopLossMode.CANDLE_EXTREME.value: "Signal candle low/high + buffer",
            logic.StopLossMode.FIXED_FROM_ENTRY.value: "Fixed distance from entry",
        }
        combo = self.field_widgets.get(field_name)
        if not isinstance(combo, QComboBox):
            return
        current = combo.currentData()
        if current is None:
            current = combo.currentText()
        combo.blockSignals(True)
        for i in range(combo.count()):
            val = combo.itemData(i)
            if val is None:
                val = combo.itemText(i)
            combo.setItemText(i, labels.get(str(val), str(val)))
        idx = combo.findData(current)
        if idx < 0 and current is not None:
            idx = combo.findData(str(current))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _sl_mode_is_fixed(self, widget_name: str) -> bool:
        combo = self.field_widgets.get(widget_name)
        if not isinstance(combo, QComboBox):
            return False
        raw = combo.currentData()
        if raw is None:
            raw = combo.currentText()
        return str(raw) == logic.StopLossMode.FIXED_FROM_ENTRY.value

    def _refresh_sl_mode_fields(self, side: str):
        if side == "classic":
            mode_name = "sl_mode"
            fixed_name = "sl_fixed_distance"
            buffer_names = ("buffer_mode", "sl_buffer_pct", "sl_buffer_flat")
        else:
            mode_name = "inverted_sl_mode"
            fixed_name = "inverted_sl_fixed_distance"
            buffer_names = ("inverted_buffer_mode", "inverted_sl_buffer_pct", "inverted_sl_buffer_flat")
        fixed_on = self._sl_mode_is_fixed(mode_name)
        for name in buffer_names:
            widget = self.field_widgets.get(name)
            label = self.field_labels.get(name)
            if widget is not None:
                widget.setEnabled(not fixed_on)
            if label is not None:
                label.setEnabled(not fixed_on)
        widget = self.field_widgets.get(fixed_name)
        label = self.field_labels.get(fixed_name)
        if widget is not None:
            widget.setEnabled(fixed_on)
        if label is not None:
            label.setEnabled(fixed_on)

    def _refresh_side_body_fields_for_wick(self, side: str, wick_required: bool):
        """When wick is off, Body % is a max cap; tolerance rows are unused."""
        prefix = f"{side}_"
        pct_label = self.field_labels.get(f"{prefix}body_pct")
        if pct_label is not None:
            pct_label.setText(
                "Body % (target)" if wick_required else "Max body % (wick off)"
            )
        for name, _, _, _ in (
            BUY_CONTEXT_BODY_FIELDS if side == "buy" else SELL_CONTEXT_BODY_FIELDS
        ):
            if name.endswith("body_pct"):
                continue
            widget = self.field_widgets.get(name)
            label = self.field_labels.get(name)
            if widget is not None:
                widget.setEnabled(wick_required)
            if label is not None:
                label.setEnabled(wick_required)
        if wick_required:
            self._apply_one_symmetric_enabled(
                f"{prefix}body_tol_is_symmetric",
                f"{prefix}body_tol",
                f"{prefix}body_tol_lower",
                f"{prefix}body_tol_upper",
            )

    def _apply_one_symmetric_enabled(
        self, sym_name: str, tol_name: str, lo_name: str, hi_name: str,
    ) -> None:
        sym_widget = self.field_widgets.get(sym_name)
        if sym_widget is None:
            return
        symmetric = sym_widget.isChecked()
        tol_widget = self.field_widgets.get(tol_name)
        if tol_widget is not None:
            tol_widget.setEnabled(symmetric)
            label = self.field_labels.get(tol_name)
            if label is not None:
                label.setEnabled(symmetric)
        for widget_name in (lo_name, hi_name):
            widget = self.field_widgets.get(widget_name)
            if widget is not None:
                widget.setEnabled(not symmetric)
            label = self.field_labels.get(widget_name)
            if label is not None:
                label.setEnabled(not symmetric)

    def _make_field_tab(self, field_defs, *defaults_objs, live_preview=False) -> QWidget:
        """
        Builds one scrollable tab page containing all fields in
        field_defs, reading defaults from the given dataclass instances.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)

        columns_per_row = 3
        for pair_col in range(columns_per_row):
            grid.setColumnStretch(pair_col * 2 + 1, 1)
            grid.setColumnMinimumWidth(pair_col * 2, 170)

        default_sources = list(defaults_objs)

        row = 0
        col_pair = 0
        for name, label_text, ftype, choices in field_defs:
            default_val = default_value_for_field(name, default_sources)
            if isinstance(default_val, Enum):
                default_val = default_val.value
            if default_val is None:
                default_val = ""

            col_base = col_pair * 2
            help_text = FIELD_HELP.get(name, label_text)
            label = QLabel(label_text)
            label.setObjectName("fieldLabel")
            label.setWordWrap(True)
            label.setToolTip(help_text)
            grid.addWidget(label, row, col_base)
            self.field_labels[name] = label

            if ftype == FIELD_TYPE_TEXT:
                widget = QLineEdit(str(default_val))
                widget.setToolTip(help_text)
                if live_preview:
                    widget.textChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            elif ftype == FIELD_TYPE_CHECK:
                widget = QCheckBox()
                widget.setChecked(bool(default_val))
                widget.setToolTip(help_text)
                if live_preview:
                    widget.stateChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            elif ftype == FIELD_TYPE_DROPDOWN:
                widget = QComboBox()
                _populate_enum_combo(widget, choices, default_val)
                widget.setToolTip(help_text)
                if live_preview:
                    widget.currentTextChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            else:
                continue

            self.field_widgets[name] = widget

            col_pair += 1
            if col_pair >= columns_per_row:
                col_pair = 0
                row += 1

        if col_pair != 0:
            row += 1
        grid.setRowStretch(row, 1)
        scroll.setWidget(inner)
        return scroll

    def _add_fields_to_grid(
        self,
        grid: QGridLayout,
        field_defs,
        *defaults_objs,
        live_preview: bool = False,
        columns_per_row: int = 3,
    ) -> None:
        """Place labeled parameter widgets into an existing grid."""
        for pair_col in range(columns_per_row):
            grid.setColumnStretch(pair_col * 2 + 1, 1)
            grid.setColumnMinimumWidth(pair_col * 2, 140)

        default_sources = list(defaults_objs)
        row = 0
        col_pair = 0
        for name, label_text, ftype, choices in field_defs:
            default_val = default_value_for_field(name, default_sources)
            if isinstance(default_val, Enum):
                default_val = default_val.value
            if default_val is None:
                default_val = ""

            col_base = col_pair * 2
            help_text = FIELD_HELP.get(name, label_text)
            label = QLabel(label_text)
            label.setObjectName("fieldLabel")
            label.setWordWrap(True)
            label.setToolTip(help_text)
            grid.addWidget(label, row, col_base)
            self.field_labels[name] = label

            if ftype == FIELD_TYPE_TEXT:
                widget = QLineEdit(str(default_val))
                widget.setToolTip(help_text)
                if live_preview:
                    widget.textChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            elif ftype == FIELD_TYPE_CHECK:
                widget = QCheckBox()
                widget.setChecked(bool(default_val))
                widget.setToolTip(help_text)
                if live_preview:
                    widget.stateChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            elif ftype == FIELD_TYPE_DROPDOWN:
                widget = QComboBox()
                _populate_enum_combo(widget, choices, default_val)
                widget.setToolTip(help_text)
                if live_preview:
                    widget.currentTextChanged.connect(self._redraw_candle_preview)
                grid.addWidget(widget, row, col_base + 1)
            else:
                continue

            self.field_widgets[name] = widget
            col_pair += 1
            if col_pair >= columns_per_row:
                col_pair = 0
                row += 1

    def _make_entry_exit_tab(self, defaults_strategy) -> QWidget:
        """Classic hammer settings in one box, inverted in another — not mixed in one grid."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        hint = QLabel(
            "Classic (long lower wick) and inverted (long upper wick) have their own entry and stop. "
            "HAMMER_HIGH on classic is the short-wick side; on inverted it is the long-wick tip."
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        classic_box = QGroupBox("Classic hammer")
        classic_grid = QGridLayout(classic_box)
        classic_grid.setContentsMargins(12, 16, 12, 12)
        classic_grid.setHorizontalSpacing(20)
        classic_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(classic_grid, CLASSIC_ENTRY_EXIT_FIELDS, defaults_strategy, live_preview=True)
        layout.addWidget(classic_box)

        inverted_box = QGroupBox("Inverted hammer")
        inverted_grid = QGridLayout(inverted_box)
        inverted_grid.setContentsMargins(12, 16, 12, 12)
        inverted_grid.setHorizontalSpacing(20)
        inverted_grid.setVerticalSpacing(10)
        self._add_fields_to_grid(inverted_grid, INVERTED_ENTRY_EXIT_FIELDS, defaults_strategy, live_preview=True)
        layout.addWidget(inverted_box)

        self._entry_exit_hint = hint
        self._entry_exit_classic_box = classic_box
        self._entry_exit_inverted_box = inverted_box
        layout.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _make_run_settings_tab(self, defaults_backtest) -> QWidget:
        """Backtest run options + optional saved parameter sets (not required to run)."""
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 8, 8, 8)

        preset_box = QGroupBox("Saved parameter sets (optional)")
        preset_layout = QVBoxLayout(preset_box)
        preset_hint = QLabel(
            "You can backtest with any settings — no preset needed. "
            "After a run looks good, save the current parameters here to reload the same setup later "
            "(for example before live trading). Loading a preset replaces the form; it is never required."
        )
        preset_hint.setObjectName("sectionHint")
        preset_hint.setWordWrap(True)
        preset_layout.addWidget(preset_hint)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Saved:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.preset_combo.setToolTip(
            "Optional. Pick a saved file and click Load — or ignore and run backtests as usual."
        )
        preset_row.addWidget(self.preset_combo, 1)
        load_preset_btn = QPushButton("Load preset")
        load_preset_btn.setObjectName("secondaryButton")
        load_preset_btn.clicked.connect(self._load_selected_preset)
        save_preset_btn = QPushButton("Save current settings…")
        save_preset_btn.setObjectName("secondaryButton")
        save_preset_btn.setToolTip("Snapshot whatever is on screen now (all tabs).")
        save_preset_btn.clicked.connect(self._save_preset_dialog)
        delete_preset_btn = QPushButton("Delete")
        delete_preset_btn.setObjectName("secondaryButton")
        delete_preset_btn.clicked.connect(self._delete_selected_preset)
        preset_row.addWidget(load_preset_btn)
        preset_row.addWidget(save_preset_btn)
        preset_row.addWidget(delete_preset_btn)
        preset_layout.addLayout(preset_row)
        layout.addWidget(preset_box)

        run_hint = QLabel(
            "Backtest execution: symbol, data folder, date range, position sizing (compare Fixed $ risk vs % equity vs fixed lots), "
            "overlap, commission/slippage. Signal rules come from the other parameter tabs + Timeframes. "
            "Bold labels below = fields used by the current Position Sizing Mode."
        )
        run_hint.setObjectName("sectionHint")
        run_hint.setWordWrap(True)
        layout.addWidget(run_hint)

        layout.addWidget(self._make_field_tab(BACKTEST_FIELDS, defaults_backtest), 1)
        QTimer.singleShot(0, lambda: self._refresh_preset_combo(keep_selection=False))
        QTimer.singleShot(0, self._wire_sizing_mode_highlights)
        return wrap

    def _wire_sizing_mode_highlights(self):
        combo = self.field_widgets.get("position_sizing_mode")
        if isinstance(combo, QComboBox):
            combo.currentTextChanged.connect(lambda _t: self._refresh_sizing_field_highlights())
        self._refresh_sizing_field_highlights()

    def _refresh_sizing_field_highlights(self):
        """Bold labels for sizing fields that apply to the selected mode (all fields stay editable)."""
        normal = "font-weight: normal; color: #3C4043;"
        active = "font-weight: bold; color: #137333;"
        mode_raw = ""
        combo = self.field_widgets.get("position_sizing_mode")
        if isinstance(combo, QComboBox):
            mode_raw = combo.currentText() or ""
        active_names = set(SIZING_MODE_ACTIVE_FIELDS.get(mode_raw, []))
        backtest_names = {n for n, _, _, _ in BACKTEST_FIELDS}
        for name, label in self.field_labels.items():
            if name not in backtest_names:
                continue
            if label is None:
                continue
            label.setStyleSheet(active if name in active_names else normal)

    def _collect_preset_backtest_settings(self) -> Dict[str, Any]:
        """Snapshot Run Settings for preset JSON (client can compare sizing setups)."""
        out: Dict[str, Any] = {}
        for name, _, ftype, _ in BACKTEST_FIELDS:
            widget = self.field_widgets.get(name)
            if widget is None:
                continue
            val = self._widget_value(widget)
            if ftype == FIELD_TYPE_CHECK:
                out[name] = bool(val)
            else:
                out[name] = "" if val is None else str(val)
        mode = str(out.get("position_sizing_mode", "FIXED_RISK_USD"))
        if mode == "FIXED_UNITS":
            out["plain_english"] = f"Backtest sizes each trade at {out.get('position_size', '?')} units/lots."
        elif mode == "PERCENT_OF_EQUITY":
            out["plain_english"] = (
                f"Backtest risks {out.get('risk_pct_of_equity', '?')}% of equity per trade "
                f"(start ${out.get('starting_capital', '?')}, cap {out.get('max_equity_multiple', '?')}×)."
            )
        else:
            out["plain_english"] = (
                f"Backtest risks ${out.get('fixed_risk_usd', '?')} per trade (fixed $ risk mode)."
            )
        out["applies_to"] = "backtest_only — live uses Live panel lot size"
        return out

    def _make_combined_timeframes_tab(
        self,
        defaults_strategy,
        defaults_context,
        defaults_doji_strategy,
    ) -> QWidget:
        """RR / max SL per timeframe, risk-limit toggles, and include-in-run checkboxes."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(14, 14, 14, 14)

        hint = QLabel(
            "Set reward:risk and max stop-loss per timeframe, choose which folders to include in a run, "
            "and use the risk toggles below to skip trades that exceed max SL or have invalid entry/SL."
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        risk_box = QGroupBox("Signal risk filters")
        risk_box.setObjectName("fieldGroup")
        risk_grid = QGridLayout(risk_box)
        risk_grid.setContentsMargins(12, 10, 12, 10)
        risk_grid.setHorizontalSpacing(24)
        risk_grid.setVerticalSpacing(8)
        self._add_fields_to_grid(
            risk_grid,
            RISK_CONTROL_FIELDS,
            defaults_strategy,
            defaults_context,
            defaults_doji_strategy,
            columns_per_row=2,
        )
        layout.addWidget(risk_box)

        session_box = QGroupBox("When to take trades")
        session_box.setObjectName("fieldGroup")
        session_layout = QVBoxLayout(session_box)
        session_hint = QLabel(
            "Choose <b>one</b>: trading sessions, or Indian time hours. "
            "IST converts IC Markets server time automatically (GMT+2 winter / GMT+3 summer). "
            "Same setting is used for backtest and live."
        )
        session_hint.setObjectName("sectionHint")
        session_hint.setWordWrap(True)
        session_hint.setTextFormat(Qt.RichText)
        session_layout.addWidget(session_hint)

        self.time_filter_mode_group = QButtonGroup(self)
        self.time_filter_mode_sessions = QRadioButton("Trading sessions (Asian / London / US)")
        self.time_filter_mode_sessions.setChecked(True)
        self.time_filter_mode_sessions.setToolTip(
            "Keep only entries in the sessions you tick (broker/CSV clock)."
        )
        self.time_filter_mode_ist = QRadioButton("Indian time (IST)")
        self.time_filter_mode_ist.setToolTip(
            "Keep only entries between From and To on the Indian clock."
        )
        self.time_filter_mode_group.addButton(self.time_filter_mode_sessions, 0)
        self.time_filter_mode_group.addButton(self.time_filter_mode_ist, 1)
        session_layout.addWidget(self.time_filter_mode_sessions)

        self.session_checks_row = QWidget()
        session_row = QHBoxLayout(self.session_checks_row)
        session_row.setContentsMargins(24, 0, 0, 8)
        session_row.setSpacing(24)
        for sess_name in sessions.SESSION_ORDER:
            win = sessions.SESSION_WINDOWS[sessions.SESSION_ORDER.index(sess_name)]
            cb = QCheckBox(sess_name)
            cb.setChecked(True)
            cb.setToolTip(f"{sess_name}: {win[1]}–{win[2]} on IC Markets server clock")
            self.session_enabled_widgets[sess_name] = cb
            session_row.addWidget(cb)
        session_row.addStretch()
        session_layout.addWidget(self.session_checks_row)

        session_layout.addWidget(self.time_filter_mode_ist)
        self.ist_filter_row = QWidget()
        ist_row = QHBoxLayout(self.ist_filter_row)
        ist_row.setContentsMargins(24, 0, 0, 0)
        ist_row.setSpacing(8)
        ist_row.addWidget(QLabel("From"))
        self.ist_time_start_edit = QLineEdit("09:15")
        self.ist_time_start_edit.setPlaceholderText("HH:MM")
        self.ist_time_start_edit.setMaximumWidth(70)
        self.ist_time_start_edit.setToolTip("Indian Standard Time start")
        ist_row.addWidget(self.ist_time_start_edit)
        ist_row.addWidget(QLabel("to"))
        self.ist_time_end_edit = QLineEdit("15:30")
        self.ist_time_end_edit.setPlaceholderText("HH:MM")
        self.ist_time_end_edit.setMaximumWidth(70)
        self.ist_time_end_edit.setToolTip("Indian Standard Time end")
        ist_row.addWidget(self.ist_time_end_edit)
        ist_row.addWidget(QLabel("IST"))
        ist_row.addStretch()
        session_layout.addWidget(self.ist_filter_row)

        # Keep old attribute names as no-ops / aliases so presets don't crash
        self.session_clock_combo = None
        self.broker_offset_combo = None
        self.ist_time_filter_cb = None
        self.broker_summer_cb = None

        self.time_filter_mode_sessions.toggled.connect(self._sync_time_filter_mode_ui)
        self.time_filter_mode_ist.toggled.connect(self._sync_time_filter_mode_ui)
        self._sync_time_filter_mode_ui()
        layout.addWidget(session_box)

        table = QTableWidget(len(TIMEFRAME_LABELS), 4)
        table.setHorizontalHeaderLabels(["Include", "Timeframe", "RR Multiple", "Max SL ($)"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        _configure_table_widget_mac(table)

        defaults = logic.DEFAULT_TIMEFRAME_SETTINGS
        for i, tf in enumerate(TIMEFRAME_LABELS):
            table.setRowHeight(i, 44)
            folder = TIMEFRAME_TO_FOLDER[tf]

            include_wrap = QWidget()
            include_layout = QHBoxLayout(include_wrap)
            include_layout.setContentsMargins(0, 0, 0, 0)
            include_layout.setAlignment(Qt.AlignCenter)
            default_checked = folder != "1min"
            include_cb = QCheckBox()
            include_cb.setChecked(default_checked)
            if folder == "1min":
                include_cb.setToolTip("1-minute data is large — opt in only if you need it.")
            include_layout.addWidget(include_cb)
            table.setCellWidget(i, 0, include_wrap)
            self.timeframe_enabled_widgets[folder] = include_cb

            tf_item = QTableWidgetItem(tf if tf != "1m" else "1m (slow)")
            tf_item.setFlags(tf_item.flags() & ~Qt.ItemIsEditable)
            tf_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(i, 1, tf_item)

            rr_edit = QLineEdit(f"{defaults[tf].rr_multiple:.1f}")
            rr_edit.setObjectName("tableNumberInput")
            rr_edit.setAlignment(Qt.AlignCenter)
            rr_edit.setToolTip(f"Reward:risk for {tf} — profit target = this × stop distance.")
            rr_edit.textChanged.connect(self._redraw_candle_preview)

            sl_edit = QLineEdit(f"{defaults[tf].max_sl_usd:g}")
            sl_edit.setObjectName("tableNumberInput")
            sl_edit.setAlignment(Qt.AlignCenter)
            sl_edit.setToolTip(f"Maximum $ stop-loss distance allowed on {tf}; wider signals are skipped.")
            sl_edit.textChanged.connect(self._redraw_candle_preview)

            table.setCellWidget(i, 2, rr_edit)
            table.setCellWidget(i, 3, sl_edit)
            self.timeframe_widgets[tf] = {"rr": rr_edit, "sl": sl_edit}

        table.setMinimumHeight(340)
        layout.addWidget(table)
        layout.addStretch()
        scroll.setWidget(inner)
        return scroll

    # ------------------------------------------------------------------
    # PREVIEW PANEL: split into two horizontal sub-tabs -- "Signal Shape"
    # (the raw candle + tolerance range) and "Pattern In Context" (the
    # per-timeframe trade-idea charts). Splitting it this way means
    # neither tab has to stack three big sections on top of each other,
    # so the panel fits comfortably without constant scrolling, and it
    # mirrors the same horizontal-tab pattern already used for Parameters.
    # ------------------------------------------------------------------
    def _build_preview_panel(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(10, 10, 10, 10)

        self._preview_trade_side = "BUY"
        self._preview_hammer_shape = "CLASSIC"
        layout.addWidget(self._build_preview_toolbar())

        sub_tabs = QTabWidget()
        layout.addWidget(sub_tabs, 1)

        sub_tabs.addTab(self._build_signal_shape_tab(), "Signal Shape")
        sub_tabs.addTab(self._build_pattern_context_tab(), "Pattern In Context")

        # Scroll wrapper: without it the one-line toolbar (BUY/SELL +
        # Classic/Inverted buttons) forces a ~714px minimum width and the
        # panel divider refuses to move — panels must always be resizable.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(wrap)
        return scroll

    def _sync_time_filter_mode_ui(self) -> None:
        """Show only the controls for the selected filter mode."""
        use_ist = bool(
            getattr(self, "time_filter_mode_ist", None)
            and self.time_filter_mode_ist.isChecked()
        )
        if hasattr(self, "session_checks_row"):
            self.session_checks_row.setEnabled(not use_ist)
            self.session_checks_row.setVisible(True)
        if hasattr(self, "ist_filter_row"):
            self.ist_filter_row.setEnabled(use_ist)
            self.ist_filter_row.setVisible(True)

    def _time_filter_mode(self) -> str:
        if getattr(self, "time_filter_mode_ist", None) and self.time_filter_mode_ist.isChecked():
            return "ist"
        return "sessions"

    def _collect_time_filter_kwargs(self) -> Dict[str, Any]:
        """
        Map the simple two-option UI onto backtest/live filter fields.
        Mode 'sessions' → Asian/London/US on broker clock.
        Mode 'ist' → IST start/end window only (IC Markets → IST auto by bar date).
        """
        mode = self._time_filter_mode()
        start = (
            self.ist_time_start_edit.text().strip()
            if hasattr(self, "ist_time_start_edit") else "09:15"
        ) or "09:15"
        end = (
            self.ist_time_end_edit.text().strip()
            if hasattr(self, "ist_time_end_edit") else "15:30"
        ) or "15:30"

        if mode == "ist":
            return {
                "sessions_enabled": list(sessions.SESSION_ORDER),
                "session_clock": sessions.CLOCK_BROKER,
                "broker_utc_offset_hours": None,  # auto IC Markets GMT+2/GMT+3 per bar
                "ist_time_filter_enabled": True,
                "ist_time_start": start,
                "ist_time_end": end,
                "time_filter_mode": "ist",
            }

        enabled = [
            name for name in sessions.SESSION_ORDER
            if self.session_enabled_widgets.get(name)
            and self.session_enabled_widgets[name].isChecked()
        ]
        return {
            "sessions_enabled": enabled,
            "session_clock": sessions.CLOCK_BROKER,
            "broker_utc_offset_hours": None,
            "ist_time_filter_enabled": False,
            "ist_time_start": start,
            "ist_time_end": end,
            "time_filter_mode": "sessions",
        }

    def _build_preview_toolbar(self) -> QWidget:
        """BUY vs SELL and classic vs inverted preview toggles (hammer + context charts)."""
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 6)
        outer_layout.setSpacing(6)

        intro = QLabel(
            "Use the toggles to preview how a valid signal looks for a BUY vs SELL and "
            "for classic (long lower wick) vs inverted (long upper wick). "
            "This mirrors logic.py: shape first, then candle color → direction from Parameters → Direction."
        )
        intro.setObjectName("sectionHint")
        intro.setWordWrap(True)
        self.preview_toolbar_intro = intro
        outer_layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Preview trade:"))
        self.preview_side_buy_btn = QPushButton("BUY (green)")
        self.preview_side_sell_btn = QPushButton("SELL (red)")
        for btn in (self.preview_side_buy_btn, self.preview_side_sell_btn):
            btn.setCheckable(True)
            btn.setObjectName("previewSideToggle")
        self.preview_side_buy_btn.setChecked(True)
        self.preview_side_buy_btn.clicked.connect(lambda: self._set_preview_trade_side("BUY"))
        self.preview_side_sell_btn.clicked.connect(lambda: self._set_preview_trade_side("SELL"))
        row.addWidget(self.preview_side_buy_btn)
        row.addWidget(self.preview_side_sell_btn)

        row.addSpacing(16)
        self.preview_hammer_shape_row = QWidget()
        shape_row = QHBoxLayout(self.preview_hammer_shape_row)
        shape_row.setContentsMargins(0, 0, 0, 0)
        shape_row.addWidget(QLabel("Preview shape:"))
        self.preview_shape_classic_btn = QPushButton("Classic hammer")
        self.preview_shape_inverted_btn = QPushButton("Inverted hammer")
        for btn in (self.preview_shape_classic_btn, self.preview_shape_inverted_btn):
            btn.setCheckable(True)
            btn.setObjectName("previewSideToggle")
        self.preview_shape_classic_btn.setChecked(True)
        self.preview_shape_classic_btn.clicked.connect(lambda: self._set_preview_hammer_shape("CLASSIC"))
        self.preview_shape_inverted_btn.clicked.connect(lambda: self._set_preview_hammer_shape("INVERTED"))
        shape_row.addWidget(self.preview_shape_classic_btn)
        shape_row.addWidget(self.preview_shape_inverted_btn)
        row.addWidget(self.preview_hammer_shape_row)
        row.addStretch()
        outer_layout.addLayout(row)

        self.preview_summary_label = QLabel("")
        self.preview_summary_label.setWordWrap(True)
        self.preview_summary_label.setObjectName("sectionHint")
        outer_layout.addWidget(self.preview_summary_label)

        self._sync_preview_toolbar_for_pattern()
        return outer

    def _sync_preview_toolbar_for_pattern(self):
        """Hammer vs Doji: show the right preview controls and refresh summary."""
        if not hasattr(self, "preview_toolbar_intro"):
            return
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else "Hammer"
        is_doji = pattern == "Doji"
        if hasattr(self, "preview_hammer_shape_row"):
            self.preview_hammer_shape_row.setVisible(not is_doji)
        if is_doji:
            self.preview_toolbar_intro.setText(
                "Preview how a doji signal looks for BUY (green) vs SELL (red). "
                "Shape comes from Parameters → Doji Style and wick/body fields; "
                "direction comes from Doji Direction (same rules as backtest and live). "
                "Pattern In Context updates with these toggles."
            )
        else:
            self.preview_toolbar_intro.setText(
                "Preview hammer signals: BUY vs SELL candle color, classic vs inverted wick shape. "
                "Matches logic.py and Parameters → Direction. Pattern In Context uses the same settings."
            )
            self._sync_preview_shape_toggle_enabled()

    def _set_preview_trade_side(self, side: str):
        self._preview_trade_side = "SELL" if side == "SELL" else "BUY"
        self.preview_side_buy_btn.setChecked(self._preview_trade_side == "BUY")
        self.preview_side_sell_btn.setChecked(self._preview_trade_side == "SELL")
        self._redraw_candle_preview()

    def _set_preview_hammer_shape(self, shape: str):
        self._preview_hammer_shape = "INVERTED" if shape == "INVERTED" else "CLASSIC"
        self.preview_shape_classic_btn.setChecked(self._preview_hammer_shape == "CLASSIC")
        self.preview_shape_inverted_btn.setChecked(self._preview_hammer_shape == "INVERTED")
        self._redraw_candle_preview()

    def _sync_preview_shape_toggle_enabled(self):
        if not hasattr(self, "preview_shape_classic_btn"):
            return
        eff = self._effective_hammer_wick_side_value()
        if eff == "EITHER":
            self.preview_shape_classic_btn.setEnabled(True)
            self.preview_shape_inverted_btn.setEnabled(True)
        elif eff == "UPPER":
            self._preview_hammer_shape = "INVERTED"
            self.preview_shape_classic_btn.setEnabled(False)
            self.preview_shape_inverted_btn.setEnabled(True)
            self.preview_shape_classic_btn.setChecked(False)
            self.preview_shape_inverted_btn.setChecked(True)
        else:
            self._preview_hammer_shape = "CLASSIC"
            self.preview_shape_classic_btn.setEnabled(True)
            self.preview_shape_inverted_btn.setEnabled(False)
            self.preview_shape_inverted_btn.setChecked(False)
            self.preview_shape_classic_btn.setChecked(True)

    def _hammer_preview_trade_direction(self) -> logic.TradeDirection:
        side = getattr(self, "_preview_trade_side", "BUY")
        return logic.TradeDirection.SELL if side == "SELL" else logic.TradeDirection.BUY

    def _update_preview_summary_label(
        self,
        *,
        pattern: str,
        variant: str,
        is_green: bool,
        trade_side: str,
        detection_line: str,
    ):
        if not hasattr(self, "preview_summary_label"):
            return
        color_word = "Green" if is_green else "Red"
        if pattern == "Doji":
            self.preview_summary_label.setText(
                f"Preview: {color_word} doji · {trade_side} trade · {detection_line}"
            )
        elif pattern == "Hammer with candles":
            self.preview_summary_label.setText(
                f"Preview: {variant} · {color_word} candle · {trade_side} trade · {detection_line}"
            )
        else:
            allowed = self._hammer_preview_direction_allowed(variant, trade_side)
            allow_note = "" if allowed else " (this combo is set to NO)"
            self.preview_summary_label.setText(
                f"Preview: {variant} hammer · {color_word} candle · {trade_side} trade · "
                f"{detection_line}{allow_note}"
            )

    def _hammer_preview_direction_allowed(self, variant: str, trade_side: str) -> bool:
        cfg = self._build_hammer_strategy_config()
        hv = logic.HammerVariant.INVERTED if variant == "INVERTED" else logic.HammerVariant.CLASSIC
        for c in (logic.CandleColor.GREEN, logic.CandleColor.RED):
            if logic.hammer_trade_action(cfg, hv, c).value == trade_side:
                return True
        return False

    def _build_signal_shape_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        preview_box = QGroupBox("Live Candle Preview")
        preview_layout = QVBoxLayout(preview_box)
        candle_row = QHBoxLayout()
        candle_row.addStretch(1)
        self.candle_widget = CandleWidget()
        self.candle_widget.setMinimumSize(120, 180)
        self.candle_widget.setMaximumWidth(240)
        self.candle_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        candle_row.addWidget(self.candle_widget)
        candle_row.addStretch(1)
        preview_layout.addLayout(candle_row, 1)
        layout.addWidget(preview_box, 1)

        tolerance_box = QGroupBox("Tolerance Range Preview")
        tolerance_layout = QVBoxLayout(tolerance_box)

        hint = QLabel("Loosest and tightest candles your current tolerance "
                       "settings will still accept as a valid hammer.")
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        self.tolerance_hint_label = hint
        tolerance_layout.addWidget(hint)

        pair_row = QHBoxLayout()
        min_col = QVBoxLayout()
        self.tolerance_widget_min = CandleWidget()
        self.tolerance_widget_min.setMinimumSize(100, 130)
        min_col.addWidget(self.tolerance_widget_min)
        self.tolerance_label_min = QLabel("")
        self.tolerance_label_min.setAlignment(Qt.AlignCenter)
        self.tolerance_label_min.setStyleSheet("font-size: 11px;")
        min_col.addWidget(self.tolerance_label_min)
        pair_row.addLayout(min_col)

        max_col = QVBoxLayout()
        self.tolerance_widget_max = CandleWidget()
        self.tolerance_widget_max.setMinimumSize(100, 130)
        max_col.addWidget(self.tolerance_widget_max)
        self.tolerance_label_max = QLabel("")
        self.tolerance_label_max.setAlignment(Qt.AlignCenter)
        self.tolerance_label_max.setStyleSheet("font-size: 11px;")
        max_col.addWidget(self.tolerance_label_max)
        pair_row.addLayout(max_col)

        tolerance_layout.addLayout(pair_row)
        layout.addWidget(tolerance_box)

        scroll.setWidget(wrap)
        return scroll

    def _build_pattern_context_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # ---- Pattern in Context: one tab per timeframe, showing the
        # signal candle inside an illustrative trade setup (entry, stop,
        # profit target) that also reflects that timeframe's own RR
        # multiple / max SL from the Timeframe Table tab. This is what
        # answers "does everything I'm setting actually line up" --
        # not just an isolated candle shape.
        context_hint = QLabel(
            "ENTRY / SL / TP follow Entry / Exit for the preview side. "
            "Hammer with candles: colour the signal candle and the prior N bars from the Context tab. "
            "Hammer and Doji colour only the signal (single-candle patterns). "
            "The next bar is the entry candle — it stays grey. Trail bars after that stay grey. "
            "Every timeframe tab uses the same N; only noise/RR/max SL change per tab."
        )
        context_hint.setObjectName("sectionHint")
        context_hint.setWordWrap(True)
        layout.addWidget(context_hint)

        self.context_tabs = QTabWidget()
        self.context_tabs.setObjectName("contextTimeframeTabs")
        self.context_charts: Dict[str, PatternContextChart] = {}
        for tf in TIMEFRAME_LABELS:
            chart = PatternContextChart()
            chart.setMinimumHeight(230)
            self.context_charts[tf] = chart
            self.context_tabs.addTab(chart, tf)
        layout.addWidget(self.context_tabs, 1)

        indicator_row = QHBoxLayout()
        indicator_row.addWidget(QLabel("Indicator:"))
        self.preview_indicator_combo = QComboBox()
        self.preview_indicator_combo.setMinimumWidth(140)
        self.preview_indicator_combo.addItem("— add indicator —", "")
        for ind_id, meta in sorted(INDICATOR_REGISTRY.items(), key=lambda kv: kv[1]["label"]):
            self.preview_indicator_combo.addItem(meta["label"], ind_id)
        add_indicator_btn = QPushButton("Add")
        add_indicator_btn.setObjectName("secondaryButton")
        add_indicator_btn.setToolTip("Add an indicator and configure it on the Indicators tab.")
        add_indicator_btn.clicked.connect(self._on_preview_add_indicator)
        indicator_row.addWidget(self.preview_indicator_combo)
        indicator_row.addWidget(add_indicator_btn)

        save_chart_btn = QPushButton("Save Chart as Image")
        save_chart_btn.setObjectName("secondaryButton")
        save_chart_btn.setToolTip("Saves the currently viewed timeframe's chart as a PNG, for reports or sharing with the client.")
        save_chart_btn.clicked.connect(self._save_context_chart)
        indicator_row.addWidget(save_chart_btn)

        indicator_row.addStretch()
        layout.addLayout(indicator_row)

        scroll.setWidget(wrap)
        return scroll

    def _save_context_chart(self):
        chart = self.context_tabs.currentWidget() if hasattr(self, "context_tabs") else None
        if chart is None:
            QMessageBox.information(self, "Nothing to Save", "No chart is currently visible.")
            return

        snapshots_dir = os.path.join(DEFAULT_OUTPUT_DIR, "chart_snapshots")
        try:
            os.makedirs(snapshots_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Save Failed", f"Could not create the snapshots folder:\n{e}")
            return

        tf_label = getattr(chart, "timeframe_label", "chart")
        filename = f"pattern_in_context_{tf_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path = os.path.join(snapshots_dir, filename)

        pixmap = chart.grab()
        if pixmap.save(path, "PNG"):
            self._set_app_status(f"Chart saved: {path}")
        else:
            QMessageBox.warning(self, "Save Failed", "Could not save the chart image.")

    # ------------------------------------------------------------------
    # LIVE CANDLE + TOLERANCE PREVIEW REDRAW
    # ------------------------------------------------------------------
    def _get_field_float(self, name: str, default: Optional[float] = None) -> Optional[float]:
        widget = self.field_widgets.get(name)
        if widget is None:
            return default
        try:
            if isinstance(widget, QLineEdit):
                text = widget.text().strip()
                if not text:
                    return default
                return float(text)
            return float(widget.text())
        except (ValueError, AttributeError):
            return default

    def _get_field_str(self, name: str) -> Optional[str]:
        widget = self.field_widgets.get(name)
        if widget is None:
            return None
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QLineEdit):
            return widget.text()
        return None

    def _preview_bounds_from_ratios(self, ratios) -> tuple[float, float, float, float, float, float]:
        """Use the same asymmetric/symmetric bounds logic as the engine."""
        body_lo, body_hi = ratios.body_bounds()
        dominant_lo, dominant_hi = ratios.dominant_wick_bounds()
        small_lo, small_hi = ratios.small_wick_bounds()
        return body_lo, body_hi, dominant_lo, dominant_hi, small_lo, small_hi

    def _redraw_candle_preview(self, *args):
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else "Hammer"
        if pattern == "Hammer with candles":
            self._redraw_hammer_context_preview()
        elif pattern == "Doji":
            self._redraw_doji_candle_preview()
        else:
            self._redraw_hammer_candle_preview()

    def _redraw_hammer_context_preview(self):
        """Preview BUY classic or SELL inverted shape for Hammer with candles."""
        self._sync_preview_shape_toggle_enabled()
        trade_side = getattr(self, "_preview_trade_side", "BUY")
        is_buy = trade_side != "SELL"
        prefix = "buy_" if is_buy else "sell_"
        body_pct = self._get_field_float(f"{prefix}body_pct")
        dominant_pct = self._get_field_float(f"{prefix}dominant_wick_pct")
        small_pct = self._get_field_float(f"{prefix}small_wick_pct")
        if body_pct is None:
            return
        if dominant_pct is None:
            dominant_pct = 60.0
        if small_pct is None:
            small_pct = 20.0

        try:
            cfg = self._build_hammer_context_strategy_config()
        except Exception:
            cfg = hammer_context_logic.HammerContextConfig()

        require_wick = cfg.buy_require_wick if is_buy else cfg.sell_require_wick
        shape_choice = "CLASSIC" if is_buy else "INVERTED"
        self._preview_hammer_shape = shape_choice

        trade_dir = (
            logic.TradeDirection.BUY if is_buy else logic.TradeDirection.SELL
        )
        is_green = is_buy
        ratios = cfg.buy_hammer_ratios if is_buy else cfg.sell_hammer_ratios
        shape_cfg = cfg.to_buy_shape_config() if is_buy else cfg.to_sell_shape_config()
        draw_wick = logic.preview_draw_wick_side(shape_cfg, shape_choice)
        variant = "body ≤ max, wick ignored" if not require_wick else logic.variant_name_for_draw_wick(draw_wick)
        body_lo, body_hi, dominant_lo, dominant_hi, small_lo, small_hi = (
            self._preview_bounds_from_ratios(ratios)
        )
        body_cap = hammer_context_logic.body_only_max_pct(ratios)

        if not require_wick:
            leftover = max(0.0, 100.0 - body_cap) / 2.0
            draw_wick = "BODY"
            dominant_pct = leftover
            small_pct = leftover
            self.candle_widget.set_shape(body_cap, leftover, leftover, draw_wick, is_green)
            leftover_tiny = max(0.0, 100.0 - min(3.0, body_cap)) / 2.0
            self.tolerance_widget_min.set_shape(body_cap, leftover, leftover, draw_wick, is_green)
            self.tolerance_label_min.setText(
                f"Largest allowed body\nBody ≤ {body_cap:.0f}%  (wick ignored)"
            )
            self.tolerance_widget_max.set_shape(min(3.0, body_cap), leftover_tiny, leftover_tiny, draw_wick, is_green)
            self.tolerance_label_max.setText(
                f"Smaller body still valid\nBody ≤ {body_cap:.0f}%  (prev candle sets BUY/SELL)"
            )
        else:
            self.candle_widget.set_shape(body_pct, dominant_pct, small_pct, draw_wick, is_green)
            self.tolerance_widget_min.set_shape(body_hi, dominant_lo, small_hi, draw_wick, is_green)
            self.tolerance_label_min.setText(f"Loosest valid shape\nBody: {body_hi:.0f}%  Wick: {dominant_lo:.0f}%")
            self.tolerance_widget_max.set_shape(body_lo, dominant_hi, small_lo, draw_wick, is_green)
            self.tolerance_label_max.setText(f"Tightest valid shape\nBody: {body_lo:.0f}%  Wick: {dominant_hi:.0f}%")

        n = cfg.lookback_candles
        det = hammer_context_logic.describe_hammer_context_rules(cfg)
        if hasattr(self, "tolerance_hint_label"):
            self.tolerance_hint_label.setText(
                f"Hammer with candles — preview {trade_side}. Prior {n} candle(s) must respect hammer "
                f"{'low' if is_buy else 'high'}. BUY/SELL each have own Body/Wicks rows. {det}"
            )

        self._update_preview_summary_label(
            pattern="Hammer with candles",
            variant=variant,
            is_green=is_green,
            trade_side=trade_side,
            detection_line=det,
        )
        ee = self._preview_entry_exit_kwargs(False, "INVERTED" if not is_buy else "CLASSIC")
        self._update_context_charts(
            body_pct, dominant_pct, small_pct, draw_wick, is_green, is_doji=False,
            hammer_variant="INVERTED" if not is_buy else "CLASSIC", preview_trade_side=trade_side,
            context_lookback=n,
            **ee,
        )

    def _redraw_hammer_candle_preview(self):
        self._sync_preview_shape_toggle_enabled()
        body_pct = self._get_field_float("body_pct")
        dominant_pct = self._get_field_float("dominant_wick_pct")
        small_pct = self._get_field_float("small_wick_pct")

        if None in (body_pct, dominant_pct, small_pct):
            return

        try:
            cfg = self._build_hammer_strategy_config()
        except Exception:
            cfg = logic.StrategyConfig()

        trade_dir = self._hammer_preview_trade_direction()
        trade_side = trade_dir.value
        shape_choice = getattr(self, "_preview_hammer_shape", "CLASSIC")
        is_green = logic.preview_candle_is_green(trade_dir, cfg, shape_choice)
        draw_wick = logic.preview_draw_wick_side(cfg, shape_choice)
        variant = logic.variant_name_for_draw_wick(draw_wick)

        self.candle_widget.set_shape(body_pct, dominant_pct, small_pct, draw_wick, is_green)
        body_lo, body_hi, dominant_lo, dominant_hi, small_lo, small_hi = (
            self._preview_bounds_from_ratios(cfg.hammer_ratios)
        )

        self.tolerance_widget_min.set_shape(body_hi, dominant_lo, small_hi, draw_wick, is_green)
        self.tolerance_label_min.setText(f"Loosest valid shape\nBody: {body_hi:.0f}%  Wick: {dominant_lo:.0f}%")

        self.tolerance_widget_max.set_shape(body_lo, dominant_hi, small_lo, draw_wick, is_green)
        self.tolerance_label_max.setText(f"Tightest valid shape\nBody: {body_lo:.0f}%  Wick: {dominant_hi:.0f}%")

        if hasattr(self, "tolerance_hint_label"):
            self.tolerance_hint_label.setText(
                f"Tolerance band for a valid {variant.lower()} hammer ({draw_wick.lower()} dominant wick). "
                f"Engine: {logic.describe_hammer_detection(cfg)}"
            )

        self._update_preview_summary_label(
            pattern="Hammer",
            variant=variant,
            is_green=is_green,
            trade_side=trade_side,
            detection_line=logic.describe_hammer_detection(cfg),
        )

        self._update_context_charts(
            body_pct, dominant_pct, small_pct, draw_wick, is_green, is_doji=False,
            hammer_variant=variant, preview_trade_side=trade_side,
            context_lookback=0,
        )

    def _doji_preview_is_green(self, lower_wick_pct: float, upper_wick_pct: float) -> bool:
        """Preview BUY/SELL colouring from doji direction settings."""
        mode = self._get_field_str("doji_direction_mode") or doji_logic.DojiDirectionMode.WICK_BIAS.value
        if mode == doji_logic.DojiDirectionMode.CANDLE_COLOR.value:
            allow_g = True
            allow_r = True
            w_g = self.field_widgets.get("doji_allow_green_trades")
            w_r = self.field_widgets.get("doji_allow_red_trades")
            if isinstance(w_g, QCheckBox):
                allow_g = w_g.isChecked()
            if isinstance(w_r, QCheckBox):
                allow_r = w_r.isChecked()
            green_dir = self._get_field_str("doji_green_direction") or logic.TradeDirection.BUY.value
            if allow_g and green_dir == logic.TradeDirection.BUY.value:
                return True
            if allow_r:
                red_dir = self._get_field_str("doji_red_direction") or logic.TradeDirection.SELL.value
                return red_dir == logic.TradeDirection.BUY.value
            return green_dir == logic.TradeDirection.BUY.value
        if mode == doji_logic.DojiDirectionMode.FIXED_BUY.value:
            return True
        if mode == doji_logic.DojiDirectionMode.FIXED_SELL.value:
            return False
        threshold = self._get_field_float("doji_wick_bias_threshold_pct", 5.0)
        diff = lower_wick_pct - upper_wick_pct
        if diff > threshold:
            return True
        if diff < -threshold:
            return False
        fallback = self._get_field_str("doji_fallback_direction") or logic.TradeDirection.BUY.value
        return fallback == logic.TradeDirection.BUY.value

    def _compute_doji_preview_shape(self):
        """
        Map the selected Doji Style (+ wick fields) to candle proportions for
        live preview and Pattern In Context. Each style is visually distinct.
        """
        shape_defaults = doji_logic.DojiRatioConfig()
        max_body = self._get_field_float("doji_max_body_pct", shape_defaults.max_body_pct)
        min_upper = self._get_field_float("doji_min_upper_wick_pct", shape_defaults.min_upper_wick_pct)
        min_lower = self._get_field_float("doji_min_lower_wick_pct", shape_defaults.min_lower_wick_pct)
        dom_wick = self._get_field_float("doji_dominant_wick_pct", shape_defaults.dominant_wick_pct)
        small_wick = self._get_field_float("doji_small_wick_pct", shape_defaults.small_wick_pct)
        doji_style = self._get_field_str("doji_style") or doji_logic.DojiStyle.ANY.value

        if doji_style == doji_logic.DojiStyle.DRAGONFLY.value:
            return (
                max_body, dom_wick, small_wick, "LOWER", True, doji_style,
            )
        if doji_style == doji_logic.DojiStyle.GRAVESTONE.value:
            return (
                max_body, dom_wick, small_wick, "UPPER", False, doji_style,
            )
        if doji_style == doji_logic.DojiStyle.LONG_LEGGED.value:
            leg = dom_wick
            is_green = self._doji_preview_is_green(leg, leg)
            return (min(max_body, 3.0), leg, leg, "LOWER", is_green, doji_style)
        if doji_style == doji_logic.DojiStyle.CLASSIC.value:
            is_green = self._doji_preview_is_green(min_lower, min_upper)
            return (max_body, min_lower, min_upper, "LOWER", is_green, doji_style)
        # ANY — small body, short balanced wicks (looser than CLASSIC minimums)
        any_leg = max(5.0, min(max_body * 2.0, (min_upper + min_lower) * 0.35))
        is_green = self._doji_preview_is_green(any_leg, any_leg)
        return (max_body, any_leg, any_leg, "LOWER", is_green, doji_style)

    def _redraw_doji_candle_preview(self):
        shape_defaults = doji_logic.DojiRatioConfig()
        (
            body_pct, dominant_pct, small_pct, wick_side, is_green, doji_style,
        ) = self._compute_doji_preview_shape()

        trade_side = getattr(self, "_preview_trade_side", "BUY")
        trade_dir = logic.TradeDirection.SELL if trade_side == "SELL" else logic.TradeDirection.BUY
        try:
            doji_cfg = self._build_doji_strategy_config()
            is_green = doji_logic.preview_candle_is_green(trade_dir, doji_cfg)
        except Exception:
            is_green = trade_side == "BUY"

        max_body = self._get_field_float("doji_max_body_pct", shape_defaults.max_body_pct)
        max_body_tol = self._get_field_float("doji_max_body_tol", shape_defaults.max_body_tol)
        min_upper = self._get_field_float("doji_min_upper_wick_pct", shape_defaults.min_upper_wick_pct)
        min_lower = self._get_field_float("doji_min_lower_wick_pct", shape_defaults.min_lower_wick_pct)
        min_wick_tol = self._get_field_float("doji_min_wick_tol", shape_defaults.min_wick_tol)

        self.candle_widget.set_shape(body_pct, dominant_pct, small_pct, wick_side, is_green)

        def clamp(v):
            return max(0.0, min(100.0, v))

        body_lo = clamp(max_body - max_body_tol)
        body_hi = clamp(max_body + max_body_tol)
        wick_lo = clamp(min(min_upper, min_lower) - min_wick_tol)
        wick_hi = clamp(max(min_upper, min_lower) + min_wick_tol)

        if doji_style == doji_logic.DojiStyle.DRAGONFLY.value:
            dom_tol = self._get_field_float("doji_dominant_wick_tol", shape_defaults.dominant_wick_tol)
            sm_tol = self._get_field_float("doji_small_wick_tol", shape_defaults.small_wick_tol)
            sm_w = self._get_field_float("doji_small_wick_pct", shape_defaults.small_wick_pct)
            self.tolerance_widget_min.set_shape(
                body_hi, clamp(dominant_pct - dom_tol), clamp(sm_w + sm_tol), wick_side, is_green,
            )
            self.tolerance_label_min.setText(f"Loosest ({doji_style})\nBody: {body_hi:.0f}%")
            self.tolerance_widget_max.set_shape(
                body_lo, clamp(dominant_pct + dom_tol), clamp(sm_w - sm_tol), wick_side, is_green,
            )
            self.tolerance_label_max.setText(f"Tightest ({doji_style})\nBody: {body_lo:.0f}%")
        elif doji_style == doji_logic.DojiStyle.GRAVESTONE.value:
            dom_tol = self._get_field_float("doji_dominant_wick_tol", shape_defaults.dominant_wick_tol)
            sm_tol = self._get_field_float("doji_small_wick_tol", shape_defaults.small_wick_tol)
            dom_w = self._get_field_float("doji_dominant_wick_pct", shape_defaults.dominant_wick_pct)
            sm_w = self._get_field_float("doji_small_wick_pct", shape_defaults.small_wick_pct)
            self.tolerance_widget_min.set_shape(body_hi, clamp(dom_w - dom_tol), clamp(sm_w + sm_tol), "UPPER", is_green)
            self.tolerance_label_min.setText(f"Loosest ({doji_style})\nBody: {body_hi:.0f}%")
            self.tolerance_widget_max.set_shape(body_lo, clamp(dom_w + dom_tol), clamp(sm_w - sm_tol), "UPPER", is_green)
            self.tolerance_label_max.setText(f"Tightest ({doji_style})\nBody: {body_lo:.0f}%")
        else:
            self.tolerance_widget_min.set_shape(body_hi, wick_lo, wick_lo, wick_side, is_green)
            self.tolerance_label_min.setText(f"Loosest ({doji_style})\nBody: {body_hi:.0f}%  Wick: {wick_lo:.0f}%")
            self.tolerance_widget_max.set_shape(body_lo, wick_hi, wick_hi, wick_side, is_green)
            self.tolerance_label_max.setText(f"Tightest ({doji_style})\nBody: {body_lo:.0f}%  Wick: {wick_hi:.0f}%")

        self._update_context_charts(
            body_pct, dominant_pct, small_pct, wick_side, is_green,
            is_doji=True, doji_style=doji_style,
            preview_trade_side=trade_side,
            context_lookback=0,
        )
        mode = self._get_field_str("doji_direction_mode") or "WICK_BIAS"
        try:
            det = doji_logic.describe_doji_detection(self._build_doji_strategy_config())
        except Exception:
            det = f"Doji direction mode: {mode}"
        self._update_preview_summary_label(
            pattern="Doji",
            variant=doji_style or "ANY",
            is_green=is_green,
            trade_side=trade_side,
            detection_line=det,
        )

        if hasattr(self, "tolerance_hint_label"):
            self.tolerance_hint_label.setText(
                f"Tolerance band for doji style {doji_style}. {det}"
            )

    def _preview_entry_exit_kwargs(self, is_doji: bool, hammer_variant: str) -> dict:
        """Classic vs inverted Entry / Exit fields for Pattern In Context."""
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else "Hammer"
        if pattern == "Hammer with candles":
            try:
                cfg = self._build_hammer_context_strategy_config()
            except Exception:
                cfg = hammer_context_logic.HammerContextConfig()
            use_sell = str(hammer_variant).upper() == "INVERTED"
            if use_sell:
                return {
                    "entry_rule": cfg.inverted_entry_rule,
                    "entry_offset": cfg.inverted_entry_offset,
                    "sl_mode": cfg.inverted_sl_mode,
                    "sl_fixed_distance": cfg.inverted_sl_fixed_distance,
                    "buffer_mode": cfg.inverted_buffer_mode,
                    "sl_buffer_pct": cfg.inverted_sl_buffer_pct,
                    "sl_buffer_flat": cfg.inverted_sl_buffer_flat,
                }
            return {
                "entry_rule": cfg.entry_rule,
                "entry_offset": cfg.entry_offset,
                "sl_mode": cfg.sl_mode,
                "sl_fixed_distance": cfg.sl_fixed_distance,
                "buffer_mode": cfg.buffer_mode,
                "sl_buffer_pct": cfg.sl_buffer_pct,
                "sl_buffer_flat": cfg.sl_buffer_flat,
            }
        use_inverted = (not is_doji) and str(hammer_variant).upper() == "INVERTED"
        defaults = logic.StrategyConfig()
        if use_inverted:
            rule = self._read_ui_field(
                "inverted_entry_rule", defaults.inverted_entry_rule, FIELD_TYPE_DROPDOWN,
            )
            offset = self._read_ui_field(
                "inverted_entry_offset", defaults.inverted_entry_offset, FIELD_TYPE_TEXT,
            )
            sl_mode = self._read_ui_field(
                "inverted_sl_mode", defaults.inverted_sl_mode, FIELD_TYPE_DROPDOWN,
            )
            fixed = self._read_ui_field(
                "inverted_sl_fixed_distance", defaults.inverted_sl_fixed_distance, FIELD_TYPE_TEXT,
            )
            buf = self._read_ui_field(
                "inverted_buffer_mode", defaults.inverted_buffer_mode, FIELD_TYPE_DROPDOWN,
            )
            pct = self._read_ui_field(
                "inverted_sl_buffer_pct", defaults.inverted_sl_buffer_pct, FIELD_TYPE_TEXT,
            )
            flat = self._read_ui_field(
                "inverted_sl_buffer_flat", defaults.inverted_sl_buffer_flat, FIELD_TYPE_TEXT,
            )
        else:
            rule = self._read_ui_field("entry_rule", defaults.entry_rule, FIELD_TYPE_DROPDOWN)
            offset = self._read_ui_field("entry_offset", defaults.entry_offset, FIELD_TYPE_TEXT)
            sl_mode = self._read_ui_field("sl_mode", defaults.sl_mode, FIELD_TYPE_DROPDOWN)
            fixed = self._read_ui_field("sl_fixed_distance", defaults.sl_fixed_distance, FIELD_TYPE_TEXT)
            buf = self._read_ui_field("buffer_mode", defaults.buffer_mode, FIELD_TYPE_DROPDOWN)
            pct = self._read_ui_field("sl_buffer_pct", defaults.sl_buffer_pct, FIELD_TYPE_TEXT)
            flat = self._read_ui_field("sl_buffer_flat", defaults.sl_buffer_flat, FIELD_TYPE_TEXT)
        sl_mode_val = sl_mode.value if isinstance(sl_mode, logic.StopLossMode) else str(sl_mode)
        return {
            "entry_rule": rule.value if hasattr(rule, "value") else rule,
            "entry_offset": float(offset or 0.0),
            "sl_mode": sl_mode_val,
            "sl_fixed_distance": float(fixed or 0.0),
            "buffer_mode": buf.value if hasattr(buf, "value") else buf,
            "sl_buffer_pct": float(pct or 0.0),
            "sl_buffer_flat": float(flat or 0.0),
        }

    def _update_context_charts(
        self, body_pct, dominant_pct, small_pct, wick_side, is_green, is_doji: bool = False,
        doji_style: str = "",
        hammer_variant: str = "CLASSIC",
        preview_trade_side: str = "BUY",
        context_lookback: int = 5,
        **entry_exit_kwargs,
    ):
        defaults = logic.DEFAULT_TIMEFRAME_SETTINGS
        for tf, chart in getattr(self, "context_charts", {}).items():
            tf_widgets = self.timeframe_widgets.get(tf)
            try:
                rr_multiple = float(tf_widgets["rr"].text()) if tf_widgets else defaults[tf].rr_multiple
            except (ValueError, KeyError, AttributeError):
                rr_multiple = defaults[tf].rr_multiple
            try:
                max_sl_usd = float(tf_widgets["sl"].text()) if tf_widgets else defaults[tf].max_sl_usd
            except (ValueError, KeyError, AttributeError):
                max_sl_usd = defaults[tf].max_sl_usd

            ind = self._indicator_preview_state(
                is_green, wick_side, is_doji,
                hammer_variant=hammer_variant,
                preview_trade_side=preview_trade_side,
            )
            chart.set_data(
                body_pct, dominant_pct, small_pct, wick_side, is_green,
                rr_multiple, max_sl_usd, tf, is_doji=is_doji, doji_style=doji_style,
                preview_trade_side=preview_trade_side,
                context_lookback=context_lookback,
                **ind,
                **(entry_exit_kwargs if entry_exit_kwargs else self._preview_entry_exit_kwargs(is_doji, hammer_variant)),
            )

    def _indicator_preview_state(
        self,
        is_green: bool,
        wick_side: str,
        is_doji: bool,
        *,
        hammer_variant: str = "CLASSIC",
        preview_trade_side: str = "BUY",
    ) -> dict:
        """Illustrative ST/VWAP lines for Pattern In Context (aligned with indicators/filter.py)."""
        show_st = "supertrend" in self.added_indicator_ids
        show_vwap = "vwap" in self.added_indicator_ids
        direction = (
            logic.TradeDirection.SELL if preview_trade_side == "SELL" else logic.TradeDirection.BUY
        )
        if is_doji:
            st_bullish = is_green
            above_vwap = is_green
        else:
            variant = hammer_variant or ("INVERTED" if wick_side == "UPPER" else "CLASSIC")
            if variant == "CLASSIC" and direction == logic.TradeDirection.BUY:
                st_bullish = True
                above_vwap = True
            elif variant == "INVERTED" and direction == logic.TradeDirection.SELL:
                st_bullish = False
                above_vwap = False
            elif variant == "INVERTED" and direction == logic.TradeDirection.BUY:
                st_bullish = True
                above_vwap = False
            else:
                st_bullish = is_green
                above_vwap = is_green
        return {
            "preview_show_st": show_st,
            "preview_st_bullish": st_bullish,
            "preview_show_vwap": show_vwap,
            "preview_price_above_vwap": above_vwap,
        }

    def _add_indicator(self, ind_id: str) -> bool:
        if not ind_id or ind_id not in INDICATOR_REGISTRY:
            return False
        if ind_id in self.added_indicator_ids:
            return False
        self.added_indicator_ids.add(ind_id)
        group = self.indicator_group_boxes.get(ind_id)
        if group is not None:
            group.setVisible(True)
        self._refresh_indicator_combine_visibility()
        self._redraw_candle_preview()
        return True

    def _remove_indicator(self, ind_id: str):
        if ind_id not in self.added_indicator_ids:
            return
        self.added_indicator_ids.discard(ind_id)
        group = self.indicator_group_boxes.get(ind_id)
        if group is not None:
            group.setVisible(False)
        self._refresh_indicator_combine_visibility()
        self._redraw_candle_preview()

    def _refresh_indicator_combine_visibility(self):
        if self.indicator_combine_row is not None:
            self.indicator_combine_row.setVisible(len(self.added_indicator_ids) >= 2)

    def _on_preview_add_indicator(self):
        ind_id = self.preview_indicator_combo.currentData()
        if not self._add_indicator(ind_id):
            if ind_id:
                QMessageBox.information(
                    self, "Indicator",
                    f"{INDICATOR_REGISTRY[ind_id]['label']} is already added — edit settings on the Indicators tab.",
                )
            return
        if hasattr(self, "parameter_tabs"):
            for i in range(self.parameter_tabs.count()):
                if self.parameter_tabs.tabText(i) == "Indicators":
                    self.parameter_tabs.setCurrentIndex(i)
                    break

    def _on_add_indicator_clicked(self):
        ind_id = self.indicator_pick_combo.currentData()
        if not self._add_indicator(ind_id) and ind_id:
            QMessageBox.information(
                self, "Indicator",
                f"{INDICATOR_REGISTRY[ind_id]['label']} is already in the list.",
            )

    def _add_indicator_field_row(
        self, grid: QGridLayout, row: int, name: str, label_text: str, ftype: int, choices,
        live_preview: bool,
    ) -> int:
        default_val = default_value_for_field(name, [logic.StrategyConfig()])
        if isinstance(default_val, Enum):
            default_val = default_val.value
        help_text = FIELD_HELP.get(name, label_text)
        label = QLabel(label_text)
        label.setWordWrap(True)
        label.setToolTip(help_text)
        grid.addWidget(label, row, 0)
        self.field_labels[name] = label

        if ftype == FIELD_TYPE_TEXT:
            widget = QLineEdit(str(default_val))
            widget.setToolTip(help_text)
            if live_preview:
                widget.textChanged.connect(self._redraw_candle_preview)
            grid.addWidget(widget, row, 1)
        elif ftype == FIELD_TYPE_CHECK:
            widget = QCheckBox()
            widget.setChecked(bool(default_val))
            widget.setToolTip(help_text)
            if live_preview:
                widget.stateChanged.connect(self._redraw_candle_preview)
            grid.addWidget(widget, row, 1)
        elif ftype == FIELD_TYPE_DROPDOWN:
            widget = QComboBox()
            widget.addItems(choices)
            widget.setCurrentText(str(default_val))
            widget.setToolTip(help_text)
            grid.addWidget(widget, row, 1)
        else:
            return row

        self.field_widgets[name] = widget
        return row + 1

    def _indicator_filter_logic_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), INDICATOR_FILTER_LOGIC_FILE)

    def _show_indicator_filter_logic_help(self):
        lines = [
            "Trade-filter rules are not edited in the dashboard — they live in Python so you can change them anytime.",
            "",
            f"File: {self._indicator_filter_logic_path()}",
            "",
            "Combine mode (2+ indicators):",
            INDICATOR_COMBINE_HELP.replace("\n", "\n"),
            "",
        ]
        for ind_id, meta in INDICATOR_REGISTRY.items():
            lines.append(f"—— {meta['label']} ——")
            for rule in meta.get("filter_rules", []):
                lines.append(f"  • {rule}")
            fn = meta.get("filter_fn")
            if fn:
                lines.append(f"  Function: {fn}()")
            lines.append("")
        lines.append("New indicators: register in indicators/registry.py, compute in a new module, add filter fn in filter.py.")
        QMessageBox.information(self, "Indicator filter logic", "\n".join(lines))

    def _open_indicator_filter_logic_file(self):
        path = self._indicator_filter_logic_path()
        if not os.path.isfile(path):
            QMessageBox.warning(self, "File not found", path)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _make_indicator_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(12, 12, 12, 12)

        hint = QLabel(
            "Add indicators from the dropdown (required — adding alone enables them), tune parameters, "
            "and keep “Apply trade filter (backtest + live)” checked to block trades that fail "
            "SuperTrend/VWAP on the same timeframe as live trading. Stop and Start live after changes."
        )
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        logic_row = QHBoxLayout()
        rules_btn = QPushButton("View filter rules…")
        rules_btn.setObjectName("secondaryButton")
        rules_btn.setToolTip("Shows the exact BUY/SELL rules used in backtests.")
        rules_btn.clicked.connect(self._show_indicator_filter_logic_help)
        open_code_btn = QPushButton("Open filter logic file")
        open_code_btn.setObjectName("secondaryButton")
        open_code_btn.setToolTip(
            f"Opens {INDICATOR_FILTER_LOGIC_FILE} in your editor — edit _supertrend_passes / _vwap_passes to change behavior."
        )
        open_code_btn.clicked.connect(self._open_indicator_filter_logic_file)
        logic_row.addWidget(rules_btn)
        logic_row.addWidget(open_code_btn)
        logic_row.addStretch()
        outer.addLayout(logic_row)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Add indicator:"))
        self.indicator_pick_combo = QComboBox()
        self.indicator_pick_combo.setMinimumWidth(180)
        self.indicator_pick_combo.addItem("— choose —", "")
        for ind_id, meta in sorted(INDICATOR_REGISTRY.items(), key=lambda kv: kv[1]["label"]):
            self.indicator_pick_combo.addItem(meta["label"], ind_id)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("secondaryButton")
        add_btn.clicked.connect(self._on_add_indicator_clicked)
        add_row.addWidget(self.indicator_pick_combo, 1)
        add_row.addWidget(add_btn)
        outer.addLayout(add_row)

        for ind_id, meta in INDICATOR_REGISTRY.items():
            group = QGroupBox(meta["label"])
            group.setVisible(False)
            g_layout = QVBoxLayout(group)
            desc = QLabel(meta["description"])
            desc.setObjectName("sectionHint")
            desc.setWordWrap(True)
            g_layout.addWidget(desc)

            rules_lines = meta.get("filter_rules") or []
            if rules_lines:
                rules_box = QLabel(
                    "Backtest filter rules:\n" + "\n".join(f"• {r}" for r in rules_lines)
                )
                rules_box.setObjectName("sectionHint")
                rules_box.setWordWrap(True)
                rules_box.setStyleSheet("color: #3C4043; padding: 6px 0;")
                g_layout.addWidget(rules_box)

            grid = QGridLayout()
            grid.setColumnStretch(1, 1)
            row = 0
            for name, label_text, ftype, choices in INDICATOR_WIDGET_GROUPS.get(ind_id, []):
                row = self._add_indicator_field_row(
                    grid, row, name, label_text, ftype, choices, live_preview=True,
                )
            g_layout.addLayout(grid)

            remove_btn = QPushButton(f"Remove {meta['label']}")
            remove_btn.setObjectName("secondaryButton")
            remove_btn.clicked.connect(lambda _checked=False, i=ind_id: self._remove_indicator(i))
            g_layout.addWidget(remove_btn, alignment=Qt.AlignRight)

            outer.addWidget(group)
            self.indicator_group_boxes[ind_id] = group

        self.indicator_combine_row = QFrame()
        combine_layout = QHBoxLayout(self.indicator_combine_row)
        combine_layout.setContentsMargins(0, 8, 0, 0)
        combine_label = QLabel("Combine filters (2+ indicators):")
        combine_label.setToolTip(INDICATOR_COMBINE_HELP)
        combine_layout.addWidget(combine_label)
        combine_choices = [m.value for m in IndicatorCombineMode]
        combine_default = INDICATOR_UI_DEFAULTS["indicators_combine_mode"]
        combine_combo = QComboBox()
        combine_combo.addItems(combine_choices)
        combine_combo.setCurrentText(str(combine_default))
        combine_combo.setToolTip(INDICATOR_COMBINE_HELP)
        self.field_widgets["indicators_combine_mode"] = combine_combo
        self.field_labels["indicators_combine_mode"] = combine_label
        combine_layout.addWidget(combine_combo, 1)
        self.indicator_combine_row.setVisible(False)
        outer.addWidget(self.indicator_combine_row)

        outer.addStretch()
        scroll.setWidget(inner)
        return scroll

    @staticmethod
    def _live_pair_row(grid: QGridLayout, row: int, left_label: str, left_w: QWidget,
                       right_label: str, right_w: QWidget):
        la = QLabel(left_label)
        la.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        la.setObjectName("sectionHint")
        grid.addWidget(la, row, 0)
        grid.addWidget(left_w, row, 1)
        rb = QLabel(right_label)
        rb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rb.setObjectName("sectionHint")
        grid.addWidget(rb, row, 2)
        grid.addWidget(right_w, row, 3)

    def _build_live_panel(self) -> QWidget:
        """Single Live Trading dock — structured layout that scrolls inside the panel."""
        self._init_live_trading_widgets()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        wrap = QWidget()
        root = QVBoxLayout(wrap)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        hero = QFrame()
        hero.setObjectName("liveHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(12, 10, 12, 10)
        hero_layout.setSpacing(6)
        title = QLabel("Live trading")
        title.setObjectName("liveHeroTitle")
        hero_layout.addWidget(title)
        steps = QHBoxLayout()
        steps.setSpacing(8)
        for text in ("1 · Strategy & risk", "2 · Connect MT5", "3 · Dry run, then go live"):
            chip = QLabel(text)
            chip.setObjectName("liveStepChip")
            steps.addWidget(chip)
        steps.addStretch()
        hero_layout.addLayout(steps)
        sub = QLabel(
            "Use menu <b>Notifications</b> for Telegram alerts. "
            "Use <b>Settings…</b> for MT5, orders, and risk limits."
        )
        sub.setTextFormat(Qt.RichText)
        sub.setWordWrap(True)
        sub.setObjectName("sectionHint")
        hero_layout.addWidget(sub)
        root.addWidget(hero)

        control = QFrame()
        control.setObjectName("liveControlBar")
        ctrl_layout = QHBoxLayout(control)
        ctrl_layout.setContentsMargins(10, 8, 10, 8)
        ctrl_layout.addWidget(self.live_connect_btn)
        ctrl_layout.addWidget(self.live_disconnect_btn)
        self.live_settings_btn = QPushButton("Settings…")
        self.live_settings_btn.setObjectName("secondaryButton")
        self.live_settings_btn.setToolTip("MT5, execution, market/limit orders, safety limits, advanced options")
        self.live_settings_btn.clicked.connect(self._open_live_settings)
        ctrl_layout.addWidget(self.live_settings_btn)
        ctrl_layout.addStretch()
        self.live_dry_run_cb = QCheckBox("Dry run (no orders)")
        self.live_dry_run_cb.setChecked(True)
        self.live_dry_run_cb.setToolTip(
            "When checked, signals are logged only — MT5 orders are not sent. "
            "Uncheck only when you intend to place orders."
        )
        self.live_dry_run_cb.toggled.connect(self._on_live_dry_run_checkbox)
        ctrl_layout.addWidget(self.live_dry_run_cb)
        ctrl_layout.addWidget(self.live_start_btn)
        ctrl_layout.addWidget(self.live_stop_btn)
        root.addWidget(control)

        root.addWidget(self.live_status_label)

        self.live_config_summary = QLabel("Open Settings to configure MT5, orders, and risk limits.")
        self.live_config_summary.setWordWrap(True)
        self.live_config_summary.setObjectName("liveSummaryCard")
        self.live_config_summary.setTextFormat(Qt.RichText)
        root.addWidget(self.live_config_summary)

        strat_box = QGroupBox("Strategy")
        strat_layout = QVBoxLayout(strat_box)
        pat_row = QHBoxLayout()
        pat_row.addWidget(QLabel("Pattern"))
        pat_row.addWidget(self.live_pattern_combo, 1)
        refresh_strat_btn = QPushButton("Refresh")
        refresh_strat_btn.setObjectName("secondaryButton")
        refresh_strat_btn.setFixedWidth(88)
        refresh_strat_btn.clicked.connect(self._refresh_live_strategy_summary)
        pat_row.addWidget(refresh_strat_btn)
        apply_live_btn = QPushButton("Apply to live")
        apply_live_btn.setObjectName("secondaryButton")
        apply_live_btn.setFixedWidth(100)
        apply_live_btn.setToolTip(
            "Push current Parameters (direction, shape, indicators, RR/SL) to a running live loop "
            "without Stop/Start. Use after you change the Direction tab or load a preset."
        )
        apply_live_btn.clicked.connect(self._apply_parameters_to_live)
        pat_row.addWidget(apply_live_btn)
        verify_btn = QPushButton("Verify rules")
        verify_btn.setObjectName("secondaryButton")
        verify_btn.setFixedWidth(96)
        verify_btn.setToolTip("Self-test: green bar → Green direction, red bar → Red direction.")
        verify_btn.clicked.connect(self._verify_hammer_direction_rules)
        pat_row.addWidget(verify_btn)
        strat_layout.addLayout(pat_row)
        strat_layout.addWidget(self.live_strategy_summary)
        root.addWidget(strat_box)

        log_box = QGroupBox("Activity log")
        log_layout = QVBoxLayout(log_box)
        self.live_log_view.setMinimumHeight(120)
        self.live_log_view.setMaximumHeight(200)
        log_layout.addWidget(self.live_log_view)
        root.addWidget(log_box)

        scroll.setWidget(wrap)
        return scroll

    def _init_live_trading_widgets(self):
        """Create live widgets once (single parent tree in _build_live_panel)."""
        if getattr(self, "_live_widgets_initialized", False):
            return
        self._live_widgets_initialized = True

        self.live_pattern_combo = QComboBox()
        self.live_pattern_combo.addItems(list(PATTERN_REGISTRY.keys()))
        self.live_pattern_combo.setToolTip(
            "Mirrors Parameters → Pattern. Live reads all shape, entry/exit, RR/SL, and "
            "indicator fields from the Parameters panel when you click Start live."
        )
        self.live_pattern_combo.currentTextChanged.connect(self._on_live_pattern_combo_changed)

        self.live_strategy_summary = QLabel("Loading strategy summary…")
        self.live_strategy_summary.setWordWrap(True)
        self.live_strategy_summary.setObjectName("liveSummaryCard")
        self.live_strategy_summary.setTextFormat(Qt.RichText)

        self.live_mt5_path = QLineEdit()
        self.live_mt5_path.setPlaceholderText("Optional if MT5 is already running")
        self.live_login = QLineEdit()
        self.live_password = QLineEdit()
        self.live_password.setEchoMode(QLineEdit.Password)
        self.live_server = QLineEdit()

        self.live_symbol = QLineEdit("XAUUSD")
        self.live_timeframe = QComboBox()
        self.live_timeframe.addItems(TIMEFRAME_LABELS)
        self.live_volume = QLineEdit("0.01")
        self.live_magic = QLineEdit("88001001")
        self.live_max_positions = QLineEdit("1")
        self.live_poll_sec = QLineEdit("2")

        self.live_demo_only = QCheckBox("Optional: restrict to demo accounts only")
        self.live_demo_only.setChecked(False)
        self.live_max_daily_trades = QLineEdit("10")
        self.live_max_daily_loss = QLineEdit("100")
        self.live_min_minutes_between = QLineEdit("5")
        self.live_max_spread = QLineEdit("50")
        self.live_max_lot_cap = QLineEdit("0.10")
        self.live_max_lot_cap.setToolTip("Hard ceiling on volume sent to MT5.")

        self.live_history_bars = QLineEdit("400")
        self.live_history_bars.setToolTip("Bars fetched from MT5 for signal calculation.")

        self.live_order_mode = QComboBox()
        for mode_id, label in LIVE_ORDER_MODES:
            self.live_order_mode.addItem(label, mode_id)
        self.live_limit_offset = QLineEdit("0")
        self.live_max_entry_deviation = QLineEdit("200")
        self.live_max_entry_deviation.setToolTip(
            "If strategy entry (candle open) is farther than this many points from bid/ask, "
            "live sends a market order at current price with SL/TP re-anchored (recommended for XAU)."
        )
        self.live_limit_offset_from_market = QCheckBox("Limit offset from current bid/ask (not candle entry)")
        self.live_limit_offset_from_market.setChecked(True)
        self.live_limit_offset_from_market.setToolTip(
            "When order type is Limit — entry ± offset, base the limit on live tick price instead of backtest entry."
        )
        self.live_deviation = QLineEdit("20")
        self.live_order_comment = QLineEdit("HammerDashboard")

        self.live_fallback_market = QCheckBox("If limit order fails, try market once")
        self.live_fallback_market.setToolTip(
            "When order type is limit and MT5 rejects the pending order, send one market order instead."
        )
        self.live_warn_tf_mismatch = QCheckBox("Warn if live timeframe is not enabled in backtest")
        self.live_warn_tf_mismatch.setChecked(True)
        self.live_warn_tf_mismatch.setToolTip(
            "On Start live, remind you if the live chart TF is unchecked in Parameters → Timeframes."
        )

        self.live_telegram_enabled = QCheckBox("Send Telegram alerts for all order events")
        self.live_telegram_enabled.setToolTip(
            "Send Telegram messages for real orders, dry run, failed orders, and safety blocks."
        )
        self.live_telegram_token = QLineEdit()
        self.live_telegram_token.setEchoMode(QLineEdit.Password)
        self.live_telegram_token.setPlaceholderText("From @BotFather")
        self.live_telegram_chat_id = QLineEdit()
        self.live_telegram_chat_id.setPlaceholderText("Your numeric chat id")
        self.live_telegram_chat_id.setToolTip(
            "Message @userinfobot on Telegram to get your numeric chat id."
        )
        self.live_telegram_status = QLabel("Off")
        self.live_telegram_status.setObjectName("liveNotifyStatus")
        self.live_telegram_enabled.toggled.connect(
            self._refresh_live_telegram_status, Qt.ConnectionType.UniqueConnection
        )
        # Apply on Save in Notifications dialog — not on every keystroke (avoids UI stalls).

        self.live_settings_strategy_info = QLabel("Strategy summary will appear when you open Settings.")
        self.live_settings_strategy_info.setWordWrap(True)
        self.live_settings_strategy_info.setTextFormat(Qt.RichText)

        self.live_mt5_path.setToolTip("Full path to terminal64.exe — leave empty if MT5 is already running.")
        self.live_login.setToolTip("MT5 account number (optional if terminal is already logged in).")
        self.live_magic.setToolTip("Unique ID on orders — use one magic per strategy/bot.")
        self.live_poll_sec.setToolTip("How often to poll MT5 for new bars (seconds). 1–5 is typical.")
        self.live_max_spread.setToolTip(
            "Block orders when spread exceeds this (in points). Raise for XAUUSD if safety blocks too often."
        )
        self.live_min_minutes_between.setToolTip("Minimum minutes between filled orders. Use 0 while testing.")
        self.live_demo_only.setToolTip("When checked, Start live refuses a real/live MT5 account.")

        host = getattr(self, "_live_settings_host", None)
        if host is not None:
            for w in self._live_settings_field_widgets():
                w.setParent(host)
        notify_host = getattr(self, "_live_notifications_host", None)
        if notify_host is not None:
            for w in self._live_notifications_field_widgets():
                w.setParent(notify_host)

        self.live_connect_btn = QPushButton("Connect MT5")
        self.live_connect_btn.setObjectName("secondaryButton")
        self.live_connect_btn.clicked.connect(self._on_live_connect)
        self.live_disconnect_btn = QPushButton("Disconnect")
        self.live_disconnect_btn.setObjectName("secondaryButton")
        self.live_disconnect_btn.clicked.connect(self._on_live_disconnect)
        self.live_start_btn = QPushButton("Start live")
        self.live_start_btn.setObjectName("liveStartButton")
        self.live_start_btn.clicked.connect(self._on_live_start)
        self.live_stop_btn = QPushButton("Stop live")
        self.live_stop_btn.setObjectName("secondaryButton")
        self.live_stop_btn.setEnabled(False)
        self.live_stop_btn.clicked.connect(self._stop_live_trading)

        self.live_status_label = QLabel("MT5: not connected")
        self.live_status_label.setObjectName("liveStatusBadge")

        self.live_log_view = QPlainTextEdit()
        self.live_log_view.setObjectName("liveLogConsole")
        self.live_log_view.setReadOnly(True)
        self.live_log_view.setMinimumHeight(160)
        self.live_log_view.setPlaceholderText(
            "Connection messages and live loop output… "
            f"(also saved under {LIVE_JOURNAL_DIR}/)"
        )

        self.live_symbol.textChanged.connect(lambda: self._refresh_live_strategy_summary())
        self.live_timeframe.currentTextChanged.connect(lambda _t: self._refresh_live_strategy_summary())
        QTimer.singleShot(0, self._sync_live_pattern_from_dashboard)
        QTimer.singleShot(0, self._refresh_live_strategy_summary)
        QTimer.singleShot(0, self._refresh_live_config_summary)

    def _verify_hammer_direction_rules(self):
        if self.pattern_combo.currentText() == "Doji":
            QMessageBox.information(
                self,
                "Verify rules",
                "Switch Pattern to Hammer to run green/red direction self-test, "
                "or backtest Doji with its Direction mode.",
            )
            return
        try:
            cfg = self._build_hammer_strategy_config()
        except Exception as e:
            QMessageBox.critical(self, "Verify rules", f"Could not read parameters:\n{e}")
            return
        lines = logic.run_hammer_direction_self_test(cfg)
        body = "\n".join(lines)
        failed = any("FAIL" in ln for ln in lines)
        if failed:
            QMessageBox.warning(self, "Direction self-test — fix parameters", body)
        else:
            QMessageBox.information(self, "Direction self-test — OK", body)

    def _apply_parameters_to_live(self):
        if self._live_engine is None or self._live_thread is None or not self._live_thread.isRunning():
            QMessageBox.information(
                self,
                "Apply to live",
                "Start live first, then change Parameters and click Apply to live "
                "(or Stop live → fix settings → Start live).",
            )
            return
        try:
            pattern, pattern_type, strategy_config, indicator_stack = (
                self._collect_live_strategy_from_parameters()
            )
        except Exception as e:
            QMessageBox.critical(self, "Apply to live", str(e))
            return
        tf = self._collect_time_filter_kwargs()
        self._live_engine.update_runtime_strategy(
            strategy_config, indicator_stack, pattern_type, pattern,
            sessions_enabled=tf["sessions_enabled"],
            session_clock=tf["session_clock"],
            broker_utc_offset_hours=tf["broker_utc_offset_hours"],
            ist_time_filter_enabled=tf["ist_time_filter_enabled"],
            ist_time_start=tf["ist_time_start"],
            ist_time_end=tf["ist_time_end"],
            telegram_enabled=self.live_telegram_enabled.isChecked(),
            telegram_bot_token=self.live_telegram_token.text().strip(),
            telegram_chat_id=self.live_telegram_chat_id.text().strip(),
        )
        self._refresh_live_strategy_summary()
        self._live_log("Dashboard parameters pushed to live loop.")

    def _open_live_settings(self):
        if self._live_thread is not None and self._live_thread.isRunning():
            QMessageBox.information(
                self,
                "Live running",
                "Stop live before changing settings, then Start again.",
            )
            return
        host = getattr(self, "_live_settings_host", None)
        if host is not None:
            for w in self._live_settings_field_widgets():
                if _qt_widget_valid(w) and w.parent() is not host:
                    w.setParent(host)
        dlg = LiveSettingsDialog(self)
        dlg.exec()

    def _open_live_notifications(self):
        host = getattr(self, "_live_notifications_host", None)
        if host is not None:
            for w in self._live_notifications_field_widgets():
                if _qt_widget_valid(w) and w.parent() is not host:
                    w.setParent(host)
        dlg = LiveNotificationsDialog(self)
        dlg.exec()

    def _telegram_field_values(self) -> tuple[bool, str, str]:
        if not self._telegram_widgets_valid():
            return False, "", ""
        return (
            self.live_telegram_enabled.isChecked(),
            self.live_telegram_token.text().strip(),
            self.live_telegram_chat_id.text().strip(),
        )

    def _telegram_widgets_valid(self) -> bool:
        for w in self._live_notifications_field_widgets():
            if not _qt_widget_valid(w):
                return False
        return True

    def _save_telegram_settings(self) -> None:
        if not self._telegram_widgets_valid():
            return
        enabled, token, chat_id = self._telegram_field_values()
        s = self._settings
        s.setValue("live/telegram_enabled", enabled)
        s.setValue("live/telegram_token", token)
        s.setValue("live/telegram_chat_id", chat_id)

    def _apply_live_telegram_settings(self) -> None:
        if not self._telegram_widgets_valid():
            return
        enabled, token, chat_id = self._telegram_field_values()
        if self._live_engine is not None and self._live_worker_running():
            self._live_engine.update_telegram_settings(
                enabled=enabled,
                bot_token=token,
                chat_id=chat_id,
            )

    def _live_notifications_field_widgets(self):
        widgets = [
            self.live_telegram_enabled,
            self.live_telegram_token,
            self.live_telegram_chat_id,
            self.live_telegram_status,
        ]
        return widgets

    def _stash_live_notifications_widgets(self):
        host = getattr(self, "_live_notifications_host", None)
        if host is None:
            return
        for w in self._live_notifications_field_widgets():
            w.setParent(host)

    def _live_settings_field_widgets(self):
        widgets = [
            self.live_mt5_path, self.live_login, self.live_password, self.live_server,
            self.live_symbol, self.live_timeframe, self.live_volume, self.live_magic,
            self.live_max_positions, self.live_poll_sec, self.live_history_bars,
            self.live_order_mode, self.live_limit_offset, self.live_deviation,
            self.live_max_entry_deviation, self.live_limit_offset_from_market,
            self.live_order_comment, self.live_demo_only,
            self.live_max_daily_trades, self.live_max_daily_loss,
            self.live_min_minutes_between, self.live_max_spread, self.live_max_lot_cap,
            self.live_fallback_market, self.live_warn_tf_mismatch,
            self.live_settings_strategy_info,
        ]
        if hasattr(self, "live_adv_thread_pool"):
            widgets.append(self.live_adv_thread_pool)
        if hasattr(self, "live_adv_ray"):
            widgets.append(self.live_adv_ray)
        return widgets

    def _stash_live_settings_widgets(self):
        host = getattr(self, "_live_settings_host", None)
        if host is None:
            return
        for w in self._live_settings_field_widgets():
            w.setParent(host)

    def _open_live_journal_folder(self):
        path = live_journal_dir(DEFAULT_OUTPUT_DIR)
        self._open_folder(path)
        self._live_log(f"Opened live journal folder: {path}")

    def _focus_backtest_params_dock(self):
        if hasattr(self, "params_dock"):
            self.params_dock.show()
            self.params_dock.raise_()

    def _sync_live_execution_from_backtest(self):
        sym_w = self.field_widgets.get("symbol")
        if isinstance(sym_w, QLineEdit) and sym_w.text().strip():
            self.live_symbol.setText(sym_w.text().strip())
        live_tf = None
        for tf_label in TIMEFRAME_LABELS:
            folder = TIMEFRAME_TO_FOLDER[tf_label]
            cb = self.timeframe_enabled_widgets.get(folder)
            if cb is not None and cb.isChecked():
                live_tf = tf_label
                break
        if live_tf is None:
            for tf_label in TIMEFRAME_LABELS:
                folder = TIMEFRAME_TO_FOLDER[tf_label]
                cb = self.timeframe_enabled_widgets.get(folder)
                if cb is not None:
                    live_tf = tf_label
                    break
        if live_tf and self.live_timeframe.findText(live_tf) >= 0:
            self.live_timeframe.setCurrentText(live_tf)
        self._refresh_live_strategy_summary()
        self._refresh_live_config_summary()
        self._refresh_live_settings_strategy_tab()
        self._live_log(
            f"Synced live execution from backtest: {self.live_symbol.text().strip()} @ "
            f"{self.live_timeframe.currentText()}"
        )

    def _apply_live_demo_testing_limits(self):
        self.live_max_spread.setText("150")
        self.live_min_minutes_between.setText("0")
        self._live_log("Applied relaxed demo testing limits (spread 150, cooldown 0).")

    def _refresh_live_settings_strategy_tab(self):
        if not hasattr(self, "live_settings_strategy_info"):
            return
        self._refresh_live_strategy_summary()
        preset_file = ""
        if hasattr(self, "preset_combo"):
            preset_file = self.preset_combo.currentData() or ""
        preset_line = preset_file if preset_file else "<i>none loaded</i>"
        bt_sym = ""
        sym_w = self.field_widgets.get("symbol")
        if isinstance(sym_w, QLineEdit):
            bt_sym = sym_w.text().strip()
        enabled = []
        for tf_label in TIMEFRAME_LABELS:
            folder = TIMEFRAME_TO_FOLDER[tf_label]
            cb = self.timeframe_enabled_widgets.get(folder)
            if cb is not None and cb.isChecked():
                enabled.append(tf_label)
        enabled_txt = ", ".join(enabled) if enabled else "<i>none checked</i>"
        live_sym = self.live_symbol.text().strip() if hasattr(self, "live_symbol") else "?"
        live_tf = self.live_timeframe.currentText() if hasattr(self, "live_timeframe") else "?"
        mismatch = ""
        if live_tf and enabled and live_tf not in enabled:
            mismatch = (
                f"<br><span style='color:#c5221f;'><b>Note:</b> Live TF {live_tf} is not in backtest "
                f"included timeframes ({enabled_txt}).</span>"
            )
        if bt_sym and live_sym and bt_sym.upper() != live_sym.upper():
            mismatch += (
                f"<br><span style='color:#c5221f;'><b>Note:</b> Live symbol {live_sym} ≠ "
                f"backtest symbol {bt_sym}.</span>"
            )
        html = (
            f"{self.live_strategy_summary.text()}<br><br>"
            f"<b>Backtest symbol</b> {bt_sym or '—'} · <b>Included TFs</b> {enabled_txt}<br>"
            f"<b>Live execution</b> {live_sym} @ {live_tf} · <b>Preset</b> {preset_line}"
            f"{mismatch}"
        )
        self.live_settings_strategy_info.setText(html)

    def _live_preflight_notes(self, live_cfg: live_trading.LiveRunConfig) -> List[str]:
        notes: List[str] = []
        if self._live_dry_run_checked():
            notes.append("Dry run is ON — orders will not be sent (signals still logged).")
        else:
            notes.append("Dry run is OFF — orders will be sent when signals fire.")
        notes.append(f"Order type: {live_cfg.order_mode}")
        if live_cfg.order_mode != "market":
            notes.append("Limit orders: ensure prices are valid vs bid/ask or enable market fallback in Settings.")
        if hasattr(self, "live_warn_tf_mismatch") and self.live_warn_tf_mismatch.isChecked():
            live_tf = live_cfg.timeframe_label
            folder = TIMEFRAME_TO_FOLDER.get(live_tf, "")
            cb = self.timeframe_enabled_widgets.get(folder)
            if cb is not None and not cb.isChecked():
                notes.append(
                    f"Live timeframe {live_tf} is NOT checked under Backtest → Timeframes — signals may differ."
                )
        sym_w = self.field_widgets.get("symbol")
        if isinstance(sym_w, QLineEdit):
            bt_sym = sym_w.text().strip()
            if bt_sym and bt_sym.upper() != live_cfg.symbol.upper():
                notes.append(f"Live symbol {live_cfg.symbol} differs from backtest symbol {bt_sym}.")
        notes.append(f"Journal: {session_log_path(live_cfg.journal_dir)}")
        return notes

    def _live_indicator_preflight_lines(self, stack: IndicatorStackConfig) -> List[str]:
        lines: List[str] = []
        enabled = stack.enabled_indicator_ids()
        if not enabled:
            lines.append(
                "⚠ No indicators added (Parameters → Indicators → Add). "
                "SuperTrend/VWAP will NOT block trades until you add them and restart live."
            )
            return lines
        if stack.supertrend.enabled:
            filt = "ON" if stack.supertrend.apply_trade_filter else "OFF — will NOT block trades"
            lines.append(
                f"SuperTrend filter {filt} (ATR={stack.supertrend.atr_period}, "
                f"mult={stack.supertrend.multiplier}) on live timeframe only."
            )
        if stack.vwap.enabled:
            filt = "ON" if stack.vwap.apply_trade_filter else "OFF — will NOT block trades"
            lines.append(f"VWAP session filter {filt} on live timeframe only.")
        if len(enabled) >= 2:
            lines.append(f"Combine mode: {stack.combine_mode.value}")
        lines.append(
            "Indicators use MT5 broker bars for the selected live timeframe "
            "(may differ slightly from TradingView feed)."
        )
        return lines

    def _live_order_mode_value(self) -> str:
        if not hasattr(self, "live_order_mode"):
            return "market"
        data = self.live_order_mode.currentData()
        if data:
            return str(data)
        return "market"

    def _refresh_live_config_summary(self):
        if not hasattr(self, "live_config_summary"):
            return
        mode_label = self.live_order_mode.currentText() if hasattr(self, "live_order_mode") else "Market"
        dry = "ON" if self._live_dry_run_checked() else "OFF"
        sym = self.live_symbol.text().strip() if hasattr(self, "live_symbol") else "XAUUSD"
        tf = self.live_timeframe.currentText() if hasattr(self, "live_timeframe") else "1h"
        lots = self.live_volume.text().strip() if hasattr(self, "live_volume") else "0.01"
        login = self.live_login.text().strip() if hasattr(self, "live_login") else ""
        html = (
            f"<b>Execution</b> {sym} @ {tf} · lots {lots} · dry run <b>{dry}</b><br>"
            f"<b>Order</b> {mode_label} · deviation {self.live_deviation.text().strip() if hasattr(self, 'live_deviation') else '20'} pts<br>"
            f"<b>Safety</b> max {self.live_max_daily_trades.text().strip() if hasattr(self, 'live_max_daily_trades') else '?'} trades/day · "
            f"max loss ${self.live_max_daily_loss.text().strip() if hasattr(self, 'live_max_daily_loss') else '?'} · "
            f"spread ≤ {self.live_max_spread.text().strip() if hasattr(self, 'live_max_spread') else '?'} pts<br>"
        )
        if login:
            html += f"<b>MT5 login</b> {login} (use Settings for password/server)<br>"
        else:
            html += "<b>MT5</b> not configured — open Settings<br>"
        html += "<span style='color:#5f6368;'>Click Settings… for connection, limit orders, and all limits.</span>"
        self.live_config_summary.setText(html)

    def _refresh_live_telegram_status(self) -> None:
        if not hasattr(self, "live_telegram_status") or not _qt_widget_valid(self.live_telegram_status):
            return
        if not self._telegram_widgets_valid():
            return
        enabled = self.live_telegram_enabled.isChecked()
        token = self.live_telegram_token.text().strip()
        chat_id = self.live_telegram_chat_id.text().strip()
        badge = self.live_telegram_status
        if not enabled:
            badge.setText("Off")
            badge.setProperty("ready", False)
        elif token and chat_id:
            badge.setText("Ready")
            badge.setProperty("ready", True)
        else:
            badge.setText("Needs token + chat id")
            badge.setProperty("ready", False)
        badge.style().unpolish(badge)
        badge.style().polish(badge)

    def _on_live_telegram_changed(self) -> None:
        """Legacy hook — prefer _save_telegram_settings + _apply_live_telegram_settings."""
        self._save_telegram_settings()
        self._refresh_live_telegram_status()
        self._apply_live_telegram_settings()

    def _on_live_pattern_combo_changed(self, pattern_name: str):
        if not pattern_name or not hasattr(self, "pattern_combo"):
            return
        if self.pattern_combo.currentText() != pattern_name:
            self.pattern_combo.setCurrentText(pattern_name)
        self._refresh_live_strategy_summary()

    def _sync_live_pattern_from_dashboard(self):
        if not hasattr(self, "live_pattern_combo") or not hasattr(self, "pattern_combo"):
            return
        self.live_pattern_combo.blockSignals(True)
        text = self.pattern_combo.currentText()
        if self.live_pattern_combo.findText(text) >= 0:
            self.live_pattern_combo.setCurrentText(text)
        self.live_pattern_combo.blockSignals(False)

    def _refresh_live_strategy_summary(self):
        if not hasattr(self, "live_strategy_summary"):
            return
        self._sync_live_pattern_from_dashboard()
        pattern = self.pattern_combo.currentText() if hasattr(self, "pattern_combo") else "Hammer"
        preset_file = ""
        if hasattr(self, "preset_combo"):
            preset_file = self.preset_combo.currentData() or ""
        inds = ", ".join(sorted(self.added_indicator_ids)) or "none"
        combine = "—"
        w = self.field_widgets.get("indicators_combine_mode")
        if isinstance(w, QComboBox):
            combine = w.currentText()
        ind_line = f"Indicators: {inds}"
        if inds != "none":
            ind_line += f" · combine <b>{combine}</b>"
        detail = ""
        try:
            if pattern == "Doji":
                mode = self._get_field_str("doji_direction_mode") or "WICK_BIAS"
                style = self._get_field_str("doji_style") or "ANY"
                try:
                    det = doji_logic.describe_doji_detection(self._build_doji_strategy_config())
                except Exception:
                    det = f"Doji {style} · {mode}"
                detail = det
            elif pattern == "Hammer with candles":
                try:
                    cfg = self._build_hammer_context_strategy_config()
                    detail = hammer_context_logic.describe_hammer_context_rules(cfg)
                    ee = hammer_context_logic.describe_hammer_context_entry_exit(cfg)
                    detail += f"<br><span style='color:#5f6368;'>{ee}</span>"
                except Exception:
                    detail = "Hammer with candles — classic green BUY / inverted red SELL + prior N bars"
            elif pattern == "Hammer":
                cfg = self._build_hammer_strategy_config()
                detail = logic.describe_hammer_direction_matrix(cfg)
                try:
                    det = logic.describe_hammer_detection(cfg)
                except Exception:
                    det = ""
                if det:
                    detail += f"<br><span style='color:#5f6368;'>{det}</span>"
                try:
                    ee = logic.describe_hammer_entry_exit(cfg)
                except Exception:
                    ee = ""
                if ee:
                    detail += f"<br><span style='color:#5f6368;'>{ee}</span>"
            else:
                detail = ""
        except Exception:
            detail = ""
        tf = self.live_timeframe.currentText() if hasattr(self, "live_timeframe") else "1h"
        sym = self.live_symbol.text() if hasattr(self, "live_symbol") else "XAUUSD"
        preset_html = (
            f"<b>{preset_file}</b>" if preset_file else "none <span style='color:#5F6368'>(parameter tabs)</span>"
        )
        html = (
            f"<b>Pattern</b> {pattern}<br>"
            f"<b>Preset</b> {preset_html}<br>"
            f"{ind_line}<br>"
        )
        if detail:
            html += f"{detail}<br>"
        html += f"<b>Chart</b> {sym} @ {tf}"
        self.live_strategy_summary.setText(html)

    def _live_broker_credentials(self) -> BrokerCredentials:
        login_raw = self.live_login.text().strip()
        login = int(login_raw) if login_raw.isdigit() else 0
        return BrokerCredentials(
            terminal_path=self.live_mt5_path.text().strip(),
            login=login,
            password=self.live_password.text(),
            server=self.live_server.text().strip(),
        )

    def _save_live_settings(self):
        s = self._settings
        if not _qt_widget_valid(getattr(self, "live_mt5_path", None)):
            return
        s.setValue("live/mt5_path", self.live_mt5_path.text())
        s.setValue("live/login", self.live_login.text())
        s.setValue("live/server", self.live_server.text())
        s.setValue("live/symbol", self.live_symbol.text())
        s.setValue("live/timeframe", self.live_timeframe.currentText())
        s.setValue("live/volume", self.live_volume.text())
        s.setValue("live/magic", self.live_magic.text())
        s.setValue("live/max_positions", self.live_max_positions.text())
        s.setValue("live/poll_sec", self.live_poll_sec.text())
        s.setValue("live/dry_run", self._live_dry_run_checked())
        if hasattr(self, "live_demo_only") and _qt_widget_valid(self.live_demo_only):
            s.setValue("live/demo_only", self.live_demo_only.isChecked())
        s.setValue("live/max_daily_trades", self.live_max_daily_trades.text())
        s.setValue("live/max_daily_loss", self.live_max_daily_loss.text())
        s.setValue("live/min_minutes_between", self.live_min_minutes_between.text())
        s.setValue("live/max_spread", self.live_max_spread.text())
        s.setValue("live/max_lot_cap", self.live_max_lot_cap.text())
        s.setValue("live/use_thread_pool", self._live_thread_pool_checked())
        s.setValue("live/use_ray", self._live_ray_checked())
        s.setValue("live/order_mode", self._live_order_mode_value())
        if hasattr(self, "live_limit_offset") and _qt_widget_valid(self.live_limit_offset):
            s.setValue("live/limit_offset", self.live_limit_offset.text())
        s.setValue("live/max_entry_deviation", self.live_max_entry_deviation.text())
        s.setValue("live/limit_offset_from_market", self.live_limit_offset_from_market.isChecked())
        s.setValue("live/deviation", self.live_deviation.text())
        s.setValue("live/order_comment", self.live_order_comment.text())
        s.setValue("live/history_bars", self.live_history_bars.text())
        s.setValue("live/fallback_market", self.live_fallback_market.isChecked())
        s.setValue("live/warn_tf_mismatch", self.live_warn_tf_mismatch.isChecked())
        if self._telegram_widgets_valid():
            enabled, token, chat_id = self._telegram_field_values()
            s.setValue("live/telegram_enabled", enabled)
            s.setValue("live/telegram_token", token)
            s.setValue("live/telegram_chat_id", chat_id)

    def _restore_live_settings(self):
        if not hasattr(self, "live_mt5_path"):
            return
        s = self._settings
        self.live_mt5_path.setText(s.value("live/mt5_path", "", type=str))
        self.live_login.setText(s.value("live/login", "", type=str))
        self.live_server.setText(s.value("live/server", "", type=str))
        self.live_symbol.setText(s.value("live/symbol", "XAUUSD", type=str))
        tf = s.value("live/timeframe", "1h", type=str)
        if self.live_timeframe.findText(tf) >= 0:
            self.live_timeframe.setCurrentText(tf)
        self.live_volume.setText(s.value("live/volume", "0.01", type=str))
        self.live_magic.setText(s.value("live/magic", "88001001", type=str))
        self.live_max_positions.setText(s.value("live/max_positions", "1", type=str))
        self.live_poll_sec.setText(s.value("live/poll_sec", "2", type=str))
        if hasattr(self, "_live_action_dry_run"):
            self._live_action_dry_run.setChecked(s.value("live/dry_run", True, type=bool))
            self._live_action_thread_pool.setChecked(s.value("live/use_thread_pool", True, type=bool))
            self._live_action_ray.setChecked(s.value("live/use_ray", False, type=bool))
        if hasattr(self, "live_demo_only"):
            self.live_demo_only.setChecked(s.value("live/demo_only", False, type=bool))
            self.live_max_daily_trades.setText(s.value("live/max_daily_trades", "10", type=str))
            self.live_max_daily_loss.setText(s.value("live/max_daily_loss", "100", type=str))
            self.live_min_minutes_between.setText(s.value("live/min_minutes_between", "5", type=str))
            self.live_max_spread.setText(s.value("live/max_spread", "50", type=str))
            self.live_max_lot_cap.setText(s.value("live/max_lot_cap", "0.10", type=str))
        if hasattr(self, "live_history_bars"):
            self.live_history_bars.setText(s.value("live/history_bars", "400", type=str))
            self.live_limit_offset.setText(s.value("live/limit_offset", "0", type=str))
            self.live_max_entry_deviation.setText(s.value("live/max_entry_deviation", "200", type=str))
            self.live_limit_offset_from_market.setChecked(
                s.value("live/limit_offset_from_market", True, type=bool)
            )
            self.live_deviation.setText(s.value("live/deviation", "20", type=str))
            self.live_order_comment.setText(s.value("live/order_comment", "HammerDashboard", type=str))
            mode = s.value("live/order_mode", "market", type=str)
            for i in range(self.live_order_mode.count()):
                if self.live_order_mode.itemData(i) == mode:
                    self.live_order_mode.setCurrentIndex(i)
                    break
            self.live_fallback_market.setChecked(s.value("live/fallback_market", False, type=bool))
            self.live_warn_tf_mismatch.setChecked(s.value("live/warn_tf_mismatch", True, type=bool))
        if hasattr(self, "live_telegram_enabled"):
            self.live_telegram_enabled.blockSignals(True)
            self.live_telegram_token.blockSignals(True)
            self.live_telegram_chat_id.blockSignals(True)
            try:
                self.live_telegram_enabled.setChecked(s.value("live/telegram_enabled", False, type=bool))
                self.live_telegram_token.setText(s.value("live/telegram_token", "", type=str))
                self.live_telegram_chat_id.setText(s.value("live/telegram_chat_id", "", type=str))
            finally:
                self.live_telegram_enabled.blockSignals(False)
                self.live_telegram_token.blockSignals(False)
                self.live_telegram_chat_id.blockSignals(False)
        if hasattr(self, "live_dry_run_cb") and hasattr(self, "_live_action_dry_run"):
            self.live_dry_run_cb.blockSignals(True)
            self.live_dry_run_cb.setChecked(s.value("live/dry_run", True, type=bool))
            self.live_dry_run_cb.blockSignals(False)
        self._refresh_live_telegram_status()
        self._refresh_live_perf_hint()

    def _append_live_log_line(self, line: str):
        if not hasattr(self, "live_log_view"):
            return
        self.live_log_view.appendPlainText(line)
        bar = self.live_log_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def _live_log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        print(line, flush=True)
        file_line = f"{datetime.now().isoformat(timespec='seconds')} {line}"
        journal = (self._active_live_journal_dir or "").strip() or LIVE_JOURNAL_DIR
        append_session_log(journal, file_line)
        self._live_log_bridge.line_ready.emit(line)

    def _on_live_connect(self):
        self._save_live_settings()
        self.live_connect_btn.setEnabled(False)
        self._live_log("Connecting to MT5…")
        self._set_app_status("Live: connecting to MT5…")
        self.live_status_label.setText("MT5: connecting…")
        QApplication.processEvents()
        ok, msg = self.mt5_broker.connect(self._live_broker_credentials())
        self.live_connect_btn.setEnabled(True)
        self._live_log(msg)
        if ok:
            info = self.mt5_broker.account_info_dict()
            demo = self.mt5_broker.account_is_demo()
            acct_type = "DEMO" if demo else ("LIVE" if demo is False else "unknown")
            self.live_status_label.setText(
                f"MT5: connected ({acct_type}) — {info.get('login', '?')} | "
                f"Balance {info.get('balance', 0):,.2f} {info.get('currency', '')}"
            )
            if demo is False:
                self._live_log(
                    "[LIVE ACCOUNT] Real money — Safety panel limits will apply when dry run is off."
                )
            elif demo is True:
                self._live_log("[DEMO ACCOUNT] Connected.")
            self._set_app_status(f"Live: MT5 connected ({acct_type.lower()})")
        else:
            self.live_status_label.setText("MT5: connection failed")
            self._set_app_status("Live: MT5 connection failed")
            QMessageBox.warning(self, "MT5 Connect", msg)

    def _on_live_disconnect(self):
        self._live_log("Disconnecting from MT5…")
        self._stop_live_trading()
        self.mt5_broker.disconnect()
        self.live_status_label.setText("MT5: disconnected")
        self._live_log("Disconnected from MT5.")
        self._set_app_status("Live: MT5 disconnected")

    def _build_live_run_config(self) -> live_trading.LiveRunConfig:
        def _f(name: str, default: float) -> float:
            try:
                return float(getattr(self, name).text().strip())
            except (ValueError, AttributeError):
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(float(getattr(self, name).text().strip()))
            except (ValueError, AttributeError):
                return default

        sym = self.live_symbol.text().strip() or "XAUUSD"
        journal_dir = live_journal_dir(DEFAULT_OUTPUT_DIR)

        return live_trading.LiveRunConfig(
            symbol=sym,
            timeframe_label=self.live_timeframe.currentText(),
            volume=_f("live_volume", 0.01),
            magic=_i("live_magic", 88001001),
            max_open_positions=_i("live_max_positions", 1),
            poll_interval_sec=_f("live_poll_sec", 2.0),
            dry_run=self._live_dry_run_checked(),
            history_bars=_i("live_history_bars", 400),
            max_daily_trades=_i("live_max_daily_trades", 10),
            max_daily_loss_usd=_f("live_max_daily_loss", 100.0),
            min_minutes_between_trades=_f("live_min_minutes_between", 0.0),
            max_spread_points=_f("live_max_spread", 50.0),
            demo_accounts_only=self.live_demo_only.isChecked(),
            max_lot_size=_f("live_max_lot_cap", 0.10),
            use_thread_pool_signal_cpu=self._live_thread_pool_checked(),
            use_ray_if_available=self._live_ray_checked(),
            journal_dir=journal_dir,
            order_mode=self._live_order_mode_value(),
            limit_offset_points=_f("live_limit_offset", 0.0),
            price_deviation_points=_i("live_deviation", 20),
            order_comment=(self.live_order_comment.text().strip() or "HammerDashboard")[:31],
            fallback_to_market_on_limit_fail=self.live_fallback_market.isChecked(),
            max_entry_deviation_points=_f("live_max_entry_deviation", 200.0),
            limit_offset_from_market=self.live_limit_offset_from_market.isChecked(),
            telegram_enabled=self.live_telegram_enabled.isChecked(),
            telegram_bot_token=self.live_telegram_token.text().strip(),
            telegram_chat_id=self.live_telegram_chat_id.text().strip(),
            **{k: v for k, v in self._collect_time_filter_kwargs().items() if k != "time_filter_mode"},
        )

    def _cancel_telegram_test_worker(self) -> None:
        worker = getattr(self, "_telegram_test_worker", None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(2000)
        except RuntimeError:
            pass
        self._telegram_test_worker = None

    def _test_telegram_notification(self, test_button: Optional[QPushButton] = None):
        if not self._telegram_widgets_valid():
            QMessageBox.warning(self, "Telegram", "Telegram fields are not available.")
            return
        token = self.live_telegram_token.text().strip()
        chat_id = self.live_telegram_chat_id.text().strip()
        if not token or not chat_id:
            QMessageBox.warning(
                self,
                "Telegram",
                "Enter bot token and chat ID first.\n\n"
                "Token from @BotFather · chat id from @userinfobot",
            )
            return
        if self._telegram_test_worker is not None and self._telegram_test_worker.isRunning():
            QMessageBox.information(self, "Telegram", "A test is already running — please wait.")
            return

        if test_button is not None:
            test_button.setEnabled(False)
            test_button.setText("Sending test…")

        self._telegram_test_worker = TelegramTestWorker(
            token,
            chat_id,
            "Hammer Live — test notification.\nIf you see this, Telegram alerts are configured.",
            self,
        )
        self._telegram_test_worker.finished_ok.connect(
            lambda _detail: self._on_telegram_test_ok(test_button)
        )
        self._telegram_test_worker.failed.connect(
            lambda detail: self._on_telegram_test_fail(detail, test_button)
        )
        self._telegram_test_worker.finished.connect(self._on_telegram_test_worker_finished)
        self._telegram_test_worker.start()

    def _on_telegram_test_worker_finished(self):
        self._telegram_test_worker = None

    def _reset_telegram_test_button(self, test_button: Optional[QPushButton]) -> None:
        if test_button is not None and _qt_widget_valid(test_button):
            test_button.setEnabled(True)
            test_button.setText("Send test notification")

    def _on_telegram_test_ok(self, test_button: Optional[QPushButton]):
        self._reset_telegram_test_button(test_button)
        QMessageBox.information(self, "Telegram", "Test message sent successfully.")
        self._live_log("[TELEGRAM] Test notification sent.")
        if self._telegram_widgets_valid():
            self.live_telegram_status.setText("Test OK")
            self.live_telegram_status.setProperty("ready", True)
            self.live_telegram_status.style().unpolish(self.live_telegram_status)
            self.live_telegram_status.style().polish(self.live_telegram_status)

    def _on_telegram_test_fail(self, detail: str, test_button: Optional[QPushButton]):
        self._reset_telegram_test_button(test_button)
        QMessageBox.warning(
            self,
            "Telegram",
            f"Could not send test message after retries:\n\n{detail}",
        )
        self._live_log(f"[TELEGRAM] Test failed: {detail}")

    def _live_worker_running(self) -> bool:
        return self._live_thread is not None and self._live_thread.isRunning()

    def _on_live_start(self):
        if not self.mt5_broker.is_connected:
            QMessageBox.warning(
                self, "Not connected",
                "Click Connect MT5 first. Live trading requires Windows + MetaTrader5 package.",
            )
            return
        if self._live_worker_running():
            QMessageBox.information(self, "Live", "Live is already running — Stop live first.")
            return

        try:
            live_cfg = self._build_live_run_config()
            pattern, pattern_type, strategy_config, indicator_stack = (
                self._collect_live_strategy_from_parameters()
            )
        except Exception as e:
            QMessageBox.critical(self, "Invalid parameters", f"Could not read strategy settings:\n{e}")
            return

        if pattern_type == "hammer":
            test_lines = logic.run_hammer_direction_self_test(strategy_config)
            if any("FAIL" in ln for ln in test_lines):
                ans = QMessageBox.warning(
                    self,
                    "Direction rules failed self-test",
                    "\n".join(test_lines)
                    + "\n\nStart live anyway? (Not recommended — fix Direction tab first.)",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ans != QMessageBox.Yes:
                    return
            else:
                self._live_log("Direction self-test OK: " + test_lines[0])

        live_tf = live_cfg.timeframe_label
        tf_settings = getattr(strategy_config, "timeframe_settings", None) or {}
        if live_tf not in tf_settings:
            QMessageBox.warning(
                self,
                "Timeframe settings missing",
                f"No RR/SL settings found for live timeframe {live_tf} in Parameters → Timeframes.\n"
                "Set RR and Max SL for that row, then Start live again.",
            )
            return

        sym_ok, sym_msg, resolved_sym = self.mt5_broker.resolve_and_ensure_symbol(live_cfg.symbol)
        if not sym_ok:
            QMessageBox.warning(self, "Symbol not ready", sym_msg)
            self._live_log(f"[BLOCKED] {sym_msg}")
            return
        if resolved_sym != live_cfg.symbol.strip():
            live_cfg = dataclasses.replace(live_cfg, symbol=resolved_sym)
            self.live_symbol.setText(resolved_sym)
        self._live_log(sym_msg)

        self._refresh_live_strategy_summary()
        is_demo = self.mt5_broker.account_is_demo()
        if not live_cfg.sessions_enabled and self._time_filter_mode() == "sessions":
            QMessageBox.warning(
                self, "No Sessions Selected",
                "Select at least one trading session (Asian / London / US), "
                "or switch to Indian time mode.",
            )
            return

        real_money = is_demo is False

        if live_cfg.demo_accounts_only and real_money:
            QMessageBox.warning(self, "Demo-only mode", "Demo-only is on but this is a LIVE account.")
            return
        risk_err = live_trading.validate_risk_config(live_cfg, real_money=real_money)
        if risk_err and not live_cfg.dry_run:
            QMessageBox.warning(self, "Set risk limits", risk_err)
            return

        preflight = self._live_preflight_notes(live_cfg)
        preflight.extend(self._live_indicator_preflight_lines(indicator_stack))
        if getattr(live_cfg, "ist_time_filter_enabled", False):
            preflight.append(
                f"Filter: Indian time {live_cfg.ist_time_start}–{live_cfg.ist_time_end} IST"
            )
        else:
            sess_on = live_cfg.sessions_enabled or list(sessions.SESSION_ORDER)
            if set(sess_on) != set(sessions.SESSION_ORDER):
                preflight.append(f"Filter: sessions {', '.join(sess_on)}")
            else:
                preflight.append("Filter: all sessions (Asian / London / US)")

        # NEXT_CANDLE_CLOSE uses the forming bar live — warn so users don't expect backtest parity
        entry_rules = []
        for attr in ("entry_rule", "inverted_entry_rule"):
            rule = getattr(strategy_config, attr, None)
            if rule is not None:
                entry_rules.append(str(getattr(rule, "value", rule)))
        if any("CLOSE" in r.upper() for r in entry_rules):
            preflight.append(
                "WARNING: Entry uses NEXT_CANDLE_CLOSE — live evaluates the forming bar "
                "(close not final). Prefer NEXT_CANDLE_OPEN for live/backtest parity."
            )

        if pattern_type == "hammer":
            preflight.append(f"Direction: {logic.describe_hammer_direction_matrix(strategy_config)}")
            preflight.append(f"Entry/Exit: {logic.describe_hammer_entry_exit(strategy_config)}")
            preflight.append(
                "These Direction + Entry/Exit rows are the same objects used in backtest "
                "(lots/risk caps still come from the Live panel, not Run Settings)."
            )
        elif pattern_type in ("hammer_with_candles", "hammer_context"):
            preflight.append(f"Rules: {hammer_context_logic.describe_hammer_context_rules(strategy_config)}")
            preflight.append(f"Entry/Exit: {hammer_context_logic.describe_hammer_context_entry_exit(strategy_config)}")
        tf_set = tf_settings.get(live_tf)
        if tf_set is not None:
            preflight.append(
                f"Logic params for {live_tf}: RR={tf_set.rr_multiple} max SL=${tf_set.max_sl_usd} "
                f"(from Parameters → Timeframes)."
            )
        summary = (
            f"• {pattern} on {live_cfg.symbol} @ {live_cfg.timeframe_label} "
            f"magic={live_cfg.magic} lots={live_cfg.volume}"
        )
        pre_body = "\n".join(preflight + [""] + [summary])
        ans = QMessageBox.question(
            self, "Start live — confirm", f"{pre_body}\n\nStart live?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if ans != QMessageBox.Yes:
            self._live_log("Start live cancelled at pre-flight checklist.")
            return

        if not live_cfg.dry_run:
            acct = "LIVE / real money" if real_money else "this account"
            ans = QMessageBox.warning(
                self, "Send real orders?",
                f"Dry run is OFF — orders will be sent to MT5 ({acct}).\n\n{pre_body}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return

        self._save_live_settings()
        self._active_live_journal_dir = live_cfg.journal_dir or LIVE_JOURNAL_DIR
        self._live_engine = live_trading.LiveTradingEngine(
            broker=self.mt5_broker,
            live_config=live_cfg,
            pattern_type=pattern_type,
            pattern_label=pattern,
            strategy_config=strategy_config,
            indicator_stack=indicator_stack,
            log=self._live_log,
        )
        self._live_thread = LiveTradingThread(self._live_engine)
        self._live_thread.finished_cleanly.connect(self._on_live_worker_finished)
        self._live_thread.crashed.connect(self._on_live_worker_crashed)
        self._live_thread.start()
        self._live_log(
            f"Started {pattern} | {live_cfg.symbol} @ {live_cfg.timeframe_label} | "
            f"lots={live_cfg.volume} magic={live_cfg.magic}"
        )
        self._live_log(live_trading.summarize_strategy_params(
            strategy_config, live_cfg.timeframe_label, pattern_type,
        ))

        self._live_health_timer.start()
        self.live_start_btn.setEnabled(False)
        self.live_stop_btn.setEnabled(True)
        dry = "ON (no orders)" if live_cfg.dry_run else "OFF (orders enabled)"
        self._set_app_status(f"Live: running, dry run {dry}")
        if live_cfg.telegram_enabled and live_cfg.telegram_bot_token and live_cfg.telegram_chat_id:
            self._live_log("[TELEGRAM] Alerts enabled for all order events.")
        elif live_cfg.telegram_enabled:
            self._live_log("[TELEGRAM] Enabled but token or chat id missing — no alerts will send.")
        self.live_dock.show()
        self.live_dock.raise_()

    def _on_live_worker_finished(self):
        if self._live_thread is not None and not self._live_thread.isRunning():
            self._live_health_timer.stop()
            self._finalize_live_worker_stopped()

    def _on_live_worker_crashed(self, detail: str):
        self._live_log(f"[FATAL] Live worker crashed:\n{detail}")
        self._live_health_timer.stop()
        QMessageBox.critical(
            self, "Live worker stopped",
            "Live trading stopped unexpectedly. Check Live — Log.",
        )
        self._finalize_live_worker_stopped()

    def _finalize_live_worker_stopped(self):
        self._live_engine = None
        self._live_thread = None
        self._active_live_journal_dir = None
        if hasattr(self, "live_start_btn"):
            self.live_start_btn.setEnabled(True)
        if hasattr(self, "live_stop_btn"):
            self.live_stop_btn.setEnabled(False)
        self._set_app_status("Live: stopped")

    def _check_live_worker_health(self):
        eng = self._live_engine
        if eng is None:
            return
        import time as _time

        stale_sec = _time.monotonic() - eng.last_heartbeat_mono
        if stale_sec > 90:
            self._live_log(f"[WARN] No live heartbeat for {stale_sec:.0f}s.")

    def _stop_live_trading(self):
        if self._live_worker_running():
            self._live_log("Stop live requested — shutting down…")
            self._set_app_status("Live: stopping…")
        if self._live_engine is not None:
            self._live_engine.request_stop()
        if self._live_thread is not None and self._live_thread.isRunning():
            self._live_thread.request_stop()
            self._live_thread.wait(15000)
        self._live_health_timer.stop()
        self._live_engine = None
        self._live_thread = None
        self._active_live_journal_dir = None
        if hasattr(self, "live_start_btn"):
            self.live_start_btn.setEnabled(True)
        if hasattr(self, "live_stop_btn"):
            self.live_stop_btn.setEnabled(False)
        self._set_app_status("Live: stopped")

    def _build_results_panel(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(8, 14, 14, 14)

        self.results_tabs = QTabWidget()
        self.results_tabs.setObjectName("resultsMainTabs")
        layout.addWidget(self.results_tabs, 1)

        metrics_tab = QWidget()
        metrics_layout = QVBoxLayout(metrics_tab)
        self.results_color_legend = QLabel(
            'Color key: '
            '<span style="background-color:#D7F5DD; padding:2px 10px; border-radius:4px;">BUY</span> '
            '<span style="background-color:#FADBD8; padding:2px 10px; border-radius:4px;">SELL</span> '
            '<span style="background-color:#D2E3FC; padding:2px 10px; border-radius:4px;">Candle bias</span>'
            '<br>Trade count: <b>Total Trades</b> is WIN + LOSS only. '
            'Overlap skips are <b>Skipped Overlap</b> (not in Total Trades). '
            'Taken setups = Total Trades + Skipped Overlap + Still Open. '
            'By Timeframe lists each TF three times (best / candle-bias / worst) — do not add those rows. '
            '30m often has more skips than 1h because signals are denser while a trade is still open.'
            '<br><b>By Session</b>: Asian 00:00–07:59 · London 08:00–15:59 · US 16:00–23:59 '
            f'({sessions.BROKER_TIME_LABEL} or IST — Timeframes tab). Filter sessions / IST window there. '
            'Session <b>Net PnL / wins / losses</b> sum to Overall; session Max DD and Return % are '
            '<b>as if that session alone</b> (not additive across sessions).'
        )
        self.results_color_legend.setTextFormat(Qt.RichText)
        self.results_color_legend.setObjectName("sectionHint")
        metrics_layout.addWidget(self.results_color_legend)

        self.buy_sell_summary = QLabel(
            "BUY vs SELL summary will appear here after a backtest."
        )
        self.buy_sell_summary.setWordWrap(True)
        self.buy_sell_summary.setTextFormat(Qt.RichText)
        self.buy_sell_summary.setObjectName("liveSummaryCard")
        metrics_layout.addWidget(self.buy_sell_summary)

        self.metrics_sub_tabs = QTabWidget()
        metrics_layout.addWidget(self.metrics_sub_tabs)

        self.overall_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.overall_table), "Overall")

        self.by_timeframe_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.by_timeframe_table), "By Timeframe")

        self.by_direction_table = self._make_metrics_table()
        dir_wrap = QWidget()
        dir_layout = QVBoxLayout(dir_wrap)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        self.buy_sell_direction_note = QLabel(
            "Full BUY / SELL breakdown by exit model. See the green summary card above for the quick verdict."
        )
        self.buy_sell_direction_note.setObjectName("sectionHint")
        self.buy_sell_direction_note.setWordWrap(True)
        dir_layout.addWidget(self.buy_sell_direction_note)
        dir_layout.addWidget(self._wrap_table(self.by_direction_table), 1)
        self.metrics_sub_tabs.addTab(dir_wrap, "By Direction")

        self.by_session_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.by_session_table), "By Session")

        self.by_market_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.by_market_table), "By Market")

        self.by_year_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.by_year_table), "By Year")

        self.by_month_table = self._make_metrics_table()
        self.metrics_sub_tabs.addTab(self._wrap_table(self.by_month_table), "By Month")

        self.results_tabs.addTab(metrics_tab, "Metrics")

        trades_tab = QWidget()
        trades_layout = QVBoxLayout(trades_tab)
        trades_hint = QLabel(
            "Individual trades from the last run. "
            "<b>Row background:</b> green = BUY, red = SELL, blue = candle-bias exit model. "
            "<b>Text color:</b> <span style='color:#137333'>green = WIN</span>, "
            "<span style='color:#C5221F'>red = LOSS</span>.<br>"
            "Use <b>Show</b> / <b>Sort</b> to inspect wins, losses, newest (near end date), or oldest "
            "(near start date). <b>Double-click a row</b> (or select + Inspect) for the chart — "
            "then use <b>↑↓</b> to browse trades one by one."
        )
        trades_hint.setTextFormat(Qt.RichText)
        trades_hint.setObjectName("sectionHint")
        trades_hint.setWordWrap(True)
        trades_layout.addWidget(trades_hint)

        trade_filter_row = QHBoxLayout()
        trade_filter_row.addWidget(QLabel("Exit model:"))
        self.trade_exit_model_filter = QComboBox()
        self.trade_exit_model_filter.addItem("Worst case", "worst_case")
        self.trade_exit_model_filter.addItem("Candle bias", "candle_bias")
        self.trade_exit_model_filter.addItem("Best case", "best_case")
        self.trade_exit_model_filter.addItem("All models", "")
        self.trade_exit_model_filter.setToolTip(
            "Filter the trade list. Inspect uses the selected row’s exit model."
        )
        self.trade_exit_model_filter.currentIndexChanged.connect(self._refresh_trade_ledger_view)
        trade_filter_row.addWidget(self.trade_exit_model_filter)

        trade_filter_row.addWidget(QLabel("Show:"))
        self.trade_outcome_filter = QComboBox()
        self.trade_outcome_filter.addItem("All outcomes", "all")
        self.trade_outcome_filter.addItem("Wins only", "WIN")
        self.trade_outcome_filter.addItem("Losses only", "LOSS")
        self.trade_outcome_filter.setToolTip("Keep only winning or losing trades for inspection.")
        self.trade_outcome_filter.currentIndexChanged.connect(self._refresh_trade_ledger_view)
        trade_filter_row.addWidget(self.trade_outcome_filter)

        trade_filter_row.addWidget(QLabel("Sort:"))
        self.trade_sort_filter = QComboBox()
        self.trade_sort_filter.addItem("Newest first (near end date)", "newest")
        self.trade_sort_filter.addItem("Oldest first (near start date)", "oldest")
        self.trade_sort_filter.addItem("Losses first", "losses_first")
        self.trade_sort_filter.addItem("Wins first", "wins_first")
        self.trade_sort_filter.addItem("Biggest loss first", "biggest_loss")
        self.trade_sort_filter.addItem("Biggest win first", "biggest_win")
        self.trade_sort_filter.setToolTip(
            "Order the list before the row cap. Newest = closer to the backtest end date; "
            "Oldest = closer to the start date."
        )
        self.trade_sort_filter.currentIndexChanged.connect(self._refresh_trade_ledger_view)
        trade_filter_row.addWidget(self.trade_sort_filter)

        inspect_btn = QPushButton("Inspect selected trade")
        inspect_btn.setObjectName("secondaryButton")
        inspect_btn.setToolTip("Open candlestick chart for the highlighted trade.")
        inspect_btn.clicked.connect(self._inspect_selected_trade)
        trade_filter_row.addWidget(inspect_btn)
        trade_filter_row.addStretch()
        trades_layout.addLayout(trade_filter_row)

        self.trade_ledger_note = QLabel("Run a backtest to populate the trade list.")
        self.trade_ledger_note.setObjectName("sectionHint")
        trades_layout.addWidget(self.trade_ledger_note)
        self.trade_ledger_table = self._make_metrics_table()
        self.trade_ledger_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.trade_ledger_table.setSelectionMode(QTableWidget.SingleSelection)
        self.trade_ledger_table.cellDoubleClicked.connect(
            lambda _r, _c: self._inspect_selected_trade()
        )
        trades_layout.addWidget(self.trade_ledger_table, 1)
        self.results_tabs.addTab(trades_tab, "Trades")
        self._trade_ledger_display_df = None
        self._trade_ledger_full_df = None
        self.last_backtest_config = None

        # Equity + Price side-by-side for easy visual comparison
        equity_price_tab = QWidget()
        equity_price_layout = QVBoxLayout(equity_price_tab)
        equity_price_hint = QLabel(
            "Equity (left) vs price line (right). Price is close only — no trade markers. "
            "<b>Click a chart to zoom.</b>"
        )
        equity_price_hint.setTextFormat(Qt.RichText)
        equity_price_hint.setObjectName("sectionHint")
        equity_price_hint.setWordWrap(True)
        equity_price_layout.addWidget(equity_price_hint)

        side_by_side = QHBoxLayout()
        side_by_side.setSpacing(12)

        equity_panel = QWidget()
        equity_col = QVBoxLayout(equity_panel)
        equity_col.setContentsMargins(0, 0, 0, 0)
        equity_col_title = QLabel("<b>Equity</b>")
        equity_col_title.setTextFormat(Qt.RichText)
        equity_col.addWidget(equity_col_title)
        self.equity_chart_scroll = QScrollArea()
        self.equity_chart_scroll.setWidgetResizable(True)
        self.equity_chart_inner = QWidget()
        self.equity_chart_layout = QVBoxLayout(self.equity_chart_inner)
        self.equity_chart_layout.addWidget(
            self._empty_state_label("Run a backtest to see the equity curve here.")
        )
        self.equity_chart_layout.addStretch()
        self.equity_chart_scroll.setWidget(self.equity_chart_inner)
        equity_col.addWidget(self.equity_chart_scroll, 1)

        price_panel = QWidget()
        price_col = QVBoxLayout(price_panel)
        price_col.setContentsMargins(0, 0, 0, 0)
        price_col_title = QLabel("<b>Price</b>")
        price_col_title.setTextFormat(Qt.RichText)
        price_col.addWidget(price_col_title)
        self.price_chart_scroll = QScrollArea()
        self.price_chart_scroll.setWidgetResizable(True)
        self.price_chart_inner = QWidget()
        self.price_chart_layout = QVBoxLayout(self.price_chart_inner)
        self.price_chart_layout.addWidget(
            self._empty_state_label("Run a backtest to see the price line here.")
        )
        self.price_chart_layout.addStretch()
        self.price_chart_scroll.setWidget(self.price_chart_inner)
        price_col.addWidget(self.price_chart_scroll, 1)

        side_by_side.addWidget(equity_panel, 1)
        side_by_side.addWidget(price_panel, 1)
        equity_price_layout.addLayout(side_by_side, 1)
        self.results_tabs.addTab(equity_price_tab, "Equity & Price")

        charts_tab = QWidget()
        charts_outer_layout = QVBoxLayout(charts_tab)
        self.charts_scroll = QScrollArea()
        self.charts_scroll.setWidgetResizable(True)
        self.charts_inner = QWidget()
        self.charts_layout = QVBoxLayout(self.charts_inner)
        self.charts_layout.addWidget(self._empty_state_label("Run a backtest to see charts here."))
        self.charts_layout.addStretch()
        self.charts_scroll.setWidget(self.charts_inner)
        charts_outer_layout.addWidget(self.charts_scroll)
        self.results_tabs.addTab(charts_tab, "Charts")

        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)

        history_hint = QLabel(
            "Every backtest you run gets saved here automatically -- unless the parameters "
            "exactly match a run that's already saved, in which case it's skipped (same "
            "settings, same data, nothing new to record)."
        )
        history_hint.setObjectName("sectionHint")
        history_hint.setWordWrap(True)
        history_layout.addWidget(history_hint)

        self.history_table = QTableWidget()
        self.history_table.setAlternatingRowColors(True)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.itemDoubleClicked.connect(self._open_history_run_folder)
        _configure_table_widget_mac(self.history_table)
        history_layout.addWidget(self.history_table, 1)

        history_button_row = QHBoxLayout()
        refresh_history_btn = QPushButton("Refresh")
        refresh_history_btn.setObjectName("secondaryButton")
        refresh_history_btn.clicked.connect(self._refresh_run_history_table)
        export_history_btn = QPushButton("Export to Excel")
        export_history_btn.setObjectName("secondaryButton")
        export_history_btn.clicked.connect(self._export_run_history_to_excel)
        history_button_row.addWidget(refresh_history_btn)
        history_button_row.addWidget(export_history_btn)
        history_button_row.addStretch()
        history_layout.addLayout(history_button_row)

        self.results_tabs.addTab(history_tab, "History")

        compare_tab = QWidget()
        compare_layout = QVBoxLayout(compare_tab)
        compare_hint = QLabel(
            "Pick two saved runs from history. <b>Overall</b> compares each exit model; "
            "<b>By Session</b> compares Asian / London / US (worst case) side-by-side."
        )
        compare_hint.setObjectName("sectionHint")
        compare_hint.setWordWrap(True)
        compare_layout.addWidget(compare_hint)
        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("Run A:"))
        self.compare_combo_a = QComboBox()
        self.compare_combo_a.setMinimumWidth(200)
        pick_row.addWidget(self.compare_combo_a, 1)
        pick_row.addWidget(QLabel("Run B:"))
        self.compare_combo_b = QComboBox()
        self.compare_combo_b.setMinimumWidth(200)
        pick_row.addWidget(self.compare_combo_b, 1)
        compare_btn = QPushButton("Compare")
        compare_btn.setObjectName("secondaryButton")
        compare_btn.clicked.connect(self._run_history_compare)
        pick_row.addWidget(compare_btn)
        compare_layout.addLayout(pick_row)
        compare_layout.addWidget(QLabel("<b>Overall — by exit model</b>"))
        self.compare_results_table = self._make_metrics_table()
        compare_layout.addWidget(self.compare_results_table, 1)
        compare_layout.addWidget(QLabel("<b>By session — worst case</b>"))
        self.compare_session_table = self._make_metrics_table()
        compare_layout.addWidget(self.compare_session_table, 1)
        self.results_tabs.addTab(compare_tab, "Compare")

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        open_output_btn = QPushButton("Open Output Folder")
        open_output_btn.setObjectName("secondaryButton")
        open_output_btn.setToolTip("Opens the last run's output folder (CSVs, trade ledger).")
        open_output_btn.clicked.connect(self._open_output_folder)
        open_ledger_btn = QPushButton("Open trade_ledger.csv")
        open_ledger_btn.setObjectName("secondaryButton")
        open_ledger_btn.clicked.connect(self._open_trade_ledger_csv)
        export_run_btn = QPushButton("Export Last Run Metrics")
        export_run_btn.setObjectName("secondaryButton")
        export_run_btn.setToolTip("Excel workbook with summary tables from the last backtest.")
        export_run_btn.clicked.connect(self._export_last_run_metrics_excel)
        open_charts_btn = QPushButton("Open Charts Folder")
        open_charts_btn.setObjectName("secondaryButton")
        open_charts_btn.clicked.connect(self._open_plots_folder)
        button_row.addWidget(open_output_btn)
        button_row.addWidget(open_ledger_btn)
        button_row.addWidget(export_run_btn)
        button_row.addWidget(open_charts_btn)
        button_row.addStretch()
        toolbar = QFrame()
        toolbar.setObjectName("resultsToolbar")
        toolbar.setLayout(button_row)
        layout.addWidget(toolbar)

        # Scroll wrapper keeps the panel resizable below its natural width
        # (wide tables/toolbars otherwise lock the dock divider in place).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(wrap)
        return scroll

    @staticmethod
    def _empty_state_label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("emptyState")
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        return lab

    def _make_metrics_table(self) -> QTableWidget:
        table = QTableWidget()
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(False)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        _configure_table_widget_mac(table)
        return table

    def _wrap_table(self, table: QTableWidget) -> QWidget:
        _configure_table_widget_mac(table)
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        l.addWidget(table)
        return w

    # ------------------------------------------------------------------
    # CONFIG BUILDING FROM FORM VALUES -- same shape as dashboard.py
    # ------------------------------------------------------------------
    def _parse_value(self, raw: str, fallback: Any) -> Any:
        if isinstance(fallback, bool):
            return raw
        if isinstance(fallback, int):
            return int(float(raw))
        if isinstance(fallback, float):
            return float(raw)
        return raw

    def _widget_value(self, widget) -> Any:
        if isinstance(widget, QLineEdit):
            return widget.text()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            data = widget.currentData()
            if data is not None:
                return str(data)
            return widget.currentText()
        return None

    def _read_ui_field(self, name: str, default_val: Any, ftype: str) -> Any:
        """Read a Parameters-panel widget when present; otherwise use logic default."""
        widget = self.field_widgets.get(name)
        if widget is None:
            return default_val
        raw = self._widget_value(widget)
        if ftype == FIELD_TYPE_CHECK:
            return bool(raw)
        if ftype == FIELD_TYPE_DROPDOWN:
            enum_cls = type(default_val)
            try:
                return enum_cls(raw)
            except (ValueError, TypeError):
                return default_val
        raw_str = (raw if raw is not None else "")
        if isinstance(raw_str, str) and not raw_str.strip():
            return default_val
        try:
            return self._parse_value(raw_str, default_val)
        except (ValueError, TypeError):
            return default_val

    def _collect_live_strategy_from_parameters(self):
        """
        Build the same strategy + indicator objects as backtest Run, from the current
        Parameters panel (pattern, shape, entry/exit, risk, timeframe RR/SL, indicators).
        """
        if hasattr(self, "live_pattern_combo") and hasattr(self, "pattern_combo"):
            live_pat = self.live_pattern_combo.currentText()
            if live_pat and self.pattern_combo.findText(live_pat) >= 0:
                if self.pattern_combo.currentText() != live_pat:
                    self.pattern_combo.blockSignals(True)
                    self.pattern_combo.setCurrentText(live_pat)
                    self.pattern_combo.blockSignals(False)
                    self._apply_pattern_field_visibility()
        self._sync_live_pattern_from_dashboard()
        self._apply_pattern_field_visibility()

        pattern = self.pattern_combo.currentText()
        pattern_type = PATTERN_REGISTRY.get(pattern, {}).get("pattern_type", "hammer")
        strategy_config = self._build_strategy_config()
        indicator_stack = self._build_indicator_stack()
        return pattern, pattern_type, strategy_config, indicator_stack

    def _build_doji_ratio_config(self) -> doji_logic.DojiRatioConfig:
        defaults = doji_logic.DojiRatioConfig()
        kwargs = {}
        for name, _, ftype, _ in DOJI_SHAPE_FIELDS:
            field_name = name.replace("doji_", "", 1) if name.startswith("doji_") else name
            default_val = getattr(defaults, field_name)
            kwargs[field_name] = self._read_ui_field(name, default_val, ftype)
        return doji_logic.DojiRatioConfig(**kwargs)

    def _build_hammer_ratio_config(self) -> logic.HammerRatioConfig:
        defaults = logic.HammerRatioConfig()
        kwargs = {}
        for name, _, ftype, _ in HAMMER_SHAPE_FIELDS:
            default_val = getattr(defaults, name)
            kwargs[name] = self._read_ui_field(name, default_val, ftype)
        return logic.HammerRatioConfig(**kwargs)

    def _effective_hammer_wick_side_value(self) -> str:
        """Wick side used for hammer detection (matches backtest / live)."""
        defaults = logic.StrategyConfig()
        cg = self._read_ui_field("classic_green", defaults.classic_green, FIELD_TYPE_DROPDOWN)
        cr = self._read_ui_field("classic_red", defaults.classic_red, FIELD_TYPE_DROPDOWN)
        ig = self._read_ui_field("inverted_green", defaults.inverted_green, FIELD_TYPE_DROPDOWN)
        ir = self._read_ui_field("inverted_red", defaults.inverted_red, FIELD_TYPE_DROPDOWN)
        classic = logic.coerce_trade_action(cg) != logic.TradeAction.NO or logic.coerce_trade_action(cr) != logic.TradeAction.NO
        inverted = logic.coerce_trade_action(ig) != logic.TradeAction.NO or logic.coerce_trade_action(ir) != logic.TradeAction.NO
        ui_wick = self._read_ui_field(
            "hammer_wick_side", defaults.hammer_wick_side, FIELD_TYPE_DROPDOWN,
        )
        resolved = logic.resolve_hammer_wick_side(classic, inverted, ui_wick)
        return resolved.value

    def _build_hammer_strategy_config(self) -> logic.StrategyConfig:
        hammer_ratios = self._build_hammer_ratio_config()
        kwargs = {"hammer_ratios": hammer_ratios}
        defaults = logic.StrategyConfig()

        for name, _, ftype, _ in STRATEGY_FIELDS:
            default_val = getattr(defaults, name)
            kwargs[name] = self._read_ui_field(name, default_val, ftype)

        timeframe_settings = {}
        for tf in TIMEFRAME_LABELS:
            tw = self.timeframe_widgets.get(tf)
            if tw is None:
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                if defaults_tf is not None:
                    timeframe_settings[tf] = logic.TimeframeSetting(
                        rr_multiple=defaults_tf.rr_multiple,
                        max_sl_usd=defaults_tf.max_sl_usd,
                    )
                continue
            try:
                rr = float(tw["rr"].text())
                sl = float(tw["sl"].text())
            except (ValueError, AttributeError, KeyError):
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                rr = defaults_tf.rr_multiple if defaults_tf else 2.0
                sl = defaults_tf.max_sl_usd if defaults_tf else 50.0
            timeframe_settings[tf] = logic.TimeframeSetting(rr_multiple=rr, max_sl_usd=sl)
        kwargs["timeframe_settings"] = timeframe_settings
        cfg = logic.StrategyConfig(**kwargs)
        return cfg

    def _build_indicator_stack(self) -> IndicatorStackConfig:
        def _chk(name, default=False):
            w = self.field_widgets.get(name)
            return w.isChecked() if isinstance(w, QCheckBox) else default

        def _txt(name, default):
            w = self.field_widgets.get(name)
            if not isinstance(w, QLineEdit):
                return default
            try:
                return self._parse_value(w.text(), default)
            except (ValueError, TypeError):
                return default

        combine_raw = "ALL"
        w = self.field_widgets.get("indicators_combine_mode")
        if isinstance(w, QComboBox):
            combine_raw = w.currentText()

        return IndicatorStackConfig(
            combine_mode=IndicatorCombineMode(combine_raw),
            supertrend=SuperTrendConfig(
                enabled="supertrend" in self.added_indicator_ids,
                atr_period=int(_txt("indicators_st_atr_period", 10)),
                multiplier=float(_txt("indicators_st_multiplier", 3.0)),
                apply_trade_filter=_chk("indicators_st_apply_filter", True),
            ),
            vwap=VWAPConfig(
                enabled="vwap" in self.added_indicator_ids,
                apply_trade_filter=_chk("indicators_vwap_apply_filter", True),
            ),
        )

    def _build_doji_strategy_config(self) -> doji_logic.DojiStrategyConfig:
        doji_ratios = self._build_doji_ratio_config()
        kwargs = {"doji_ratios": doji_ratios}
        defaults = doji_logic.DojiStrategyConfig()

        doji_strategy_fields = [
            ("doji_style", "doji_style"),
            ("doji_direction_mode", "doji_direction_mode"),
            ("doji_fallback_direction", "fallback_direction"),
            ("doji_green_direction", "green_direction"),
            ("doji_red_direction", "red_direction"),
            ("doji_allow_green_trades", "allow_green_trades"),
            ("doji_allow_red_trades", "allow_red_trades"),
        ]
        for ui_name, cfg_name in doji_strategy_fields:
            default_val = getattr(defaults, cfg_name)
            ftype = FIELD_TYPE_CHECK if isinstance(default_val, bool) else FIELD_TYPE_DROPDOWN
            kwargs[cfg_name] = self._read_ui_field(ui_name, default_val, ftype)

        for name, _, ftype, _ in DOJI_ENTRY_EXIT_FIELDS + RISK_CONTROL_FIELDS:
            default_val = getattr(defaults, name)
            kwargs[name] = self._read_ui_field(name, default_val, ftype)

        timeframe_settings = {}
        for tf in TIMEFRAME_LABELS:
            tw = self.timeframe_widgets.get(tf)
            if tw is None:
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                if defaults_tf is not None:
                    timeframe_settings[tf] = logic.TimeframeSetting(
                        rr_multiple=defaults_tf.rr_multiple,
                        max_sl_usd=defaults_tf.max_sl_usd,
                    )
                continue
            try:
                rr = float(tw["rr"].text())
                sl = float(tw["sl"].text())
            except (ValueError, AttributeError, KeyError):
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                rr = defaults_tf.rr_multiple if defaults_tf else 2.0
                sl = defaults_tf.max_sl_usd if defaults_tf else 50.0
            timeframe_settings[tf] = logic.TimeframeSetting(rr_multiple=rr, max_sl_usd=sl)
        kwargs["timeframe_settings"] = timeframe_settings

        return doji_logic.DojiStrategyConfig(**kwargs)

    def _build_strategy_config(self):
        pattern = self.pattern_combo.currentText()
        if pattern == "Doji":
            return self._build_doji_strategy_config()
        if pattern == "Hammer with candles":
            return self._build_hammer_context_strategy_config()
        return self._build_hammer_strategy_config()

    def _build_context_side_ratio_config(self, side: str) -> logic.HammerRatioConfig:
        prefix = "buy_" if side == "buy" else "sell_"
        defaults = logic.HammerRatioConfig()
        kwargs = {}
        for name in HAMMER_RATIO_FIELD_NAMES:
            default_val = getattr(defaults, name)
            ftype = FIELD_TYPE_CHECK if isinstance(default_val, bool) else FIELD_TYPE_TEXT
            kwargs[name] = self._read_ui_field(f"{prefix}{name}", default_val, ftype)
        return logic.HammerRatioConfig(**kwargs)

    def _build_hammer_context_strategy_config(self) -> hammer_context_logic.HammerContextConfig:
        defaults = hammer_context_logic.HammerContextConfig()
        kwargs = {
            "buy_hammer_ratios": self._build_context_side_ratio_config("buy"),
            "sell_hammer_ratios": self._build_context_side_ratio_config("sell"),
            "buy_require_wick": self._read_ui_field(
                "buy_require_wick", defaults.buy_require_wick, FIELD_TYPE_CHECK,
            ),
            "sell_require_wick": self._read_ui_field(
                "sell_require_wick", defaults.sell_require_wick, FIELD_TYPE_CHECK,
            ),
        }

        for name, _, ftype, _ in CONTEXT_PATTERN_FIELDS:
            attr = CONTEXT_UI_ATTR_OVERRIDES.get(name, name)
            default_val = getattr(defaults, attr)
            kwargs[attr] = self._read_ui_field(name, default_val, ftype)

        classic_fields = CLASSIC_ENTRY_EXIT_FIELDS + RISK_CONTROL_FIELDS
        for name, _, ftype, _ in classic_fields:
            default_val = getattr(defaults, name)
            kwargs[name] = self._read_ui_field(name, default_val, ftype)

        for name, _, ftype, _ in INVERTED_ENTRY_EXIT_FIELDS:
            default_val = getattr(defaults, name)
            kwargs[name] = self._read_ui_field(name, default_val, ftype)

        timeframe_settings = {}
        for tf in TIMEFRAME_LABELS:
            tw = self.timeframe_widgets.get(tf)
            if tw is None:
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                if defaults_tf is not None:
                    timeframe_settings[tf] = logic.TimeframeSetting(
                        rr_multiple=defaults_tf.rr_multiple,
                        max_sl_usd=defaults_tf.max_sl_usd,
                    )
                continue
            try:
                rr = float(tw["rr"].text())
                sl = float(tw["sl"].text())
            except (ValueError, AttributeError, KeyError):
                defaults_tf = logic.DEFAULT_TIMEFRAME_SETTINGS.get(tf)
                rr = defaults_tf.rr_multiple if defaults_tf else 2.0
                sl = defaults_tf.max_sl_usd if defaults_tf else 50.0
            timeframe_settings[tf] = logic.TimeframeSetting(rr_multiple=rr, max_sl_usd=sl)
        kwargs["timeframe_settings"] = timeframe_settings
        return hammer_context_logic.HammerContextConfig(**kwargs)

    def _build_backtest_config(self, strategy_config) -> backtest.BacktestConfig:
        defaults = backtest.BacktestConfig()
        pattern = self.pattern_combo.currentText()
        pattern_type = PATTERN_REGISTRY.get(pattern, {}).get("pattern_type", "hammer")
        kwargs = {"strategy_config": strategy_config, "pattern_type": pattern_type}

        for name, _, ftype, _ in BACKTEST_FIELDS:
            widget = self.field_widgets[name]
            default_val = getattr(defaults, name)
            raw = self._widget_value(widget)

            if name in ("start_date", "end_date"):
                raw_str = (raw or "").strip()
                kwargs[name] = datetime.strptime(raw_str, "%Y-%m-%d") if raw_str else None
                continue

            if ftype == FIELD_TYPE_CHECK:
                kwargs[name] = bool(raw)
            elif ftype == FIELD_TYPE_DROPDOWN:
                if name == "position_sizing_mode":
                    kwargs[name] = backtest.PositionSizingMode(raw)
                elif name == "market_data":
                    raw_m = str(raw or "spot").strip().lower()
                    if raw_m in ("auto", "legacy", "default", ""):
                        raw_m = "spot"
                    try:
                        kwargs[name] = backtest.MarketDataSource(raw_m)
                    except ValueError:
                        kwargs[name] = backtest.MarketDataSource.SPOT
                else:
                    kwargs[name] = raw
            else:
                kwargs[name] = self._parse_value(raw, default_val)

        selected_timeframes = [
            tf for tf, cb in self.timeframe_enabled_widgets.items() if cb.isChecked()
        ]
        kwargs["timeframes_to_test"] = selected_timeframes
        tf_kwargs = self._collect_time_filter_kwargs()
        kwargs["sessions_enabled"] = tf_kwargs["sessions_enabled"]
        kwargs["session_clock"] = tf_kwargs["session_clock"]
        kwargs["broker_utc_offset_hours"] = tf_kwargs["broker_utc_offset_hours"]
        kwargs["ist_time_filter_enabled"] = tf_kwargs["ist_time_filter_enabled"]
        kwargs["ist_time_start"] = tf_kwargs["ist_time_start"]
        kwargs["ist_time_end"] = tf_kwargs["ist_time_end"]
        kwargs["output_root"] = DEFAULT_OUTPUT_DIR
        kwargs["run_name"] = f"dashboard_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        kwargs["indicator_stack"] = self._build_indicator_stack()

        # Absolute data_root; market_data picks spot / futures / both.
        raw_root = str(kwargs.get("data_root") or DEFAULT_DATA_DIR)
        resolved_root = backtest.resolve_data_root(raw_root, anchor_dir=APP_DIR)
        raw_sym = str(kwargs.get("symbol") or "XAUUSD").strip()
        market_pref = kwargs.get("market_data", backtest.MarketDataSource.SPOT)

        if market_pref == backtest.MarketDataSource.BOTH:
            # Keep parent data/ so expand_market_targets can see spot + futures
            parent = backtest._data_parent_root(resolved_root)
            kwargs["data_root"] = parent
            kwargs["symbol"] = backtest.resolve_symbol_folder(parent, raw_sym) or raw_sym
            # Prefer on-disk case from first available target
            targets = backtest.expand_market_targets(
                parent, raw_sym, market_pref, anchor_dir=APP_DIR,
            )
            if targets:
                kwargs["symbol"] = targets[0][2]
            eff_root, eff_sym = parent, kwargs["symbol"]
        else:
            eff_root, eff_sym = backtest.resolve_symbol_data_root(
                resolved_root, raw_sym, anchor_dir=APP_DIR, preferred=market_pref,
            )
            kwargs["data_root"] = eff_root
            kwargs["symbol"] = eff_sym

        cfg = backtest.BacktestConfig(**kwargs)
        sizing_err = backtest.validate_position_sizing_config(cfg)
        if sizing_err:
            raise ValueError(sizing_err)

        # Keep the Run Settings fields in sync with what we will actually use.
        root_w = self.field_widgets.get("data_root")
        if isinstance(root_w, QLineEdit) and root_w.text().strip() != eff_root:
            root_w.setText(eff_root)
        sym_w = self.field_widgets.get("symbol")
        if isinstance(sym_w, QLineEdit) and eff_sym != raw_sym:
            sym_w.setText(eff_sym)

        return cfg

    # ------------------------------------------------------------------
    # RUN BUTTON HANDLER (threaded, same pattern as dashboard.py)
    # ------------------------------------------------------------------
    def _on_run_clicked(self):
        try:
            strategy_config = self._build_strategy_config()
            backtest_config = self._build_backtest_config(strategy_config)
        except Exception as e:
            QMessageBox.critical(self, "Invalid Parameters", f"Could not read parameters:\n{e}")
            return

        if not backtest_config.timeframes_to_test:
            QMessageBox.warning(self, "No Timeframes Selected", "Select at least one timeframe to test.")
            return

        if not backtest_config.sessions_enabled:
            QMessageBox.warning(
                self, "No Sessions Selected",
                "Select at least one trading session (Asian / London / US), "
                "or switch to Indian time mode.",
            )
            return

        if (
            getattr(backtest_config, "ist_time_filter_enabled", False)
            and self._time_filter_mode() == "ist"
        ):
            # basic HH:MM sanity — parse will clamp; empty already defaulted
            pass

        if not os.path.isdir(backtest_config.data_root):
            QMessageBox.critical(
                self, "Data Folder Not Found",
                f"Could not find the data folder:\n{backtest_config.data_root}\n\n"
                f"Expected an absolute path next to the app (e.g. {DEFAULT_DATA_DIR}).\n"
                f"Relative paths like 'data' are resolved against the app folder, "
                f"not whatever folder you launched from."
            )
            return

        market_pref = getattr(backtest_config, "market_data", backtest.MarketDataSource.SPOT)
        targets = backtest.expand_market_targets(
            backtest_config.data_root,
            backtest_config.symbol,
            market_pref,
            anchor_dir=APP_DIR,
        )
        if not targets:
            available = backtest.find_available_symbols(
                backtest_config.data_root, anchor_dir=APP_DIR,
            )
            available_text = ", ".join(available) if available else "(none found)"
            market_label = (
                market_pref.value if hasattr(market_pref, "value") else str(market_pref)
            )
            QMessageBox.critical(
                self, "Symbol Not Found",
                f"No data found for symbol '{backtest_config.symbol}' "
                f"(Market data = {market_label}).\n\n"
                f"Looked under:\n{backtest_config.data_root}\n\n"
                f"Symbols available: {available_text}\n\n"
                "Spot uses data/spot/SYMBOL or legacy data/SYMBOL.\n"
                "Futures uses data/futures/SYMBOL."
            )
            return

        # Fail early if selected timeframes have no CSVs on any chosen market
        missing_csv = []
        for tf_folder in backtest_config.timeframes_to_test:
            found_any = False
            for _label, eff_root, eff_sym in targets:
                files = backtest.find_csv_files(
                    eff_root, eff_sym, tf_folder, preferred=_label,
                )
                if files:
                    found_any = True
                    break
            if not found_any:
                missing_csv.append(tf_folder)
        if missing_csv:
            roots = ", ".join(f"{lbl}:{root}" for lbl, root, _s in targets)
            market_label = (
                market_pref.value if hasattr(market_pref, "value") else str(market_pref)
            )
            QMessageBox.critical(
                self, "CSV data not found",
                "No price CSV files found for:\n  • "
                + "\n  • ".join(missing_csv)
                + f"\n\nMarket data setting: {market_label}\n"
                f"Looking under:\n{roots}\n\n"
                "Use Run Settings → Market data: Spot / Futures / Both."
            )
            return

        self.run_button.setEnabled(False)
        self._set_app_status("Running backtest... this may take a moment.")
        self.progress_bar.setVisible(True)

        self._worker = BacktestWorker(backtest_config)
        self._worker.finished.connect(self._on_run_complete)
        self._worker.failed.connect(self._on_run_failed)

        self._worker_thread = threading.Thread(target=self._worker.run, daemon=True)
        self._worker_thread.start()

    def _on_run_complete(self, tables, output_dir: str, plots_dir: str):
        self.progress_bar.setVisible(False)
        self.run_button.setEnabled(True)
        self.last_output_dir = output_dir
        self.last_plots_dir = plots_dir
        self.last_tables = tables
        self.last_trade_ledger_path = os.path.join(output_dir, "trade_ledger.csv")
        self.last_backtest_config = getattr(self._worker, "backtest_config", None)

        self._display_metrics(tables)
        self._display_charts(plots_dir)

        db_summary, db_detail = self._save_run_to_database(tables, output_dir, plots_dir)
        self._set_app_status(
            f"Done. {db_summary}",
            tooltip=f"Output: {output_dir}\n{db_detail}",
        )
        if hasattr(self, "results_tabs"):
            # Jump to Equity & Price so the client sees curves immediately
            for i in range(self.results_tabs.count()):
                if self.results_tabs.tabText(i) == "Equity & Price":
                    self.results_tabs.setCurrentIndex(i)
                    break
            else:
                self.results_tabs.setCurrentIndex(0)
        if hasattr(self, "results_dock"):
            self.results_dock.show()
            self.results_dock.raise_()

    def _save_run_to_database(self, tables, output_dir: str, plots_dir: str):
        """Returns (short_summary_for_status_bar, full_detail_for_tooltip)."""
        if self.run_db is None:
            return "History DB unavailable.", "Run history database could not be opened -- this run was not saved."
        backtest_config = getattr(self._worker, "backtest_config", None)
        if backtest_config is None:
            return "Not saved to history.", "Could not identify this run's parameters -- not saved to history."

        try:
            strategy_id, was_duplicate, existing = self.run_db.save_run(backtest_config, tables, output_dir, plots_dir)
        except Exception as e:
            return "History save failed.", f"Could not save to run history: {e}"

        self._refresh_run_history_table()

        if was_duplicate:
            detail = (f"Identical parameters already saved as StrategyID #{existing['run_id']} "
                      f"({existing['run_timestamp']}) -- not duplicated.")
            return f"Matches StrategyID #{existing['run_id']} -- not duplicated.", detail
        return f"Saved as StrategyID #{strategy_id}.", f"Saved to run history as StrategyID #{strategy_id}."

    def _refresh_run_history_table(self):
        if self.run_db is None or not hasattr(self, "history_table"):
            return
        try:
            rows = self.run_db.fetch_run_history()
        except Exception as e:
            print(f"Could not load run history: {e}")
            return

        headers = [
            "Strategy ID", "Created At", "Pattern", "Symbol", "Market", "Timeframes", "Body %",
            "Net PnL (WC)", "Win Rate (WC)", "PF (WC)", "Max DD % (WC)", "Best Session (WC)",
            "Dominant Wick %", "Position Sizing", "Initial Deposit", "Output Folder",
        ]
        self.history_table.setColumnCount(len(headers))
        self.history_table.setHorizontalHeaderLabels(headers)
        self.history_table.setRowCount(len(rows))

        for i, r in enumerate(rows):
            pf = r.get("ProfitFactorWC")
            net = r.get("NetProfitWC")
            wr = r.get("WinRateWC")
            values = [
                r.get("StrategyID"), r.get("CreatedAt"), r.get("PatternType"), r.get("Symbol"),
                r.get("MarketData") or "spot",
                r.get("TimeframesTested"), r.get("BodyPct"),
                f"{net:,.2f}" if isinstance(net, (int, float)) else net,
                f"{wr:.1f}%" if isinstance(wr, (int, float)) else wr,
                f"{pf:.2f}" if isinstance(pf, (int, float)) else pf,
                f"{r.get('MaxDrawdownWC'):.2f}%" if isinstance(r.get("MaxDrawdownWC"), (int, float)) else r.get("MaxDrawdownWC"),
                r.get("BestSessionWC"),
                r.get("DominantWickPct"), r.get("PositionSizingMethod"), r.get("InitialDeposit"),
                r.get("OutputDir"),
            ]
            for j, val in enumerate(values):
                item = QTableWidgetItem("" if val is None else str(val))
                if j == 0:
                    item.setData(Qt.UserRole, r.get("OutputDir"))
                self.history_table.setItem(i, j, item)

        self.history_table.resizeColumnsToContents()
        self._refresh_compare_combos()

    def _refresh_compare_combos(self):
        if not hasattr(self, "compare_combo_a"):
            return
        for combo in (self.compare_combo_a, self.compare_combo_b):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— select —", None)
            if self.run_db is not None:
                try:
                    for r in self.run_db.fetch_run_history():
                        sid = r.get("StrategyID")
                        label = (
                            f"#{sid}  {r.get('Symbol') or ''}  "
                            f"{(r.get('CreatedAt') or '')[:16]}"
                        )
                        combo.addItem(label.strip(), sid)
                except Exception as e:
                    print(f"Compare combo refresh failed: {e}")
            combo.blockSignals(False)

    def _open_history_run_folder(self, item: QTableWidgetItem):
        row = item.row()
        first_col_item = self.history_table.item(row, 0)
        output_dir = first_col_item.data(Qt.UserRole) if first_col_item else None
        self._open_folder(output_dir)

    def _export_run_history_to_excel(self):
        if self.run_db is None:
            QMessageBox.information(self, "Not Available", "The run history database isn't available.")
            return

        try:
            os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
            filename = f"run_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            path = os.path.join(DEFAULT_OUTPUT_DIR, filename)
            self.run_db.export_to_excel(path)
        except ImportError:
            QMessageBox.critical(
                self, "Missing Package",
                "Exporting to Excel needs the 'openpyxl' package.\n\nInstall it with:\npip install openpyxl"
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export run history:\n{e}")
            return

        self._set_app_status(f"Run history exported: {path}")
        QMessageBox.information(self, "Export Complete", f"Run history saved to:\n{path}")

    def _on_run_failed(self, error_msg: str, traceback_str: str):
        self.progress_bar.setVisible(False)
        self.run_button.setEnabled(True)
        self._set_app_status("Backtest failed. See error details.")
        print(traceback_str)
        QMessageBox.critical(self, "Backtest Failed",
                              f"{error_msg}\n\n(Full details printed to console.)")

    # ------------------------------------------------------------------
    # DISPLAY RESULTS
    # ------------------------------------------------------------------
    def _fill_table(self, table: QTableWidget, df, color_rows: bool = True) -> None:
        if df is None or df.height == 0:
            table.setRowCount(1)
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels([""])
            table.setItem(0, 0, QTableWidgetItem("No data for this table."))
            return

        columns = df.columns
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels([c.replace("_", " ").title() for c in columns])
        table.setRowCount(df.height)

        for i, row in enumerate(df.iter_rows(named=True)):
            row_bg = result_tint_for_row(row) if color_rows else None
            for j, col in enumerate(columns):
                val = row.get(col)
                if isinstance(val, float):
                    text = f"{val:,.3f}" if abs(val) < 1000 else f"{val:,.2f}"
                elif val is None:
                    text = ""
                else:
                    text = str(val)
                table.setItem(i, j, _table_item(text, row_bg))

        table.resizeColumnsToContents()

    def _fill_trade_ledger_table(self, df) -> None:
        self._trade_ledger_full_df = df
        self._refresh_trade_ledger_view()

    @staticmethod
    def _trade_ledger_time_col(df) -> Optional[str]:
        for col in ("exit_time", "entry_time", "exit_time_ist", "entry_time_ist"):
            if col in df.columns:
                return col
        return None

    def _apply_trade_ledger_sort(self, view, sort_key: str):
        """Sort trades for inspection; applied before the display row cap."""
        if view is None or getattr(view, "height", 0) == 0:
            return view
        time_col = self._trade_ledger_time_col(view)
        has_outcome = "outcome" in view.columns
        has_pnl = "pnl_usd" in view.columns

        if sort_key == "oldest" and time_col:
            return view.sort(time_col, descending=False, nulls_last=True)
        if sort_key == "newest" and time_col:
            return view.sort(time_col, descending=True, nulls_last=True)
        if sort_key == "losses_first" and has_outcome:
            ranked = view.with_columns(
                pl.when(pl.col("outcome") == "LOSS").then(0)
                .when(pl.col("outcome") == "WIN").then(1)
                .otherwise(2)
                .alias("_sort_rank")
            )
            sort_cols = ["_sort_rank"] + ([time_col] if time_col else [])
            descending = [False] + ([True] if time_col else [])
            ranked = ranked.sort(sort_cols, descending=descending, nulls_last=True)
            return ranked.drop("_sort_rank")
        if sort_key == "wins_first" and has_outcome:
            ranked = view.with_columns(
                pl.when(pl.col("outcome") == "WIN").then(0)
                .when(pl.col("outcome") == "LOSS").then(1)
                .otherwise(2)
                .alias("_sort_rank")
            )
            sort_cols = ["_sort_rank"] + ([time_col] if time_col else [])
            descending = [False] + ([True] if time_col else [])
            ranked = ranked.sort(sort_cols, descending=descending, nulls_last=True)
            return ranked.drop("_sort_rank")
        if sort_key == "biggest_loss" and has_pnl:
            return view.sort("pnl_usd", descending=False, nulls_last=True)
        if sort_key == "biggest_win" and has_pnl:
            return view.sort("pnl_usd", descending=True, nulls_last=True)
        if time_col:
            return view.sort(time_col, descending=True, nulls_last=True)
        return view

    def _refresh_trade_ledger_view(self) -> None:
        table = self.trade_ledger_table
        df = self._trade_ledger_full_df
        if df is None or getattr(df, "height", 0) == 0:
            self._trade_ledger_display_df = None
            table.setRowCount(1)
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels([""])
            table.setItem(0, 0, QTableWidgetItem("No trades in this run."))
            if hasattr(self, "trade_ledger_note"):
                self.trade_ledger_note.setText("No trades were generated.")
            return

        model_filter = ""
        if hasattr(self, "trade_exit_model_filter"):
            model_filter = self.trade_exit_model_filter.currentData() or ""
        outcome_filter = "all"
        if hasattr(self, "trade_outcome_filter"):
            outcome_filter = self.trade_outcome_filter.currentData() or "all"
        sort_key = "newest"
        if hasattr(self, "trade_sort_filter"):
            sort_key = self.trade_sort_filter.currentData() or "newest"

        view = df
        if model_filter and "exit_model" in df.columns:
            view = view.filter(pl.col("exit_model") == model_filter)
        if outcome_filter in ("WIN", "LOSS") and "outcome" in view.columns:
            view = view.filter(pl.col("outcome") == outcome_filter)

        view = self._apply_trade_ledger_sort(view, sort_key)

        total = view.height
        shown = total
        if total > TRADE_LEDGER_DISPLAY_MAX_ROWS:
            view = view.head(TRADE_LEDGER_DISPLAY_MAX_ROWS)
            shown = TRADE_LEDGER_DISPLAY_MAX_ROWS

        filter_bits = []
        if model_filter:
            filter_bits.append(model_filter)
        else:
            filter_bits.append("all exit models")
        if outcome_filter in ("WIN", "LOSS"):
            filter_bits.append(f"{outcome_filter.lower()}s only")
        sort_label = {
            "newest": "newest first",
            "oldest": "oldest first",
            "losses_first": "losses first",
            "wins_first": "wins first",
            "biggest_loss": "biggest loss first",
            "biggest_win": "biggest win first",
        }.get(sort_key, sort_key)
        filter_bits.append(sort_label)

        if shown < total:
            note = (
                f"Showing {shown:,} of {total:,} rows ({', '.join(filter_bits)}). "
                f"Double-click a row to inspect candlesticks."
            )
        else:
            note = (
                f"{total:,} trade rows ({', '.join(filter_bits)}). "
                f"Double-click a row to inspect candlesticks."
            )
        if hasattr(self, "trade_ledger_note"):
            cfg = getattr(self, "last_backtest_config", None)
            if cfg is not None and getattr(cfg, "ist_time_filter_enabled", False):
                clock_note = (
                    f" Filtered by Indian time "
                    f"{getattr(cfg, 'ist_time_start', '')}–{getattr(cfg, 'ist_time_end', '')} IST."
                )
            else:
                clock_note = " Entry Time Ist / Exit Time Ist are Indian clock."
            self.trade_ledger_note.setText(note + clock_note)

        self._trade_ledger_display_df = view
        prefer = [
            "market", "entry_time_ist", "entry_time", "timeframe", "direction", "session", "outcome", "exit_model",
            "entry_price", "stop_loss", "target", "exit_price", "exit_time_ist", "exit_time",
            "bars_held", "pnl_usd", "pattern_variant", "risk_usd", "session_clock",
        ]
        cols = [c for c in prefer if c in view.columns]
        cols += [c for c in view.columns if c not in cols]
        view = view.select(cols)

        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels([c.replace("_", " ").title() for c in cols])
        table.setRowCount(view.height)
        for i, row in enumerate(view.iter_rows(named=True)):
            row_bg = result_tint_for_row(row)
            row_fg = result_fg_for_outcome(row)
            for j, col in enumerate(cols):
                val = row.get(col)
                if isinstance(val, float):
                    text = f"{val:,.3f}" if abs(val) < 1000 else f"{val:,.2f}"
                elif val is None:
                    text = ""
                else:
                    text = str(val)
                item = _table_item(text, row_bg, row_fg)
                if j == 0:
                    item.setData(Qt.UserRole, dict(row))
                table.setItem(i, j, item)
        table.resizeColumnsToContents()

    def _inspect_selected_trade(self) -> None:
        table = getattr(self, "trade_ledger_table", None)
        if table is None:
            return
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Select a trade",
                "Click a trade row first, then Inspect — or double-click the row.",
            )
            return
        item = table.item(row, 0)
        trade = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(trade, dict):
            QMessageBox.information(self, "No trade data", "That row has no trade details.")
            return
        cfg = self.last_backtest_config
        if cfg is None:
            QMessageBox.warning(
                self, "No run context",
                "Run a backtest in this session first so the inspector knows the data folder.",
            )
            return
        try:
            # Prefer indicators from the run; if none were saved/enabled, use
            # whatever is currently added on the Indicators tab so overlays still show.
            stack = getattr(cfg, "indicator_stack", None)
            if stack is None or not stack.enabled_indicator_ids():
                stack = self._build_indicator_stack()
            trades_list: List[dict] = []
            for r in range(table.rowCount()):
                cell = table.item(r, 0)
                if cell is None:
                    continue
                payload = cell.data(Qt.UserRole)
                if isinstance(payload, dict):
                    trades_list.append(payload)
            start_idx = 0
            if trades_list:
                for i, t in enumerate(trades_list):
                    if t is trade or t == trade:
                        start_idx = i
                        break
                else:
                    # Fallback: match by current table row among dict rows
                    start_idx = min(max(row, 0), len(trades_list) - 1)
            else:
                trades_list = [trade]
            dialog = TradeInspectDialog(
                self, trade, cfg,
                indicator_stack=stack,
                trades=trades_list,
                trade_index=start_idx,
            )
            dialog.exec()
            # Keep table selection in sync with last viewed trade
            last_idx = getattr(dialog, "_trade_index", start_idx)
            if 0 <= last_idx < table.rowCount():
                table.selectRow(last_idx)
                table.setCurrentCell(last_idx, 0)
        except Exception as e:
            QMessageBox.critical(self, "Inspect failed", str(e))
            traceback.print_exc()

    def _fill_overall_vertical(self, df) -> None:
        """Same transpose logic as dashboard.py: metric=row, exit_model=column."""
        table = self.overall_table
        if df is None or df.height == 0:
            table.setRowCount(1)
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels([""])
            table.setItem(0, 0, QTableWidgetItem("No data for this table."))
            return

        exit_models = df["exit_model"].to_list()
        metric_cols = [c for c in df.columns if c not in ("group", "exit_model")]

        table.setColumnCount(1 + len(exit_models))
        table.setHorizontalHeaderLabels(["Metric"] + [m.replace("_", " ").title() for m in exit_models])
        metric_header = QTableWidgetItem("Metric")
        metric_header.setFlags(metric_header.flags() & ~Qt.ItemIsEditable)
        table.setHorizontalHeaderItem(0, metric_header)
        for j, model in enumerate(exit_models):
            col_bg = result_tint_for_exit_model_column(model)
            hdr = QTableWidgetItem(model.replace("_", " ").title())
            hdr.setFlags(hdr.flags() & ~Qt.ItemIsEditable)
            if col_bg is not None:
                hdr.setBackground(col_bg)
            table.setHorizontalHeaderItem(j + 1, hdr)

        table.setRowCount(len(metric_cols))

        rows_by_model = {row["exit_model"]: row for row in df.iter_rows(named=True)}

        for i, metric in enumerate(metric_cols):
            table.setItem(i, 0, _table_item(metric.replace("_", " ").title()))
            for j, model in enumerate(exit_models):
                val = rows_by_model[model].get(metric)
                if isinstance(val, float):
                    text = f"{val:,.3f}" if abs(val) < 1000 else f"{val:,.2f}"
                elif val is None:
                    text = ""
                else:
                    text = str(val)
                col_bg = result_tint_for_exit_model_column(model)
                table.setItem(i, j + 1, _table_item(text, col_bg))

        table.resizeColumnsToContents()

    def _display_metrics(self, tables):
        self._fill_overall_vertical(tables.get("overall"))
        self._fill_table(self.by_timeframe_table, tables.get("by_timeframe"))
        self._fill_table(self.by_direction_table, tables.get("by_direction"))
        self._fill_table(self.by_session_table, tables.get("by_session"))
        self._fill_table(self.by_market_table, tables.get("by_market"))
        self._fill_table(self.by_year_table, tables.get("by_year"))
        self._fill_table(self.by_month_table, tables.get("by_month"))
        self._fill_trade_ledger_table(tables.get("ledger"))
        self._refresh_session_results_legend()
        self._refresh_buy_sell_summary(tables.get("by_direction"))

    def _direction_side_key(self, row: Dict[str, Any]) -> str:
        side = _normalize_result_label(row.get("direction") or row.get("group"))
        return side if side in ("BUY", "SELL") else ""

    def _refresh_buy_sell_summary(self, by_direction_df) -> None:
        """Plain-English verdict: is BUY or SELL driving the wins?"""
        if not hasattr(self, "buy_sell_summary"):
            return
        if by_direction_df is None or getattr(by_direction_df, "height", 0) == 0:
            self.buy_sell_summary.setText(
                "<b>BUY vs SELL:</b> no direction breakdown for this run."
            )
            return

        # Prefer worst_case (live-relevant); fall back to whatever is present
        models = []
        if "exit_model" in by_direction_df.columns:
            models = by_direction_df["exit_model"].unique().to_list()
        prefer = ["worst_case", "candle_bias", "best_case"]
        model = next((m for m in prefer if m in models), models[0] if models else None)

        rows = [
            r for r in by_direction_df.iter_rows(named=True)
            if (model is None or r.get("exit_model") == model)
            and self._direction_side_key(r) in ("BUY", "SELL")
        ]
        by_side = {self._direction_side_key(r): r for r in rows}
        buy = by_side.get("BUY")
        sell = by_side.get("SELL")

        def _f(row, key, default=0.0):
            if not row:
                return default
            v = row.get(key)
            return float(v) if v is not None else default

        def _i(row, key, default=0):
            if not row:
                return default
            v = row.get(key)
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        buy_pnl = _f(buy, "net_pnl")
        sell_pnl = _f(sell, "net_pnl")
        buy_wr = _f(buy, "win_rate_pct")
        sell_wr = _f(sell, "win_rate_pct")
        buy_n = _i(buy, "total_trades")
        sell_n = _i(sell, "total_trades")
        buy_wins = _i(buy, "wins")
        sell_wins = _i(sell, "wins")
        buy_pf = _f(buy, "profit_factor")
        sell_pf = _f(sell, "profit_factor")

        # Who is driving performance?
        if buy is None and sell is None:
            self.buy_sell_summary.setText("<b>BUY vs SELL:</b> no BUY/SELL rows found.")
            return
        if buy is None:
            verdict = "Only <b>SELL</b> trades in this run."
            winner = "SELL"
        elif sell is None:
            verdict = "Only <b>BUY</b> trades in this run."
            winner = "BUY"
        elif abs(buy_pnl - sell_pnl) < 1e-9:
            verdict = "BUY and SELL contributed about the <b>same Net PnL</b>."
            winner = "TIE"
        elif buy_pnl > sell_pnl:
            gap = buy_pnl - sell_pnl
            verdict = (
                f"<b>BUY is driving the results</b> — higher Net PnL by "
                f"<b>${gap:,.2f}</b> vs SELL."
            )
            winner = "BUY"
        else:
            gap = sell_pnl - buy_pnl
            verdict = (
                f"<b>SELL is driving the results</b> — higher Net PnL by "
                f"<b>${gap:,.2f}</b> vs BUY."
            )
            winner = "SELL"

        wr_note = ""
        if buy is not None and sell is not None and abs(buy_wr - sell_wr) >= 0.5:
            better_wr = "BUY" if buy_wr > sell_wr else "SELL"
            wr_note = (
                f" Win rate edge: <b>{better_wr}</b> "
                f"({max(buy_wr, sell_wr):.1f}% vs {min(buy_wr, sell_wr):.1f}%)."
            )

        model_label = (model or "all").replace("_", " ")
        html = (
            f"<b>BUY vs SELL summary</b> "
            f"<span style='color:#5f6368;'>({model_label})</span><br>"
            f"{verdict}{wr_note}<br><br>"
            f"<span style='background-color:#D7F5DD; padding:4px 10px; border-radius:4px;'>"
            f"<b>BUY</b> — {buy_n} trades · {buy_wins} wins · WR {buy_wr:.1f}% · "
            f"Net ${buy_pnl:,.2f} · PF {buy_pf:.2f}</span>"
            f"&nbsp;&nbsp;"
            f"<span style='background-color:#FADBD8; padding:4px 10px; border-radius:4px;'>"
            f"<b>SELL</b> — {sell_n} trades · {sell_wins} wins · WR {sell_wr:.1f}% · "
            f"Net ${sell_pnl:,.2f} · PF {sell_pf:.2f}</span>"
        )
        self.buy_sell_summary.setText(html)

        if hasattr(self, "buy_sell_direction_note"):
            if winner == "BUY":
                tip = "Quick read: <b>BUY</b> side is the stronger contributor on Net PnL."
            elif winner == "SELL":
                tip = "Quick read: <b>SELL</b> side is the stronger contributor on Net PnL."
            elif winner == "TIE":
                tip = "Quick read: BUY and SELL are roughly even on Net PnL."
            else:
                tip = "Full BUY / SELL breakdown by exit model below."
            self.buy_sell_direction_note.setText(
                tip + " Open this tab for every metric row."
            )

    def _refresh_session_results_legend(self) -> None:
        if not hasattr(self, "results_color_legend"):
            return
        cfg = getattr(self, "last_backtest_config", None)
        ist_on = bool(getattr(cfg, "ist_time_filter_enabled", False)) if cfg else False
        if ist_on and cfg is not None:
            filter_txt = (
                f"<b>Filter:</b> Indian time "
                f"{getattr(cfg, 'ist_time_start', '?')}–{getattr(cfg, 'ist_time_end', '?')} IST. "
                "By Session below is still Asian/London/US on broker clock for breakdown."
            )
        else:
            enabled = getattr(cfg, "sessions_enabled", None) if cfg else None
            if enabled and set(enabled) != set(sessions.SESSION_ORDER):
                filter_txt = f"<b>Filter:</b> sessions {', '.join(enabled)} (broker clock)."
            else:
                filter_txt = "<b>Filter:</b> all sessions (broker clock)."
        self.results_color_legend.setText(
            'Color key: '
            '<span style="background-color:#D7F5DD; padding:2px 10px; border-radius:4px;">BUY</span> '
            '<span style="background-color:#FADBD8; padding:2px 10px; border-radius:4px;">SELL</span> '
            '<span style="background-color:#D2E3FC; padding:2px 10px; border-radius:4px;">Candle bias</span>'
            '<br>Trade count: <b>Total Trades</b> is WIN + LOSS only. '
            'Overlap skips are <b>Skipped Overlap</b> (not in Total Trades). '
            'Taken setups = Total Trades + Skipped Overlap + Still Open. '
            'By Timeframe lists each TF three times (best / candle-bias / worst) — do not add those rows. '
            '30m often has more skips than 1h because signals are denser while a trade is still open.'
            f'<br>{filter_txt} '
            'Trades tab shows <b>Entry Time Ist</b> / <b>Exit Time Ist</b>. '
            'Session <b>Net PnL / wins / losses</b> sum to Overall; session Max DD and Return % are '
            '<b>as if that session alone</b> (not additive across sessions).'
        )

    def _clear_vbox(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _add_chart_card(self, layout: QVBoxLayout, path: str, title: str, width: int = 720) -> None:
        card = QFrame()
        card.setObjectName("candleCard")
        card_layout = QVBoxLayout(card)
        name_label = QLabel(f"{title}  (click image to zoom)")
        name_label.setStyleSheet("font-weight: 600;")
        card_layout.addWidget(name_label)
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            scaled = pixmap.scaledToWidth(max(280, int(width)), Qt.SmoothTransformation)
            img_label = ClickableImageLabel(path, title, self)
            img_label.setPixmap(scaled)
            img_label.setCursor(Qt.PointingHandCursor)
            img_label.setToolTip("Click to open full-size zoom window")
            img_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            card_layout.addWidget(img_label)
        else:
            card_layout.addWidget(QLabel(f"(could not load {os.path.basename(path)})"))
        layout.addWidget(card)

    def _display_charts(self, plots_dir: str):
        self._clear_vbox(self.charts_layout)
        if hasattr(self, "equity_chart_layout"):
            self._clear_vbox(self.equity_chart_layout)
        if hasattr(self, "price_chart_layout"):
            self._clear_vbox(self.price_chart_layout)

        if not os.path.isdir(plots_dir):
            msg = QLabel("No charts were generated.")
            self.charts_layout.addWidget(msg)
            if hasattr(self, "equity_chart_layout"):
                self.equity_chart_layout.addWidget(QLabel("No equity chart yet — run a backtest."))
                self.equity_chart_layout.addStretch()
            if hasattr(self, "price_chart_layout"):
                self.price_chart_layout.addWidget(QLabel("No price chart yet — run a backtest."))
                self.price_chart_layout.addStretch()
            return

        png_files = sorted(f for f in os.listdir(plots_dir) if f.endswith(".png"))
        if not png_files:
            self.charts_layout.addWidget(QLabel("No charts were generated."))
            if hasattr(self, "equity_chart_layout"):
                self.equity_chart_layout.addWidget(
                    QLabel("Equity charts missing — re-run the backtest.")
                )
                self.equity_chart_layout.addStretch()
            if hasattr(self, "price_chart_layout"):
                self.price_chart_layout.addWidget(
                    QLabel("Price chart missing — re-run the backtest.")
                )
                self.price_chart_layout.addStretch()
            return

        # Side-by-side: main equity curve + clean price line
        equity_names = (
            "equity_curves_all_models.png",
            "equity_with_regression.png",
        )
        price_names = ("price_line_with_trades.png",)
        # Fit both columns in the Results pane
        col_width = 560

        for fname in equity_names:
            path = os.path.join(plots_dir, fname)
            if os.path.isfile(path) and hasattr(self, "equity_chart_layout"):
                self._add_chart_card(
                    self.equity_chart_layout, path,
                    fname.replace("_", " ").replace(".png", ""),
                    width=col_width,
                )
        if hasattr(self, "equity_chart_layout"):
            if self.equity_chart_layout.count() == 0:
                self.equity_chart_layout.addWidget(
                    QLabel("Equity charts missing — re-run the backtest.")
                )
            self.equity_chart_layout.addStretch()

        for fname in price_names:
            path = os.path.join(plots_dir, fname)
            if os.path.isfile(path) and hasattr(self, "price_chart_layout"):
                self._add_chart_card(
                    self.price_chart_layout, path,
                    "Price line", width=col_width,
                )
        if hasattr(self, "price_chart_layout"):
            if self.price_chart_layout.count() == 0:
                self.price_chart_layout.addWidget(
                    QLabel("Price chart missing — re-run the backtest.")
                )
            self.price_chart_layout.addStretch()

        # All charts gallery (equity + price first)
        priority = list(equity_names) + list(price_names) + ["drawdown.png"]
        ordered = [f for f in priority if f in png_files] + [
            f for f in png_files if f not in priority
        ]
        for fname in ordered:
            path = os.path.join(plots_dir, fname)
            self._add_chart_card(self.charts_layout, path, fname)

        self.charts_layout.addStretch()

    def open_zoomed_chart(self, path: str, title: str):
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, "Chart missing", f"Could not find:\n{path}")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.information(self, "Chart missing", f"Could not load image:\n{path}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(title or "Chart zoom")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        hint = QLabel("Scroll to pan. Close when done.")
        hint.setObjectName("sectionHint")
        layout.addWidget(hint)

        scroll = QScrollArea()
        # False = keep full image size so scrollbars actually pan/zoom-pan
        scroll.setWidgetResizable(False)
        label = QLabel()
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 1.6)
        max_h = int(screen.height() * 1.6)
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumSize(pixmap.size())
        scroll.setWidget(label)
        layout.addWidget(scroll, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)

        dialog.resize(
            min(max(pixmap.width() + 80, 720), int(screen.width() * 0.92)),
            min(max(pixmap.height() + 120, 480), int(screen.height() * 0.92)),
        )
        dialog.exec()

    # ------------------------------------------------------------------
    # FOLDER SHORTCUTS + PRESETS + RUN EXPORT / COMPARE
    # ------------------------------------------------------------------
    def _open_output_folder(self):
        self._open_folder(self.last_output_dir)

    def _open_plots_folder(self):
        self._open_folder(self.last_plots_dir)

    def _open_trade_ledger_csv(self):
        path = self.last_trade_ledger_path
        if not path or not os.path.isfile(path):
            QMessageBox.information(
                self, "No Ledger Yet",
                "Run a backtest first, or open the output folder and find trade_ledger.csv.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _export_last_run_metrics_excel(self):
        if not self.last_tables:
            QMessageBox.information(self, "No Run Yet", "Run a backtest first.")
            return
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            QMessageBox.critical(
                self, "Missing Package",
                "Install openpyxl: pip install openpyxl",
            )
            return

        os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DEFAULT_OUTPUT_DIR, f"last_run_metrics_{stamp}.xlsx")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="34A853")

        def write_polars_sheet(title: str, df):
            sheet = wb.create_sheet(title[:31])
            if df is None or getattr(df, "height", 0) == 0:
                sheet["A1"] = "(no data)"
                return
            headers = list(df.columns)
            sheet.append(headers)
            for cell in sheet[1]:
                cell.font = header_font
                cell.fill = header_fill
            for row in df.iter_rows():
                sheet.append(list(row))

        write_polars_sheet("overall", self.last_tables.get("overall"))
        write_polars_sheet("by_timeframe", self.last_tables.get("by_timeframe"))
        write_polars_sheet("by_direction", self.last_tables.get("by_direction"))
        write_polars_sheet("by_session", self.last_tables.get("by_session"))
        write_polars_sheet("by_year", self.last_tables.get("by_year"))
        write_polars_sheet("by_month", self.last_tables.get("by_month"))
        ledger = self.last_tables.get("ledger")
        if ledger is not None and ledger.height > 0:
            cap = min(ledger.height, TRADE_LEDGER_DISPLAY_MAX_ROWS)
            write_polars_sheet("trades_sample", ledger.head(cap))

        if not wb.sheetnames:
            wb.create_sheet("empty")

        try:
            wb.save(path)
        except OSError as e:
            QMessageBox.critical(self, "Export Failed", str(e))
            return
        self._set_app_status(f"Metrics exported: {path}")
        QMessageBox.information(self, "Export Complete", f"Saved to:\n{path}")

    def _run_history_compare(self):
        if self.run_db is None:
            QMessageBox.information(self, "Not Available", "Run history database is not open.")
            return
        id_a = self.compare_combo_a.currentData()
        id_b = self.compare_combo_b.currentData()
        if not id_a or not id_b:
            QMessageBox.warning(self, "Select Runs", "Choose Run A and Run B from the dropdowns.")
            return
        if id_a == id_b:
            QMessageBox.warning(self, "Same Run", "Pick two different strategy IDs to compare.")
            return
        try:
            rows_a = {r["ExitModel"]: r for r in self.run_db.fetch_backtest_results(int(id_a))}
            rows_b = {r["ExitModel"]: r for r in self.run_db.fetch_backtest_results(int(id_b))}
        except Exception as e:
            QMessageBox.critical(self, "Compare Failed", str(e))
            return
        if not rows_a and not rows_b:
            QMessageBox.information(self, "No Data", "No stored results for those strategy IDs.")
            return

        exit_models = sorted(set(rows_a.keys()) | set(rows_b.keys()))
        headers = [
            "Exit Model",
            f"Net PnL (#{id_a})", f"Net PnL (#{id_b})",
            f"Win % (#{id_a})", f"Win % (#{id_b})",
            f"Max DD % (#{id_a})", f"Max DD % (#{id_b})",
            f"Trades (#{id_a})", f"Trades (#{id_b})",
            f"PF (#{id_a})", f"PF (#{id_b})",
        ]
        table = self.compare_results_table
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(exit_models))

        def _fmt_money(v):
            if v is None:
                return "—"
            return f"{float(v):,.2f}"

        def _fmt_pct(v):
            if v is None:
                return "—"
            return f"{float(v):.1f}"

        def _fmt_int(v):
            if v is None:
                return "—"
            return str(int(float(v)))

        def _fmt_pf(v):
            if v is None:
                return "—"
            return f"{float(v):.2f}"

        for i, em in enumerate(exit_models):
            ra, rb = rows_a.get(em, {}), rows_b.get(em, {})
            row_bg = result_tint_for_row({"exit_model": em})
            values = [
                em,
                _fmt_money(ra.get("NetProfit")), _fmt_money(rb.get("NetProfit")),
                _fmt_pct(ra.get("WinRatePercent")), _fmt_pct(rb.get("WinRatePercent")),
                _fmt_pct(ra.get("MaxDrawdownPercent")), _fmt_pct(rb.get("MaxDrawdownPercent")),
                _fmt_int(ra.get("TotalTrades")), _fmt_int(rb.get("TotalTrades")),
                _fmt_pf(ra.get("ProfitFactor")), _fmt_pf(rb.get("ProfitFactor")),
            ]
            for j, text in enumerate(values):
                table.setItem(i, j, _table_item(text, row_bg))
        table.resizeColumnsToContents()

        try:
            sess_a = {
                r["GroupName"]: r
                for r in self.run_db.fetch_metrics_breakdown(int(id_a), "session", "worst_case")
            }
            sess_b = {
                r["GroupName"]: r
                for r in self.run_db.fetch_metrics_breakdown(int(id_b), "session", "worst_case")
            }
        except Exception:
            sess_a, sess_b = {}, {}

        sess_headers = [
            "Session",
            f"Net PnL (#{id_a})", f"Net PnL (#{id_b})",
            f"Win % (#{id_a})", f"Win % (#{id_b})",
            f"PF (#{id_a})", f"PF (#{id_b})",
            f"Trades (#{id_a})", f"Trades (#{id_b})",
        ]
        stable = self.compare_session_table
        stables = sessions.SESSION_ORDER
        stables_rows = [s for s in stables if s in sess_a or s in sess_b]
        if not stables_rows:
            stables_rows = stables
        stable.setColumnCount(len(sess_headers))
        stable.setHorizontalHeaderLabels(sess_headers)
        stable.setRowCount(len(stables_rows))
        for i, sess in enumerate(stables_rows):
            ra, rb = sess_a.get(sess, {}), sess_b.get(sess, {})
            svalues = [
                sess,
                _fmt_money(ra.get("NetProfit")), _fmt_money(rb.get("NetProfit")),
                _fmt_pct(ra.get("WinRatePercent")), _fmt_pct(rb.get("WinRatePercent")),
                _fmt_pf(ra.get("ProfitFactor")), _fmt_pf(rb.get("ProfitFactor")),
                _fmt_int(ra.get("TotalTrades")), _fmt_int(rb.get("TotalTrades")),
            ]
            for j, text in enumerate(svalues):
                stable.setItem(i, j, _table_item(text))
        stable.resizeColumnsToContents()

        if hasattr(self, "results_tabs"):
            for idx in range(self.results_tabs.count()):
                if self.results_tabs.tabText(idx) == "Compare":
                    self.results_tabs.setCurrentIndex(idx)
                    break

    def _preset_path(self, filename: str) -> str:
        return os.path.join(PRESETS_DIR, filename)

    def _ensure_example_presets(self) -> None:
        """Copy template example presets into presets/ if missing (client-ready)."""
        os.makedirs(PRESETS_DIR, exist_ok=True)
        template_dir = os.path.join(APP_DIR, "template")
        examples = (
            "preset_example_hammer_green_buy_red_sell.json",
            "preset_example_hammer_with_candles.json",
        )
        for name in examples:
            src = os.path.join(template_dir, name)
            dst = os.path.join(PRESETS_DIR, name)
            if os.path.isfile(src) and not os.path.isfile(dst):
                try:
                    import shutil
                    shutil.copy2(src, dst)
                except OSError:
                    pass

    def _refresh_preset_combo(self, keep_selection: bool = False):
        if not hasattr(self, "preset_combo"):
            return
        self._ensure_example_presets()
        os.makedirs(PRESETS_DIR, exist_ok=True)
        current = self.preset_combo.currentData() if keep_selection else ""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Not using a saved preset", "")
        for name in sorted(f for f in os.listdir(PRESETS_DIR) if f.endswith(".json")):
            label = name[:-5].replace("_", " ")
            # Prefer display_name from JSON when present
            path = self._preset_path(name)
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)
                if isinstance(meta, dict) and meta.get("display_name"):
                    label = str(meta["display_name"])
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            self.preset_combo.addItem(label, name)
        if keep_selection and current:
            idx = self.preset_combo.findData(current)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
            else:
                self.preset_combo.setCurrentIndex(0)
        else:
            self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)

    def _collect_preset_trade_rules(self, pattern: str) -> Dict[str, Any]:
        """Human-readable rules stored in every preset JSON for client review."""
        if pattern == "Doji":
            cfg = self._build_doji_strategy_config()
            return {
                "pattern": "Doji",
                "direction_mode": getattr(cfg.doji_direction_mode, "value", str(cfg.doji_direction_mode)),
                "doji_style": getattr(cfg.doji_style, "value", str(cfg.doji_style)),
                "green_doji": getattr(cfg.green_direction, "value", "BUY"),
                "red_doji": getattr(cfg.red_direction, "value", "SELL"),
                "note": "Doji uses doji_direction_mode — not hammer green/red tabs unless mode is CANDLE_COLOR.",
            }
        if pattern == "Hammer with candles":
            cfg = self._build_hammer_context_strategy_config()
            buy = cfg.buy_hammer_ratios
            sell = cfg.sell_hammer_ratios
            stack = self._build_indicator_stack()
            inds = stack.enabled_indicator_ids()
            return {
                "pattern": "Hammer with candles",
                "lookback_candles": cfg.lookback_candles,
                "enable_buy": cfg.enable_buy,
                "enable_sell": cfg.enable_sell,
                "buy_require_wick": cfg.buy_require_wick,
                "sell_require_wick": cfg.sell_require_wick,
                "buy_body_pct": buy.body_pct,
                "buy_dominant_wick_pct": buy.dominant_wick_pct,
                "sell_body_pct": sell.body_pct,
                "sell_dominant_wick_pct": sell.dominant_wick_pct,
                "entry_rule": getattr(cfg.entry_rule, "value", str(cfg.entry_rule)),
                "inverted_entry_rule": getattr(cfg.inverted_entry_rule, "value", str(cfg.inverted_entry_rule)),
                "indicators_added": inds,
                "plain_english": (
                    f"{hammer_context_logic.describe_hammer_context_rules(cfg)}. "
                    f"{hammer_context_logic.describe_hammer_context_entry_exit(cfg)}. "
                    "BUY and SELL each have separate Body/Wicks on the Parameters tabs."
                ),
            }
        cfg = self._build_hammer_strategy_config()
        stack = self._build_indicator_stack()
        inds = stack.enabled_indicator_ids()
        return {
            "pattern": "Hammer",
            "classic_green": cfg.classic_green.value,
            "classic_red": cfg.classic_red.value,
            "inverted_green": cfg.inverted_green.value,
            "inverted_red": cfg.inverted_red.value,
            "classic_hammer_enabled": cfg.enable_classic_hammer,
            "inverted_hammer_enabled": cfg.enable_inverted_hammer,
            "resolved_wick_mode": getattr(cfg.hammer_wick_side, "value", str(cfg.hammer_wick_side)),
            "entry_rule": getattr(cfg.entry_rule, "value", str(cfg.entry_rule)),
            "inverted_entry_rule": getattr(cfg.inverted_entry_rule, "value", str(cfg.inverted_entry_rule)),
            "indicators_added": inds,
            "supertrend_filter": (
                stack.supertrend.apply_trade_filter if "supertrend" in inds else None
            ),
            "vwap_filter": (
                stack.vwap.apply_trade_filter if "vwap" in inds else None
            ),
            "plain_english": (
                f"{logic.describe_hammer_direction_matrix(cfg)}. "
                f"{logic.describe_hammer_entry_exit(cfg)}. "
                "Entry is on the NEXT bar after the signal bar closes."
            ),
        }

    def _collect_preset_snapshot(self) -> dict:
        fields: Dict[str, Any] = {}
        for name, widget in self.field_widgets.items():
            val = self._widget_value(widget)
            if isinstance(val, bool):
                fields[name] = val
            else:
                fields[name] = "" if val is None else str(val)
        timeframes = {}
        for tf, edits in self.timeframe_widgets.items():
            folder = TIMEFRAME_TO_FOLDER[tf]
            cb = self.timeframe_enabled_widgets.get(folder)
            timeframes[tf] = {
                "rr": edits["rr"].text(),
                "sl": edits["sl"].text(),
                "enabled": cb.isChecked() if cb else True,
            }
        sessions_preset = {
            name: (self.session_enabled_widgets[name].isChecked()
                   if name in self.session_enabled_widgets else True)
            for name in sessions.SESSION_ORDER
        }
        tf = self._collect_time_filter_kwargs()
        sessions_preset["time_filter_mode"] = tf["time_filter_mode"]
        sessions_preset["ist_time_start"] = tf["ist_time_start"]
        sessions_preset["ist_time_end"] = tf["ist_time_end"]
        sessions_preset["session_clock"] = tf["session_clock"]
        sessions_preset["ist_time_filter_enabled"] = tf["ist_time_filter_enabled"]

        pattern = self.pattern_combo.currentText()
        stack = self._build_indicator_stack()
        return {
            "version": 2,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "pattern": pattern,
            "trade_rules": self._collect_preset_trade_rules(pattern),
            "backtest": self._collect_preset_backtest_settings(),
            "fields": fields,
            "timeframes": timeframes,
            "sessions": sessions_preset,
            "indicators_added": sorted(self.added_indicator_ids),
            "indicators": {
                "combine_mode": stack.combine_mode.value,
                "supertrend": {
                    "enabled": "supertrend" in self.added_indicator_ids,
                    "atr_period": fields.get("indicators_st_atr_period", "10"),
                    "multiplier": fields.get("indicators_st_multiplier", "3.0"),
                    "apply_trade_filter": fields.get("indicators_st_apply_filter", True),
                },
                "vwap": {
                    "enabled": "vwap" in self.added_indicator_ids,
                    "apply_trade_filter": fields.get("indicators_vwap_apply_filter", True),
                },
            },
        }

    def _set_field_widget_value(self, name: str, value: Any):
        widget = self.field_widgets.get(name)
        if widget is None:
            return
        if isinstance(widget, QLineEdit):
            widget.setText("" if value is None else str(value))
        elif isinstance(widget, QCheckBox):
            if isinstance(value, str):
                widget.setChecked(value.strip().lower() in ("1", "true", "yes", "on"))
            else:
                widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox):
            text = "" if value is None else str(value)
            idx = widget.findData(text)
            if idx >= 0:
                widget.setCurrentIndex(idx)
            elif widget.findText(text) >= 0:
                widget.setCurrentText(text)

    def _migrate_preset_fields_for_pattern(self, pattern: str, fields: dict) -> dict:
        """Fill Hammer-with-candles BUY/SELL fields from plain hammer fields when missing."""
        if pattern != "Hammer with candles":
            return fields
        for side in ("buy", "sell"):
            for name in HAMMER_RATIO_FIELD_NAMES:
                key = f"{side}_{name}"
                if key not in fields and name in fields:
                    fields[key] = fields[name]
        return fields

    def _apply_preset_snapshot(self, data: dict):
        pattern = data.get("pattern", "Hammer")
        if self.pattern_combo.findText(pattern) >= 0:
            self.pattern_combo.setCurrentText(pattern)
        self._apply_pattern_field_visibility()
        fields = dict(data.get("fields") or {})
        fields = self._migrate_preset_fields_for_pattern(pattern, fields)
        if "classic_green" not in fields:
            migrated = logic.direction_matrix_from_legacy_fields(
                green_direction=fields.get("green_direction", "BUY"),
                red_direction=fields.get("red_direction", "SELL"),
                enable_classic=fields.get("enable_classic_hammer", True),
                enable_inverted=fields.get("enable_inverted_hammer", True),
                classic_allow_buy=fields.get("classic_hammer_allow_buy", True),
                classic_allow_sell=fields.get("classic_hammer_allow_sell", True),
                inverted_allow_buy=fields.get("inverted_hammer_allow_buy", True),
                inverted_allow_sell=fields.get("inverted_hammer_allow_sell", True),
            )
            fields.update(migrated)
        if "inverted_entry_rule" not in fields and "entry_rule" in fields:
            fields["inverted_entry_rule"] = fields.get("entry_rule")
            fields["inverted_entry_offset"] = fields.get("entry_offset", "0")
            fields["inverted_buffer_mode"] = fields.get("buffer_mode")
            fields["inverted_sl_buffer_pct"] = fields.get("sl_buffer_pct")
            fields["inverted_sl_buffer_flat"] = fields.get("sl_buffer_flat")
        for name, value in fields.items():
            self._set_field_widget_value(name, value)
        for tf, cfg in (data.get("timeframes") or {}).items():
            edits = self.timeframe_widgets.get(tf)
            if not edits:
                continue
            if "rr" in cfg:
                edits["rr"].setText(str(cfg["rr"]))
            if "sl" in cfg:
                edits["sl"].setText(str(cfg["sl"]))
            folder = TIMEFRAME_TO_FOLDER.get(tf)
            if folder and folder in self.timeframe_enabled_widgets and "enabled" in cfg:
                self.timeframe_enabled_widgets[folder].setChecked(bool(cfg["enabled"]))
        sess_data = data.get("sessions") or {}
        for sess_name in sessions.SESSION_ORDER:
            if sess_name not in sess_data:
                continue
            cb = self.session_enabled_widgets.get(sess_name)
            if cb is not None:
                cb.setChecked(bool(sess_data[sess_name]))
        mode = sess_data.get("time_filter_mode")
        if mode is None:
            # Migrate old presets: IST window checkbox → ist mode
            if sess_data.get("ist_time_filter_enabled"):
                mode = "ist"
            elif sess_data.get("session_clock") == sessions.CLOCK_IST:
                mode = "ist"
            else:
                mode = "sessions"
        if hasattr(self, "time_filter_mode_ist") and hasattr(self, "time_filter_mode_sessions"):
            if mode == "ist":
                self.time_filter_mode_ist.setChecked(True)
            else:
                self.time_filter_mode_sessions.setChecked(True)
        if hasattr(self, "ist_time_start_edit") and sess_data.get("ist_time_start"):
            self.ist_time_start_edit.setText(str(sess_data["ist_time_start"]))
        if hasattr(self, "ist_time_end_edit") and sess_data.get("ist_time_end"):
            self.ist_time_end_edit.setText(str(sess_data["ist_time_end"]))
        self._sync_time_filter_mode_ui()
        for ind_id in list(self.added_indicator_ids):
            self._remove_indicator(ind_id)
        for ind_id in data.get("indicators_added") or []:
            self._add_indicator(ind_id)
        for name, value in (data.get("backtest") or {}).items():
            if name in ("plain_english", "applies_to"):
                continue
            self._set_field_widget_value(name, value)
        # Strict defaults for older presets missing newer Run Settings keys
        if not (data.get("backtest") or {}).get("market_data") and "market_data" not in (data.get("fields") or {}):
            self._set_field_widget_value("market_data", "spot")
        if pattern == "Doji":
            self._fill_empty_doji_widget_defaults()
        self._wire_symmetric_tolerance_fields()
        self._wire_wick_requirement_fields()
        self._wire_sl_mode_fields()
        self._redraw_candle_preview()
        self._refresh_sizing_field_highlights()
        self._refresh_live_strategy_summary()

    def _save_preset_dialog(self):
        name, ok = QInputDialog.getText(
            self,
            "Save current settings (optional)",
            "Name this parameter set (for reload later / live use):",
        )
        if not ok or not name.strip():
            return
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name.strip())
        safe = safe.replace(" ", "_").lower() or "preset"
        filename = f"{safe}.json"
        os.makedirs(PRESETS_DIR, exist_ok=True)
        path = self._preset_path(filename)
        if os.path.isfile(path):
            ans = QMessageBox.question(
                self, "Overwrite?",
                f"'{filename}' already exists. Overwrite?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        payload = self._collect_preset_snapshot()
        payload["display_name"] = name.strip()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError as e:
            QMessageBox.critical(self, "Save Failed", str(e))
            return
        self._refresh_preset_combo(keep_selection=True)
        idx = self.preset_combo.findData(filename)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self._set_app_status(f"Saved settings as {filename} (optional preset — run backtest anytime).")

    def _load_selected_preset(self):
        filename = self.preset_combo.currentData()
        if not filename:
            QMessageBox.information(
                self,
                "Optional presets",
                "Nothing to load — choose a saved file from the list, or keep tuning parameters "
                "and run backtests without any preset.",
            )
            return
        path = self._preset_path(filename)
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Missing File", f"Could not find:\n{path}")
            self._refresh_preset_combo()
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Load Failed", str(e))
            return
        self._apply_preset_snapshot(data)
        self._set_app_status(f"Loaded saved settings from {filename}.")
        self._refresh_live_strategy_summary()

    def _delete_selected_preset(self):
        filename = self.preset_combo.currentData()
        if not filename:
            QMessageBox.information(
                self, "Optional presets",
                "Select a saved file to delete, or leave on “Not using a saved preset”.",
            )
            return
        ans = QMessageBox.question(
            self, "Delete Preset",
            f"Delete preset file '{filename}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        path = self._preset_path(filename)
        try:
            os.remove(path)
        except OSError as e:
            QMessageBox.critical(self, "Delete Failed", str(e))
            return
        self._refresh_preset_combo(keep_selection=False)
        self._set_app_status(f"Deleted {filename}. Current form values unchanged.")

    def _open_folder(self, path: Optional[str]):
        if not path or not os.path.isdir(path):
            QMessageBox.information(self, "No Output Yet", "Run a backtest first.")
            return
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')


class TradeInspectDialog(QDialog):
    """Candlestick chart for one backtest trade: prior bars + entry/SL/TP/exit."""

    BARS_BEFORE = 40
    BARS_AFTER = 25
    DRAW_PALETTE = (
        "#F9AB00",  # amber
        "#1A73E8",  # blue
        "#34A853",  # green
        "#EA4335",  # red
        "#A142F4",  # purple
        "#E37400",  # orange
        "#00ACC1",  # cyan
        "#5F6368",  # gray
        "#000000",  # black
        "#FFFFFF",  # white
    )

    def __init__(
        self,
        parent,
        trade: dict,
        backtest_config,
        indicator_stack=None,
        trades: Optional[List[dict]] = None,
        trade_index: int = 0,
    ):
        super().__init__(parent)
        self.setWindowTitle("Inspect trade — candlesticks")
        self.resize(1280, 860)
        self.setMinimumSize(960, 640)
        self.setFocusPolicy(Qt.StrongFocus)
        self._cfg = backtest_config
        self._trades: List[dict] = list(trades) if trades else [trade]
        if not self._trades:
            self._trades = [trade]
        self._trade_index = max(0, min(int(trade_index), len(self._trades) - 1))
        trade = self._trades[self._trade_index]

        self._indicator_stack = indicator_stack
        if self._indicator_stack is None:
            self._indicator_stack = getattr(backtest_config, "indicator_stack", None)
        # If the saved run had no indicators, fall back to the live UI stack.
        if (
            self._indicator_stack is None
            or not getattr(self._indicator_stack, "enabled_indicator_ids", lambda: [])()
        ):
            parent_dash = parent
            if parent_dash is not None and hasattr(parent_dash, "_build_indicator_stack"):
                ui_stack = parent_dash._build_indicator_stack()
                if ui_stack.enabled_indicator_ids():
                    self._indicator_stack = ui_stack

        layout = QVBoxLayout(self)

        nav = QHBoxLayout()
        self._prev_btn = QPushButton("▲ Prev")
        self._prev_btn.setToolTip("Previous trade (↑ or ←)")
        self._prev_btn.clicked.connect(lambda: self._navigate(-1))
        self._next_btn = QPushButton("▼ Next")
        self._next_btn.setToolTip("Next trade (↓ or →)")
        self._next_btn.clicked.connect(lambda: self._navigate(1))
        self._nav_label = QLabel("")
        self._nav_label.setObjectName("sectionHint")
        nav.addWidget(self._prev_btn)
        nav.addWidget(self._next_btn)
        nav.addWidget(self._nav_label, 1)
        nav_hint = QLabel("↑↓ / ←→ browse trades like a photo preview")
        nav_hint.setObjectName("sectionHint")
        nav.addWidget(nav_hint)
        layout.addLayout(nav)
        self._refresh_nav_controls()

        self._summary_label = QLabel(
            self._summary_html(trade, backtest_config, self._indicator_stack)
        )
        self._summary_label.setTextFormat(Qt.RichText)
        self._summary_label.setWordWrap(True)
        self._summary_label.setObjectName("sectionHint")
        layout.addWidget(self._summary_label)

        stack = self._indicator_stack
        ind_bits = []
        if stack is not None:
            if getattr(stack.supertrend, "enabled", False):
                ind_bits.append(
                    f"<span style='color:#34A853'>━</span>/<span style='color:#EA4335'>━</span> "
                    f"SuperTrend (ATR {stack.supertrend.atr_period}, ×{stack.supertrend.multiplier:g})"
                )
            if getattr(stack.vwap, "enabled", False):
                ind_bits.append("<span style='color:#1A73E8'>- -</span> VWAP")
        legend = QLabel(
            "<span style='color:#188038'>■</span> BUY / green &nbsp; "
            "<span style='color:#D93025'>■</span> SELL / red &nbsp; "
            "<span style='background:#FFF2A8; padding:1px 6px;'>signal bar</span> "
            "(bar before entry) &nbsp; "
            "<span style='background:#D2E3FC; padding:1px 6px;'>entry bar</span> &nbsp; "
            "<span style='color:#188038'>— TP</span> &nbsp; "
            "<span style='color:#D93025'>— SL</span> &nbsp; "
            "<span style='color:#5F6368'>× exit</span>"
            + ((" &nbsp; · &nbsp; " + " &nbsp; ".join(ind_bits)) if ind_bits else
               " &nbsp; · &nbsp; <i>No indicators — add SuperTrend/VWAP on Indicators tab, then Inspect again</i>")
        )
        legend.setTextFormat(Qt.RichText)
        legend.setObjectName("sectionHint")
        layout.addWidget(legend)

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
            from matplotlib.figure import Figure
        except ImportError:
            layout.addWidget(QLabel("matplotlib Qt backend is required to draw the chart."))
            close = QPushButton("Close")
            close.clicked.connect(self.accept)
            layout.addWidget(close)
            return

        self._ax = None
        self._canvas = None
        self._fig = None
        self._draw_mode = "cursor"
        self._draw_color = "#F9AB00"
        self._pending_point = None
        self._preview_artists = []
        self._user_drawings = []  # each item: list of matplotlib artists
        self._crosshair_h = None
        self._crosshair_v = None
        self._crosshair_txt = None
        self._cid_click = self._cid_move = self._cid_key = None

        tools = QHBoxLayout()
        tools.setSpacing(6)
        self._draw_hint = QLabel("Tools: pick a draw mode, then click the chart. Esc cancels. ⌫ undoes.")
        self._draw_hint.setObjectName("sectionHint")
        tools.addWidget(self._draw_hint, 1)

        self._draw_btn_group = QButtonGroup(self)
        self._draw_btn_group.setExclusive(True)
        mode_specs = [
            ("cursor", "Cursor"),
            ("hline", "H-Line"),
            ("vline", "V-Line"),
            ("trend", "Trendline"),
            ("ray", "Ray"),
        ]
        self._draw_mode_buttons = {}
        for mode_id, label in mode_specs:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(mode_id == "cursor")
            btn.setToolTip({
                "cursor": "Pan/zoom with toolbar below; hover shows price",
                "hline": "Click once to place a horizontal price line",
                "vline": "Click once to place a vertical time line",
                "trend": "Click two points for a trendline",
                "ray": "Click two points; line extends across the chart",
            }[mode_id])
            self._draw_btn_group.addButton(btn)
            self._draw_mode_buttons[mode_id] = btn
            btn.clicked.connect(lambda checked=False, m=mode_id: self._set_draw_mode(m))
            tools.addWidget(btn)

        undo_btn = QPushButton("Undo")
        undo_btn.setToolTip("Remove last drawing (Backspace / Delete)")
        undo_btn.clicked.connect(self._undo_drawing)
        tools.addWidget(undo_btn)
        clear_btn = QPushButton("Clear drawings")
        clear_btn.setToolTip("Remove all user drawings")
        clear_btn.clicked.connect(self._clear_drawings)
        tools.addWidget(clear_btn)
        layout.addLayout(tools)

        # Color palette for new drawings (TradingView-style swatches + custom)
        color_row = QHBoxLayout()
        color_row.setSpacing(4)
        color_lbl = QLabel("Color:")
        color_lbl.setObjectName("sectionHint")
        color_row.addWidget(color_lbl)
        self._color_btn_group = QButtonGroup(self)
        self._color_btn_group.setExclusive(True)
        self._color_swatch_buttons = {}
        for hex_color in self.DRAW_PALETTE:
            sw = QPushButton()
            sw.setCheckable(True)
            sw.setFixedSize(22, 22)
            sw.setCursor(Qt.PointingHandCursor)
            sw.setToolTip(hex_color)
            sw.setStyleSheet(
                f"QPushButton {{ background-color: {hex_color}; border: 1px solid #5F6368; "
                f"border-radius: 3px; }}"
                f"QPushButton:checked {{ border: 2px solid #202124; }}"
            )
            sw.setChecked(hex_color.upper() == self._draw_color.upper())
            self._color_btn_group.addButton(sw)
            self._color_swatch_buttons[hex_color] = sw
            sw.clicked.connect(lambda checked=False, c=hex_color: self._set_draw_color(c))
            color_row.addWidget(sw)

        self._color_preview = QPushButton()
        self._color_preview.setFixedSize(28, 22)
        self._color_preview.setToolTip("Current draw color — click for full palette")
        self._color_preview.setCursor(Qt.PointingHandCursor)
        self._color_preview.clicked.connect(self._pick_custom_color)
        color_row.addWidget(self._color_preview)
        custom_btn = QPushButton("Custom…")
        custom_btn.setToolTip("Open full color picker")
        custom_btn.clicked.connect(self._pick_custom_color)
        color_row.addWidget(custom_btn)
        color_row.addStretch(1)
        layout.addLayout(color_row)
        self._refresh_color_preview()

        self._coord_label = QLabel("—")
        self._coord_label.setObjectName("sectionHint")
        layout.addWidget(self._coord_label)

        fig = Figure(figsize=(12.2, 6.4), dpi=110)
        canvas = FigureCanvasQTAgg(fig)
        canvas.setFocusPolicy(Qt.StrongFocus)
        self._fig = fig
        self._canvas = canvas
        layout.addWidget(canvas, 1)
        self._nav = NavigationToolbar2QT(canvas, self)
        layout.addWidget(self._nav)
        self._draw_chart(fig, trade, backtest_config)
        canvas.draw()
        self._install_draw_events()

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

    def _refresh_nav_controls(self) -> None:
        n = len(getattr(self, "_trades", []) or [])
        i = getattr(self, "_trade_index", 0)
        if hasattr(self, "_nav_label"):
            self._nav_label.setText(f"Trade <b>{i + 1}</b> of <b>{n}</b>" if n else "No trades")
            self._nav_label.setTextFormat(Qt.RichText)
        if hasattr(self, "_prev_btn"):
            self._prev_btn.setEnabled(n > 1 and i > 0)
        if hasattr(self, "_next_btn"):
            self._next_btn.setEnabled(n > 1 and i < n - 1)
        trade = self._trades[i] if n and 0 <= i < n else {}
        side = str(trade.get("direction") or trade.get("outcome") or "trade")
        self.setWindowTitle(f"Inspect trade — {i + 1}/{n} · {side}")

    def _navigate(self, delta: int) -> None:
        trades = getattr(self, "_trades", None) or []
        if len(trades) <= 1:
            return
        new_idx = getattr(self, "_trade_index", 0) + int(delta)
        if new_idx < 0 or new_idx >= len(trades):
            return
        self._show_trade_at(new_idx)

    def _show_trade_at(self, index: int) -> None:
        trades = getattr(self, "_trades", None) or []
        if not trades:
            return
        index = max(0, min(int(index), len(trades) - 1))
        self._trade_index = index
        trade = trades[index]
        self._refresh_nav_controls()
        if hasattr(self, "_summary_label"):
            self._summary_label.setText(
                self._summary_html(trade, self._cfg, self._indicator_stack)
            )
        if self._fig is None or self._canvas is None:
            return
        self._disconnect_draw_events()
        self._pending_point = None
        self._preview_artists = []
        self._user_drawings = []
        self._crosshair_h = self._crosshair_v = self._crosshair_txt = None
        self._fig.clear()
        self._ax = None
        try:
            self._draw_chart(self._fig, trade, self._cfg)
            self._canvas.draw()
            self._install_draw_events()
        except Exception as e:
            self._fig.clear()
            ax = self._fig.add_subplot(111)
            ax.text(0.5, 0.5, f"Could not draw trade:\n{e}", ha="center", va="center")
            ax.axis("off")
            self._ax = ax
            self._canvas.draw()
        # Sync parent trades table selection
        parent = self.parent()
        table = getattr(parent, "trade_ledger_table", None) if parent is not None else None
        if table is not None and 0 <= index < table.rowCount():
            table.selectRow(index)
            table.setCurrentCell(index, 0)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key_Up, Qt.Key_Left):
            self._navigate(-1)
            event.accept()
            return
        if key in (Qt.Key_Down, Qt.Key_Right):
            self._navigate(1)
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _summary_html(trade: dict, cfg=None, stack=None) -> str:
        def fmt(v, money=False):
            if v is None:
                return "—"
            if money and isinstance(v, (int, float)):
                return f"{float(v):,.2f}"
            return str(v)

        ind_note = ""
        if stack is None and cfg is not None:
            stack = getattr(cfg, "indicator_stack", None)
        if stack is not None:
            parts = []
            if getattr(stack.supertrend, "enabled", False):
                filt = "filter ON" if stack.supertrend.apply_trade_filter else "filter OFF"
                parts.append(f"SuperTrend ({filt})")
            if getattr(stack.vwap, "enabled", False):
                filt = "filter ON" if stack.vwap.apply_trade_filter else "filter OFF"
                parts.append(f"VWAP ({filt})")
            if parts:
                ind_note = " · indicators: " + ", ".join(parts)

        return (
            f"<b>{fmt(trade.get('direction'))}</b> · {fmt(trade.get('timeframe'))} · "
            f"session <b>{fmt(trade.get('session'))}</b> · "
            f"{fmt(trade.get('exit_model'))} · outcome <b>{fmt(trade.get('outcome'))}</b>"
            f"{ind_note}<br>"
            f"Entry {fmt(trade.get('entry_time'))} @ {fmt(trade.get('entry_price'), True)} · "
            f"SL {fmt(trade.get('stop_loss'), True)} · TP {fmt(trade.get('target'), True)} · "
            f"Exit {fmt(trade.get('exit_time'))} @ {fmt(trade.get('exit_price'), True)} · "
            f"PnL {fmt(trade.get('pnl_usd'), True)} · bars {fmt(trade.get('bars_held'))} · "
            f"variant {fmt(trade.get('pattern_variant'))}"
        )

    def _draw_chart(self, fig, trade: dict, cfg) -> None:
        import numpy as np
        from matplotlib.patches import Rectangle
        from indicators.supertrend import compute_supertrend
        from indicators.vwap import compute_vwap

        ax = fig.add_subplot(111)
        self._ax = ax
        tf_folder = self._resolve_timeframe_folder(trade.get("timeframe"))
        entry_time = trade.get("entry_time")
        if entry_time is None:
            ax.text(0.5, 0.5, "No entry_time on this trade.", ha="center", va="center")
            ax.axis("off")
            return
        if isinstance(entry_time, str):
            entry_time = datetime.fromisoformat(entry_time.replace("Z", ""))

        df = backtest.load_candles_df(
            cfg.data_root, cfg.symbol, tf_folder,
            getattr(cfg, "start_date", None), getattr(cfg, "end_date", None),
            preferred=backtest.market_preferred_for_config(cfg),
        )
        if df.height == 0:
            ax.text(0.5, 0.5, f"No candles for {cfg.symbol}/{tf_folder}", ha="center", va="center")
            ax.axis("off")
            return

        times = df["datetime"].to_list()
        # Find entry bar index (nearest at/after entry_time)
        entry_idx = None
        for i, ts in enumerate(times):
            if ts == entry_time or (ts is not None and ts >= entry_time):
                entry_idx = i
                break
        if entry_idx is None:
            entry_idx = len(times) - 1

        signal_idx = max(0, entry_idx - 1)
        bars_held = trade.get("bars_held") or 0
        try:
            bars_held = int(bars_held)
        except (TypeError, ValueError):
            bars_held = 0
        start = max(0, signal_idx - self.BARS_BEFORE)
        end = min(len(times), entry_idx + max(self.BARS_AFTER, bars_held + 3) + 1)
        window = df.slice(start, end - start)
        if window.height == 0:
            ax.text(0.5, 0.5, "Empty candle window.", ha="center", va="center")
            ax.axis("off")
            return

        # Compute indicators on the FULL series (ATR / session VWAP need history),
        # then slice to the visible window so lines match the backtest filters.
        stack = self._indicator_stack
        show_st = bool(stack and getattr(stack.supertrend, "enabled", False))
        show_vwap = bool(stack and getattr(stack.vwap, "enabled", False))
        st_win = dir_win = vwap_win = None
        if show_st or show_vwap:
            high_all = df["high"].to_numpy()
            low_all = df["low"].to_numpy()
            close_all = df["close"].to_numpy()
            vol_all = df["volume"].to_numpy() if "volume" in df.columns else None
            if show_st:
                st_line, st_dir = compute_supertrend(
                    high_all, low_all, close_all,
                    atr_period=int(stack.supertrend.atr_period),
                    multiplier=float(stack.supertrend.multiplier),
                )
                st_win = st_line[start:end]
                dir_win = st_dir[start:end]
            if show_vwap:
                vwap_all = compute_vwap(high_all, low_all, close_all, vol_all, timestamps=times)
                vwap_win = vwap_all[start:end]

        w_times = window["datetime"].to_list()
        opens = window["open"].to_list()
        highs = window["high"].to_list()
        lows = window["low"].to_list()
        closes = window["close"].to_list()
        x = list(range(len(w_times)))

        local_signal = signal_idx - start
        local_entry = entry_idx - start

        for i in x:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            up = c >= o
            color = "#188038" if up else "#D93025"
            ax.vlines(i, l, h, color=color, linewidth=1.0, zorder=2)
            body_bottom = min(o, c)
            body_h = max(abs(c - o), (max(highs) - min(lows)) * 0.001)
            face = "#188038" if up else "#D93025"
            ax.add_patch(Rectangle(
                (i - 0.35, body_bottom), 0.7, body_h,
                facecolor=face, edgecolor=face, linewidth=0.6, zorder=3,
            ))

        # SuperTrend: green = bullish, red = bearish (same as filter engine)
        st_plotted = False
        if st_win is not None and len(st_win) == len(x):
            st_label_used = False
            for i in range(len(x) - 1):
                a, b = st_win[i], st_win[i + 1]
                if a != a or b != b:  # NaN
                    continue
                bull = int(dir_win[i]) >= 0 if dir_win is not None else True
                col = "#34A853" if bull else "#EA4335"
                lbl = None
                if not st_label_used:
                    lbl = "SuperTrend"
                    st_label_used = True
                ax.plot([i, i + 1], [a, b], color=col, linewidth=2.2, zorder=6, label=lbl)
                st_plotted = True

        vwap_plotted = False
        if vwap_win is not None and len(vwap_win) == len(x):
            ys = [float(v) if v == v else np.nan for v in vwap_win]
            if any(v == v for v in ys):
                ax.plot(x, ys, color="#1A73E8", linewidth=2.0, linestyle="--", zorder=6, label="VWAP")
                vwap_plotted = True

        # Highlight signal (pattern) bar and entry bar
        y_vals = list(lows) + list(highs)
        if st_win is not None:
            y_vals.extend([float(v) for v in st_win if v == v])
        if vwap_win is not None:
            y_vals.extend([float(v) for v in vwap_win if v == v])
        y0, y1 = min(y_vals), max(y_vals)
        pad = (y1 - y0) * 0.04 if y1 > y0 else 1.0
        if 0 <= local_signal < len(x):
            ax.axvspan(local_signal - 0.45, local_signal + 0.45, color="#FFF2A8", alpha=0.55, zorder=1)
            ax.text(local_signal, y1 + pad * 0.2, "signal", ha="center", fontsize=8, color="#B06000")
        if 0 <= local_entry < len(x):
            ax.axvspan(local_entry - 0.45, local_entry + 0.45, color="#D2E3FC", alpha=0.55, zorder=1)
            ax.text(local_entry, y1 + pad * 0.55, "entry", ha="center", fontsize=8, color="#1967D2")

        sl = trade.get("stop_loss")
        tp = trade.get("target")
        entry_px = trade.get("entry_price")
        if entry_px is not None:
            ax.axhline(float(entry_px), color="#1967D2", linewidth=1.1, linestyle="--", label="Entry", zorder=5)
        if sl is not None:
            ax.axhline(float(sl), color="#D93025", linewidth=1.2, linestyle="-", label="SL", zorder=5)
        if tp is not None:
            ax.axhline(float(tp), color="#188038", linewidth=1.2, linestyle="-", label="TP", zorder=5)

        exit_time = trade.get("exit_time")
        exit_price = trade.get("exit_price")
        if exit_time is not None and exit_price is not None:
            if isinstance(exit_time, str):
                try:
                    exit_time = datetime.fromisoformat(exit_time.replace("Z", ""))
                except ValueError:
                    exit_time = None
            if exit_time is not None:
                for i, ts in enumerate(w_times):
                    if ts == exit_time or (ts is not None and ts >= exit_time):
                        ax.scatter([i], [float(exit_price)], marker="X", s=80, color="#5F6368",
                                   zorder=7, label="Exit")
                        break

        # Tick labels: sparse timestamps
        step = max(1, len(x) // 8)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(
            [ts.strftime("%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts) for ts in w_times[::step]],
            rotation=25, ha="right", fontsize=8,
        )
        ax.set_xlim(-1, len(x))
        ax.set_ylim(y0 - pad, y1 + pad * 1.8)
        ax.set_ylabel("Price")
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
        direction = trade.get("direction") or ""
        ind_title = []
        if show_st:
            ind_title.append("ST" + ("✓" if st_plotted else "?"))
        if show_vwap:
            ind_title.append("VWAP" + ("✓" if vwap_plotted else "?"))
        ind_sfx = (" + " + "/".join(ind_title)) if ind_title else ""
        ax.set_title(
            f"{cfg.symbol} {tf_folder} — {direction} — prior + signal + exit{ind_sfx}",
            fontsize=11,
        )
        fig.tight_layout()

    def _draw_pen_color(self) -> str:
        return getattr(self, "_draw_color", "#F9AB00") or "#F9AB00"

    def _refresh_color_preview(self) -> None:
        c = self._draw_pen_color()
        if hasattr(self, "_color_preview"):
            border = "#5F6368" if c.upper() in ("#FFFFFF", "#FFF") else "#202124"
            self._color_preview.setStyleSheet(
                f"QPushButton {{ background-color: {c}; border: 2px solid {border}; "
                f"border-radius: 3px; }}"
            )
            self._color_preview.setToolTip(f"Current color {c} — click for full palette")

    def _set_draw_color(self, hex_color: str) -> None:
        if not hex_color:
            return
        if not str(hex_color).startswith("#"):
            hex_color = "#" + str(hex_color)
        self._draw_color = str(hex_color).upper()
        # Sync swatch checked state (custom colors may not be in palette)
        matched = False
        for c, btn in getattr(self, "_color_swatch_buttons", {}).items():
            on = c.upper() == self._draw_color
            btn.setChecked(on)
            matched = matched or on
        if not matched:
            for btn in getattr(self, "_color_swatch_buttons", {}).values():
                btn.setChecked(False)
        self._refresh_color_preview()

    def _pick_custom_color(self) -> None:
        initial = QColor(self._draw_pen_color())
        chosen = QColorDialog.getColor(initial, self, "Drawing color")
        if chosen.isValid():
            self._set_draw_color(chosen.name())

    def _set_draw_mode(self, mode: str) -> None:
        self._draw_mode = mode
        self._cancel_pending()
        hints = {
            "cursor": "Cursor: hover for price · use toolbar to pan/zoom",
            "hline": "H-Line: click once on the chart to place a horizontal line",
            "vline": "V-Line: click once to place a vertical line",
            "trend": "Trendline: click start point, then end point",
            "ray": "Ray: click start, then direction — extends across the chart",
        }
        if hasattr(self, "_draw_hint"):
            self._draw_hint.setText(hints.get(mode, ""))
        if self._canvas is not None:
            self._canvas.setCursor(Qt.CrossCursor if mode != "cursor" else Qt.ArrowCursor)
            self._canvas.setFocus()

    def _install_draw_events(self) -> None:
        if self._canvas is None or self._ax is None:
            return
        self._disconnect_draw_events()
        self._cid_click = self._canvas.mpl_connect("button_press_event", self._on_draw_click)
        self._cid_move = self._canvas.mpl_connect("motion_notify_event", self._on_draw_move)
        self._cid_key = self._canvas.mpl_connect("key_press_event", self._on_draw_key)
        self._canvas.setFocus()

    def _disconnect_draw_events(self) -> None:
        if self._canvas is None:
            return
        for attr in ("_cid_click", "_cid_move", "_cid_key"):
            cid = getattr(self, attr, None)
            if cid is not None:
                try:
                    self._canvas.mpl_disconnect(cid)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _data_xy(self, event):
        if event is None or event.inaxes is not self._ax:
            return None
        if event.xdata is None or event.ydata is None:
            return None
        return float(event.xdata), float(event.ydata)

    def _clear_preview(self) -> None:
        for art in self._preview_artists:
            try:
                art.remove()
            except Exception:
                pass
        self._preview_artists = []

    def _cancel_pending(self) -> None:
        self._pending_point = None
        self._clear_preview()
        if self._canvas is not None:
            self._canvas.draw_idle()

    def _on_draw_key(self, event) -> None:
        key = (event.key or "").lower()
        if key == "escape":
            self._cancel_pending()
            if "cursor" in self._draw_mode_buttons:
                self._draw_mode_buttons["cursor"].setChecked(True)
            self._set_draw_mode("cursor")
        elif key in ("backspace", "delete"):
            self._undo_drawing()
        elif key in ("up", "left"):
            self._navigate(-1)
        elif key in ("down", "right"):
            self._navigate(1)

    def _on_draw_move(self, event) -> None:
        xy = self._data_xy(event)
        if xy is None:
            return
        x, y = xy
        if hasattr(self, "_coord_label"):
            self._coord_label.setText(f"Bar ≈ {x:.1f}   Price {y:,.2f}")

        # Crosshair
        if self._ax is not None:
            if self._crosshair_h is None:
                self._crosshair_h = self._ax.axhline(
                    y, color="#9AA0A6", linewidth=0.7, linestyle=":", alpha=0.85, zorder=20,
                )
                self._crosshair_v = self._ax.axvline(
                    x, color="#9AA0A6", linewidth=0.7, linestyle=":", alpha=0.85, zorder=20,
                )
            else:
                self._crosshair_h.set_ydata([y, y])
                self._crosshair_v.set_xdata([x, x])

        # Live preview for 2-click tools
        pen = self._draw_pen_color()
        if self._pending_point is not None and self._draw_mode in ("trend", "ray"):
            self._clear_preview()
            x0, y0 = self._pending_point
            if self._draw_mode == "trend":
                (ln,) = self._ax.plot(
                    [x0, x], [y0, y], color=pen, linewidth=1.4, linestyle="--",
                    alpha=0.8, zorder=19,
                )
                self._preview_artists.append(ln)
            else:
                xs, ys = self._extend_ray(x0, y0, x, y)
                (ln,) = self._ax.plot(
                    xs, ys, color=pen, linewidth=1.4, linestyle="--",
                    alpha=0.8, zorder=19,
                )
                self._preview_artists.append(ln)
        if self._canvas is not None:
            self._canvas.draw_idle()

    def _on_draw_click(self, event) -> None:
        if event.button != 1:
            return
        # Don't draw while using nav pan/zoom tools
        if getattr(self, "_nav", None) is not None:
            mode = getattr(self._nav, "mode", "")
            if mode:
                return
        if self._draw_mode == "cursor":
            return
        xy = self._data_xy(event)
        if xy is None:
            return
        x, y = xy
        pen = self._draw_pen_color()

        if self._draw_mode == "hline":
            self._add_hline(y)
            return
        if self._draw_mode == "vline":
            self._add_vline(x)
            return
        if self._draw_mode in ("trend", "ray"):
            if self._pending_point is None:
                self._pending_point = (x, y)
                (mk,) = self._ax.plot(
                    [x], [y], marker="o", markersize=5, color=pen, zorder=19,
                )
                self._preview_artists.append(mk)
                self._canvas.draw_idle()
                if hasattr(self, "_draw_hint"):
                    self._draw_hint.setText("Click the second point… (Esc to cancel)")
            else:
                x0, y0 = self._pending_point
                if self._draw_mode == "trend":
                    self._add_trendline(x0, y0, x, y)
                else:
                    self._add_ray(x0, y0, x, y)
                self._cancel_pending()
                if hasattr(self, "_draw_hint"):
                    self._draw_hint.setText(
                        "Trendline: click start, then end"
                        if self._draw_mode == "trend"
                        else "Ray: click start, then direction — extends across the chart"
                    )

    def _extend_ray(self, x0, y0, x1, y1):
        """Extend segment through axes limits (TradingView-style ray both ways)."""
        xlim = self._ax.get_xlim()
        ylim = self._ax.get_ylim()
        dx = x1 - x0
        dy = y1 - y0
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            return [x0, x1], [y0, y1]
        if abs(dx) < 1e-12:
            return [x0, x0], [ylim[0], ylim[1]]
        m = dy / dx
        b = y0 - m * x0
        xs = [xlim[0], xlim[1]]
        ys = [m * xs[0] + b, m * xs[1] + b]
        return xs, ys

    def _commit_artists(self, artists, note: str = "") -> None:
        self._user_drawings.append(artists)
        if self._canvas is not None:
            self._canvas.draw_idle()
        if note and hasattr(self, "_coord_label"):
            self._coord_label.setText(note)

    def _add_hline(self, y: float) -> None:
        pen = self._draw_pen_color()
        ln = self._ax.axhline(y, color=pen, linewidth=1.5, linestyle="-", zorder=18)
        txt = self._ax.text(
            self._ax.get_xlim()[1], y, f"  {y:,.2f}",
            color=pen, fontsize=8, va="center", ha="left", zorder=18,
            clip_on=False,
        )
        self._commit_artists([ln, txt], f"H-Line @ {y:,.2f}")

    def _add_vline(self, x: float) -> None:
        pen = self._draw_pen_color()
        ln = self._ax.axvline(x, color=pen, linewidth=1.3, linestyle="--", zorder=18)
        self._commit_artists([ln], f"V-Line @ bar {x:.1f}")

    def _add_trendline(self, x0, y0, x1, y1) -> None:
        pen = self._draw_pen_color()
        (ln,) = self._ax.plot(
            [x0, x1], [y0, y1], color=pen, linewidth=1.8, zorder=18,
        )
        (a,) = self._ax.plot([x0], [y0], marker="o", markersize=4, color=pen, zorder=18)
        (b,) = self._ax.plot([x1], [y1], marker="o", markersize=4, color=pen, zorder=18)
        self._commit_artists([ln, a, b], f"Trendline {y0:,.2f} → {y1:,.2f}")

    def _add_ray(self, x0, y0, x1, y1) -> None:
        pen = self._draw_pen_color()
        xs, ys = self._extend_ray(x0, y0, x1, y1)
        (ln,) = self._ax.plot(xs, ys, color=pen, linewidth=1.6, zorder=18)
        (a,) = self._ax.plot([x0], [y0], marker="o", markersize=4, color=pen, zorder=18)
        (b,) = self._ax.plot([x1], [y1], marker="o", markersize=4, color=pen, zorder=18)
        self._commit_artists([ln, a, b], f"Ray {y0:,.2f} → {y1:,.2f}")

    def _undo_drawing(self) -> None:
        if not self._user_drawings:
            return
        arts = self._user_drawings.pop()
        for art in arts:
            try:
                art.remove()
            except Exception:
                pass
        if self._canvas is not None:
            self._canvas.draw_idle()

    def _clear_drawings(self) -> None:
        self._cancel_pending()
        while self._user_drawings:
            self._undo_drawing()

    @staticmethod
    def _resolve_timeframe_folder(raw) -> str:
        if not raw:
            return "1hour"
        s = str(raw)
        reverse = {v: v for v in TIMEFRAME_TO_FOLDER.values()}
        reverse.update(TIMEFRAME_TO_FOLDER)
        return reverse.get(s, s)


class ClickableImageLabel(QLabel):
    """A QLabel that opens the full-resolution chart when clicked."""

    def __init__(self, path: str, title: str, dashboard: "BacktestDashboard"):
        super().__init__()
        self.path = path
        self.title = title
        self.dashboard = dashboard
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.dashboard is not None:
            self.dashboard.open_zoomed_chart(self.path, self.title)
            event.accept()
            return
        super().mousePressEvent(event)


# ============================================================================
# CHECKBOX TICK-MARK ICON
# ----------------------------------------------------------------------------
# A Qt stylesheet checkbox indicator can only be given a flat colour on
# its own, which reads as an empty coloured square rather than an
# obviously "checked" box. This draws a small white tick-mark PNG at
# startup and registers it under the "checkicon:" search prefix used by
# QCheckBox::indicator:checked in STYLESHEET above. Purely cosmetic --
# no effect on any field value or behaviour.
# ============================================================================
def _generate_checkmark_icon() -> str:
    icon_dir = os.path.join(tempfile.gettempdir(), "hammer_dashboard_assets")
    os.makedirs(icon_dir, exist_ok=True)
    icon_path = os.path.join(icon_dir, "checkmark.png")

    size = 32
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#FFFFFF"))
    pen.setWidth(4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    tick = QPolygonF([QPointF(7, 17), QPointF(13, 23), QPointF(25, 9)])
    painter.drawPolyline(tick)
    painter.end()

    image.save(icon_path, "PNG")
    return icon_dir


# ============================================================================
# ENTRY POINT
# ============================================================================
def launch() -> None:
    """Start the Qt dashboard. Used by python dashboard.py and by frozen main.py."""
    if sys.platform == "darwin":
        # Discard any legacy dock layout before Qt reads it (prevents off-screen float restore).
        _early_settings = QSettings("HammerDashboard", "BacktestDashboard")
        _early_settings.remove("window/dockState")
        del _early_settings

    app = QApplication(sys.argv)
    QDir.addSearchPath("checkicon", _generate_checkmark_icon())
    app.setStyleSheet(STYLESHEET)

    # Custom app icon: logo.ico or app_icon.ico next to the app / bundled via PyInstaller datas.
    for icon_filename in ("logo.ico", "app_icon.ico", "app_icon.png", "logo.png"):
        icon_path = get_asset_path(icon_filename)
        if os.path.exists(icon_path):
            app_icon = QIcon(icon_path)
            app.setWindowIcon(app_icon)
            break

    window = BacktestDashboard()
    if not app.windowIcon().isNull():
        window.setWindowIcon(app.windowIcon())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    launch()