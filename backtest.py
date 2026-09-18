"""
backtest.py
===========
Advanced Hammer-Candle Strategy Backtest Engine -- POLARS + VECTORIZED VERSION.

WHAT CHANGED FROM THE PREVIOUS VERSION
----------------------------------------
1. Data loading now uses Polars (pl.read_csv / pl.concat) instead of the
   Python `csv` module -- reading, deduplicating, sorting, and filtering
   hundreds of thousands of rows is done with Polars' native (Rust-backed)
   engine instead of Python-level row loops. This is dramatically faster
   on large multi-year datasets.

2. The forward-walk exit simulation (the single most expensive part of
   any backtest -- "did SL or Target get hit first?") is now done with
   NumPy array operations (cummin/cummax + argmax) per trade, instead of
   a candle-by-candle Python for-loop. This replaces an O(n) Python loop
   per trade with an O(n) NumPy C-loop per trade -- typically 20-100x
   faster in practice, since NumPy's vectorized comparisons and
   accumulate functions run in compiled code, not the Python interpreter.

   HONEST NOTE ON "FULL" VECTORIZATION: true single-shot vectorization
   across ALL trades simultaneously (one array operation covering every
   trade at once) is not meaningfully possible here without padding every
   trade's forward-scan window to a fixed-size matrix (trades x max_scan),
   which would use enormous memory for large datasets and produce no real
   speed benefit over the per-trade NumPy approach used here. What IS
   vectorized: every per-candle comparison and every metric aggregation
   -- nothing loops candle-by-candle in pure Python anymore.

3. ALL metrics computation (win rate, profit factor, drawdown, Sharpe,
   Sortino, streaks, and every by-year/by-month/by-timeframe/by-direction
   breakdown) is now done with Polars group_by().agg() expressions
   instead of manual Python loops summing lists.

4. Skipped-trade handling is now explicit and reported everywhere: every
   signal logic.py rejected (ignored_signals), every signal skipped due
   to trade overlap (SKIPPED_OVERLAP), and every trade that ran out of
   future data before resolving (STILL_OPEN) are all counted, exported,
   and included in every metrics table's totals so nothing silently
   disappears from your record.

WHAT DID NOT CHANGE
----------------------
This is still a PURE EXECUTOR of logic.py's rules. Hammer detection,
entry price, stop loss, and target calculation are 100% logic.py's
job, imported and called unchanged. This file only simulates what
happens AFTER a signal is generated, and computes metrics from the
result. The three exit-ambiguity models (worst_case / best_case /
candle_bias) and the overlap / position-sizing config switches all work
exactly as before -- only the internal implementation is now Polars +
NumPy instead of pure Python.

--------------------------------------------------------------------------
OUTPUT FILES (all CSV, ready for Excel/Numbers import)
--------------------------------------------------------------------------
    output/<run_name>/trade_ledger.csv          -- every trade, every field
    output/<run_name>/ignored_signals.csv        -- every signal logic.py
                                                     rejected + why
    output/<run_name>/skipped_overlap.csv        -- every signal skipped
                                                     due to an already-open
                                                     trade (per exit model)
    output/<run_name>/still_open.csv             -- every trade that never
                                                     resolved within the
                                                     scan window
    output/<run_name>/summary_overall.csv        -- one row per exit model
    output/<run_name>/summary_by_year.csv        -- year x exit model
    output/<run_name>/summary_by_month.csv       -- year-month x exit model
    output/<run_name>/summary_by_timeframe.csv   -- timeframe x exit model
    output/<run_name>/summary_by_direction.csv   -- BUY/SELL x exit model
    output/<run_name>/summary_by_session.csv     -- Asian/London/US x exit model (IC Markets server time)
"""

import os
import sys
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import List, Dict, Optional, Tuple, Any

import numpy as np
import polars as pl

import logic  # hammer pattern rules
import doji_logic
import hammer_context_logic
import sessions
from indicators.config import IndicatorStackConfig
from indicators.filter import apply_indicator_filters


# ============================================================================
# SECTION 1: BACKTEST CONFIG -- everything you'd want to change is here
# ============================================================================

class PositionSizingMode(Enum):
    FIXED_UNITS = "FIXED_UNITS"
    FIXED_RISK_USD = "FIXED_RISK_USD"
    PERCENT_OF_EQUITY = "PERCENT_OF_EQUITY"


class ExitModel(Enum):
    WORST_CASE = "worst_case"     # SL assumed hit first when both in range
    BEST_CASE = "best_case"       # Target assumed hit first when both in range
    CANDLE_BIAS = "candle_bias"   # tiebreak using the exit candle's own color


class MarketDataSource(Enum):
    """Which on-disk tree to backtest."""
    SPOT = "spot"         # data/spot/<symbol> (falls back to legacy data/<symbol>)
    FUTURES = "futures"   # data/futures/<symbol>
    BOTH = "both"         # run spot and futures (whichever exist)


ALL_EXIT_MODELS = [ExitModel.WORST_CASE, ExitModel.BEST_CASE, ExitModel.CANDLE_BIAS]


@dataclass
class BacktestConfig:
    # ---- pattern selection ----
    # "hammer" -> logic.py  |  "doji" -> doji_logic.py
    pattern_type: str = "hammer"

    # ---- data location ----
    data_root: str = "data"          # matches fetcher.py's OUTPUT_ROOT
    symbol: str = "XAUUSD"
    # When spot and futures both exist, pick which to backtest (default: spot).
    market_data: MarketDataSource = MarketDataSource.SPOT

    # ---- fetcher.py label -> logic.py label mapping ----
    # fetcher.py folders are named "3min","5min","10min","15min","30min","1hour"
    # logic.py's timeframe_settings dict uses "3m","5m","10m","15m","30m","1h"
    # This bridges the two WITHOUT modifying either file.
    timeframe_folder_to_logic_label: Dict[str, str] = field(default_factory=lambda: {
        "1min": "1m", "3min": "3m", "5min": "5m", "10min": "10m",
        "15min": "15m", "30min": "30m", "1hour": "1h",
    })

    # ---- which timeframes to backtest (folder names, matching fetcher.py) ----
    timeframes_to_test: List[str] = field(default_factory=lambda: [
        "1min", "3min", "5min", "10min", "15min", "30min", "1hour"
    ])

    # ---- date range filter (None = use everything available) ----
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

    # ---- strategy config (logic.StrategyConfig for hammer,
    #      doji_logic.DojiStrategyConfig for doji) ----
    strategy_config: object = field(default_factory=lambda: logic.StrategyConfig())

    # ---- optional indicator filters (SuperTrend, VWAP, …) ----
    indicator_stack: IndicatorStackConfig = field(default_factory=IndicatorStackConfig)

    # ---- forward-walk limit ----
    # Maximum number of FUTURE candles to scan looking for SL/Target hit
    # before giving up and marking the trade "STILL_OPEN". Prevents
    # runaway scans on trades that never resolve. Set high (e.g. 5000)
    # for higher timeframes, since a stale 1h trade could take weeks.
    max_forward_candles: int = 5000

    # ---- position sizing ----
    position_sizing_mode: PositionSizingMode = PositionSizingMode.FIXED_RISK_USD
    position_size: float = 1.0              # used when mode = FIXED_UNITS
    fixed_risk_usd: float = 100.0            # used when mode = FIXED_RISK_USD
    risk_pct_of_equity: float = 1.0          # used when mode = PERCENT_OF_EQUITY (percent, e.g. 1.0 = 1%)
    starting_capital: float = 10000.0

    # ---- SAFETY CAPS for PERCENT_OF_EQUITY sizing ----
    # PERCENT_OF_EQUITY compounds every trade's position size off the
    # CURRENT running equity. With a large number of signals (tens of
    # thousands, realistic for 3m/5m data across years) and ANY sustained
    # positive edge, compounding across that many trades is mathematically
    # guaranteed to produce astronomical numbers -- e.g. a modest 0.13R
    # average edge per trade compounded 65,000 times produces a growth
    # factor of ~10^36. This is correct arithmetic, not a bug -- but it is
    # NOT a realistic simulation of real trading, since no real account
    # can compound uninterrupted through tens of thousands of trades
    # without hitting margin limits, liquidity limits, broker position
    # caps, or slippage that erodes the edge (none of which a backtest
    # naturally models). Two safeguards are applied so results stay in a
    # realistic, readable range instead of overflowing:
    #   1. equity_floor_usd: if running equity drops to/below this, all
    #      further trades in that exit-model's simulation are sized at
    #      ZERO (treated as blown/halted).
    #   2. max_equity_multiple: running equity is HARD-CAPPED at this
    #      multiple of starting_capital (e.g. 50 = equity can never be
    #      simulated above 50x your starting capital, gains beyond that
    #      are not compounded further). This keeps PERCENT_OF_EQUITY
    #      results bounded and interpretable at high trade counts. If you
    #      deliberately want to see uncapped theoretical compounding,
    #      raise this very high (e.g. 1e12) -- but treat any result that
    #      large as a mathematical artifact of trade COUNT, not a
    #      realistic account projection. FIXED_RISK_USD sizing is
    #      immune to this issue entirely (no compounding), and is the
    #      more realistic default for very high trade count backtests.
    equity_floor_usd: float = 0.0
    max_equity_multiple: float = 50.0

    # ---- overlap handling ----
    allow_overlapping_trades: bool = False

    # ---- session / time-of-day filter ----
    # Subset of sessions.SESSION_ORDER; default all three = no session filter.
    sessions_enabled: List[str] = field(default_factory=lambda: [
        "Asian", "London", "US",
    ])
    # Clock for Asian/London/US buckets: "broker" (CSV/MT5) or "ist" (India).
    session_clock: str = "broker"
    # IC Markets UTC offset for IST conversion; None = auto from bar date (US DST).
    broker_utc_offset_hours: Optional[float] = None
    # Optional allow-list of IST clock times (independent of session checkboxes).
    ist_time_filter_enabled: bool = False
    ist_time_start: str = "00:00"
    ist_time_end: str = "23:59"

    # ---- costs (set to 0 to ignore, fully optional) ----
    commission_per_trade: float = 0.0        # flat $ per trade (round turn)
    slippage_usd: float = 0.0                # flat $ added to unfavorable side of entry/exit

    # ---- output ----
    output_root: str = "output"
    run_name: str = "backtest_run"


# ============================================================================
# SECTION 2: DATA LOADING (Polars -- reads fetcher.py's CSV output structure)
# ============================================================================

# Spot / futures live under data/spot/<symbol> and data/futures/<symbol>.
# Legacy client layout is data/<symbol>. All loaders accept either.
_MARKET_SUBFOLDERS = ("spot", "futures")


def _default_data_anchor() -> str:
    """Install root (stub folder) — same idea as dashboard.get_app_dir()."""
    try:
        from update.paths import install_root

        return install_root()
    except Exception:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))


def resolve_data_root(data_root: str, anchor_dir: Optional[str] = None) -> str:
    """
    Make data_root absolute and stable across launch cwd.

    Relative paths (e.g. "data") are resolved against the app/package
    directory first, then the process cwd. This fixes the common failure
    where CSVs "aren't detected" until restart when the dashboard was
    launched from a different working directory.
    """
    raw = (data_root or "").strip() or "data"
    raw = os.path.expanduser(raw)
    if os.path.isabs(raw):
        return os.path.normpath(raw)

    anchor = anchor_dir or _default_data_anchor()
    candidate_app = os.path.normpath(os.path.join(anchor, raw))
    candidate_cwd = os.path.normpath(os.path.abspath(raw))
    if os.path.isdir(candidate_app):
        return candidate_app
    if os.path.isdir(candidate_cwd):
        return candidate_cwd
    return candidate_app


def resolve_symbol_folder(data_root: str, symbol: str) -> str:
    """Return the on-disk symbol folder name (case-insensitive match)."""
    sym = (symbol or "").strip()
    if not sym:
        return sym
    if not os.path.isdir(data_root):
        return sym
    # Always prefer the real directory name from listdir. On case-insensitive
    # filesystems (common on macOS), os.path.isdir("…/xauusd") can be True even
    # when the folder is stored as "XAUUSD" — returning the typed case would
    # break later path joins on case-sensitive machines / zip tools.
    lower = sym.lower()
    try:
        for name in os.listdir(data_root):
            path = os.path.join(data_root, name)
            if os.path.isdir(path) and name.lower() == lower:
                return name
    except OSError:
        pass
    return sym


def _data_parent_root(data_root: str) -> str:
    """
    If data_root is …/spot or …/futures, return the parent so we can switch
    markets without the user re-picking the folder.
    """
    root = os.path.normpath(data_root)
    if os.path.basename(root).lower() in _MARKET_SUBFOLDERS:
        parent = os.path.dirname(root)
        return parent or root
    return root


def resolve_symbol_data_root(
    data_root: str,
    symbol: str,
    *,
    anchor_dir: Optional[str] = None,
    preferred: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Locate the folder that actually contains <symbol>/<timeframe>/ CSVs.

    Supports:
      data_root/<symbol>/…                 (legacy — treated as spot)
      data_root/spot/<symbol>/…            (Fetch spot)
      data_root/futures/<symbol>/…         (Fetch futures)

    preferred: "spot" | "futures" | "both" (or MarketDataSource).
    "both" resolves like spot for a single path (use expand_market_targets
    to run both). If data_root already points at spot/ or futures/, the
    parent is used as the search base so switching still works.

    Returns (effective_data_root, on_disk_symbol).
    """
    root = resolve_data_root(data_root, anchor_dir=anchor_dir)
    sym = (symbol or "").strip()
    if not sym:
        return root, sym

    if isinstance(preferred, MarketDataSource):
        pref = preferred.value
    else:
        pref = (preferred or MarketDataSource.SPOT.value).strip().lower()
    # Compat with older presets / labels
    if pref in ("", "none", "default", "auto", "legacy"):
        pref = MarketDataSource.SPOT.value
    if pref == MarketDataSource.BOTH.value:
        pref = MarketDataSource.SPOT.value

    parent = _data_parent_root(root)

    def _from_market(market: str) -> Optional[Tuple[str, str]]:
        market_root = os.path.join(parent, market)
        if not os.path.isdir(market_root):
            return None
        nested_sym = resolve_symbol_folder(market_root, sym)
        if os.path.isdir(os.path.join(market_root, nested_sym)):
            return market_root, nested_sym
        return None

    def _from_legacy() -> Optional[Tuple[str, str]]:
        direct_sym = resolve_symbol_folder(parent, sym)
        if os.path.isdir(os.path.join(parent, direct_sym)):
            return parent, direct_sym
        return None

    if pref == MarketDataSource.FUTURES.value:
        hit = _from_market("futures")
        if hit:
            return hit
        fut_root = os.path.join(parent, "futures")
        return fut_root, resolve_symbol_folder(
            fut_root if os.path.isdir(fut_root) else parent, sym,
        )

    # SPOT (default): prefer data/spot/<symbol>, else legacy data/<symbol>
    hit = _from_market("spot")
    if hit:
        return hit
    hit = _from_legacy()
    if hit:
        return hit
    spot_root = os.path.join(parent, "spot")
    return spot_root, resolve_symbol_folder(
        spot_root if os.path.isdir(spot_root) else parent, sym,
    )


def expand_market_targets(
    data_root: str,
    symbol: str,
    market_data: Optional[Any] = None,
    *,
    anchor_dir: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    """
    Markets to backtest: [(label, effective_root, symbol), …].

    Spot includes legacy data/<symbol>. Futures is data/futures/<symbol>.
    Both returns every market that actually has a symbol folder on disk.
    """
    if isinstance(market_data, MarketDataSource):
        pref = market_data.value
    else:
        pref = (str(market_data or MarketDataSource.SPOT.value)).strip().lower()
    if pref in ("", "auto", "legacy", "default"):
        pref = MarketDataSource.SPOT.value

    if pref == MarketDataSource.BOTH.value:
        labels = ["spot", "futures"]
    elif pref == MarketDataSource.FUTURES.value:
        labels = ["futures"]
    else:
        labels = ["spot"]

    out: List[Tuple[str, str, str]] = []
    for label in labels:
        eff_root, eff_sym = resolve_symbol_data_root(
            data_root, symbol, anchor_dir=anchor_dir, preferred=label,
        )
        if eff_sym and os.path.isdir(os.path.join(eff_root, eff_sym)):
            out.append((label, eff_root, eff_sym))
    return out


def find_available_symbols(
    data_root: str,
    *,
    anchor_dir: Optional[str] = None,
) -> List[str]:
    """Labels for error dialogs: XAUUSD, spot/XAUUSD, futures/GCZ5, …"""
    root = resolve_data_root(data_root, anchor_dir=anchor_dir)
    # Always inventory from the parent so spot+futures both show up
    root = _data_parent_root(root)
    found: List[str] = []
    if not os.path.isdir(root):
        return found

    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found

    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name.lower() in _MARKET_SUBFOLDERS:
            try:
                nested = sorted(os.listdir(path))
            except OSError:
                continue
            for child in nested:
                if os.path.isdir(os.path.join(path, child)):
                    found.append(f"{name}/{child}")
        else:
            found.append(name)
    return found


def find_csv_files(
    data_root: str,
    symbol: str,
    timeframe_folder: str,
    *,
    preferred: Optional[str] = None,
) -> List[str]:
    """
    Finds every monthly CSV file for one symbol+timeframe under
    fetcher.py's folder structure:
        data_root/<symbol>/<timeframe_folder>/<year>/<symbol>_<tf>_<year>-<month>.csv
    Also accepts data_root/spot|futures/<symbol>/… and loose *_FULL.csv
    files in the timeframe folder. Returns paths sorted chronologically.
    """
    data_root, symbol = resolve_symbol_data_root(
        data_root, symbol, preferred=preferred,
    )
    base = os.path.join(data_root, symbol, timeframe_folder)
    if not os.path.isdir(base):
        return []

    monthly_files = []
    try:
        entries = sorted(os.listdir(base))
    except OSError as e:
        print(f"    [WARN] Cannot list {base}: {e}")
        return []

    for entry in entries:
        year_dir = os.path.join(base, entry)
        if os.path.isdir(year_dir) and entry.isdigit():
            try:
                year_files = sorted(os.listdir(year_dir))
            except OSError as e:
                print(f"    [WARN] Cannot list {year_dir}: {e}")
                continue
            for fname in year_files:
                if fname.endswith(".csv"):
                    monthly_files.append(os.path.join(year_dir, fname))

    if monthly_files:
        return sorted(monthly_files)

    # fallback: any CSV directly in the timeframe folder (FULL or loose files)
    loose = []
    try:
        loose_names = sorted(os.listdir(base))
    except OSError:
        loose_names = []
    for fname in loose_names:
        if fname.endswith(".csv") and os.path.isfile(os.path.join(base, fname)):
            loose.append(os.path.join(base, fname))
    if loose:
        return loose

    return []


def load_candles_df(
    data_root: str,
    symbol: str,
    timeframe_folder: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    *,
    preferred: Optional[str] = None,
) -> pl.DataFrame:
    """
    Loads all CSV files for one symbol+timeframe into a single Polars
    DataFrame: columns [datetime, open, high, low, close, volume(optional)].
    Uses Polars' native CSV reader (Rust-backed, reads/parses/concats in
    one pass) instead of Python's csv module row-by-row.

    Deduplicates by datetime, sorts chronologically, and applies the
    optional date range filter -- all as vectorized Polars expressions,
    not Python loops.
    """
    data_root, symbol = resolve_symbol_data_root(
        data_root, symbol, preferred=preferred,
    )
    files = find_csv_files(
        data_root, symbol, timeframe_folder, preferred=preferred,
    )
    if not files:
        return pl.DataFrame(schema={
            "datetime": pl.Datetime, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64,
        })

    frames = []
    for filepath in files:
        try:
            df = pl.read_csv(
                filepath,
                try_parse_dates=False,
                schema_overrides={"open": pl.Float64, "high": pl.Float64,
                                   "low": pl.Float64, "close": pl.Float64},
            )
        except Exception as e:
            print(f"    [WARN] Could not read {filepath}: {e}")
            continue
        if "datetime" not in df.columns:
            print(f"    [WARN] No datetime column in {filepath} — skipped")
            continue
        # parse datetime column explicitly (format matches fetcher.py's
        # "%Y-%m-%d %H:%M:%S" output); fall back to flexible parse if needed
        parsed = pl.col("datetime").str.strptime(
            pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False,
        )
        df = df.with_columns(parsed.alias("datetime"))
        if df.filter(pl.col("datetime").is_not_null()).height == 0:
            try:
                df = df.with_columns(
                    pl.col("datetime").str.to_datetime(strict=False).alias("datetime")
                )
            except Exception:
                pass
        frames.append(df)

    if not frames:
        return pl.DataFrame(schema={
            "datetime": pl.Datetime, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64,
        })

    combined = pl.concat(frames, how="diagonal_relaxed")

    combined = (
        combined
        .filter(pl.col("datetime").is_not_null())
        .filter(
            pl.col("open").is_not_null() & pl.col("high").is_not_null() &
            pl.col("low").is_not_null() & pl.col("close").is_not_null()
        )
        .unique(subset=["datetime"], keep="first")
        .sort("datetime")
    )

    if start_date is not None:
        combined = combined.filter(pl.col("datetime") >= start_date)
    if end_date is not None:
        combined = combined.filter(pl.col("datetime") <= end_date)

    return combined


def df_to_candles(df: pl.DataFrame) -> List["logic.Candle"]:
    """
    Converts a Polars DataFrame into the list[logic.Candle] that
    logic.run_strategy() expects. logic.py's hammer-detection loop is
    row-by-row by design (it's your rule engine, not something this file
    silently rewrites) -- this is the one necessary hand-off point
    between Polars-land and logic.py-land.
    """
    if df.is_empty():
        return []

    ts = df["datetime"].to_list()
    o = df["open"].to_list()
    h = df["high"].to_list()
    l = df["low"].to_list()
    c = df["close"].to_list()

    return [
        logic.Candle(timestamp=ts[i], open=o[i], high=h[i], low=l[i], close=c[i])
        for i in range(len(ts))
    ]


# ============================================================================
# SECTION 3: TRADE OUTCOME STRUCTURE
# ============================================================================

class TradeOutcome(Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    STILL_OPEN = "STILL_OPEN"          # ran out of forward data / hit max scan limit
    SKIPPED_OVERLAP = "SKIPPED_OVERLAP"  # another trade was open, this signal was skipped


@dataclass
class SimulatedTrade:
    """
    One row of the final trade ledger. Holds the original TradeSignal
    from logic.py PLUS the simulated outcome under each exit model, PLUS
    P&L and metadata needed for the metrics tables.

    position_size / risk_usd are per exit model because overlap filtering
    and PERCENT_OF_EQUITY compounding follow a different equity path in
    each model.
    """
    signal: "logic.TradeSignal"
    timeframe_folder: str
    entry_time: datetime
    outcomes: Dict[ExitModel, Tuple[TradeOutcome, Optional[float], Optional[datetime], Optional[int]]]
    position_size: Dict[ExitModel, float] = field(default_factory=dict)
    risk_usd: Dict[ExitModel, float] = field(default_factory=dict)
    pnl: Dict[ExitModel, float] = field(default_factory=dict)
    equity_after: Dict[ExitModel, float] = field(default_factory=dict)


# ============================================================================
# SECTION 4: VECTORIZED FORWARD-WALK SIMULATION
# ============================================================================
#
# For each trade, this uses NumPy's cummin/cummax + argmax to find the
# FIRST future candle where SL is touched and the FIRST future candle
# where Target is touched, as single vectorized array operations rather
# than a Python for-loop over candles. Whichever happens at the earlier
# index wins; if both happen at the SAME index, that candle is genuinely
# ambiguous from OHLC data alone, and is resolved per the exit_model
# (see module docstring for the reasoning behind each model).

def resolve_all_exit_models_for_trade(
    direction: "logic.TradeDirection",
    sl: float,
    target: float,
    future_high: np.ndarray,
    future_low: np.ndarray,
    future_open: np.ndarray,
    future_close: np.ndarray,
    future_timestamps: List[datetime],
    max_scan: int,
) -> Dict[ExitModel, Tuple[TradeOutcome, Optional[float], Optional[datetime], Optional[int]]]:
    """
    Computes the outcome under ALL THREE exit models for one trade in a
    single vectorized pass (the cummin/cummax/argmax work is shared;
    only the final ambiguous-candle tiebreak differs per model).

    PERFORMANCE NOTE: NumPy's vectorized boolean masking + argmax has
    real per-call overhead (allocating temporary arrays) that only pays
    off once the scan window is long enough. Benchmarked crossover on
    this machine: NumPy wins decisively (13x-100x+) once a trade's
    forward scan runs into the hundreds-to-thousands of candles, which
    is the realistic case for max_forward_candles defaults on higher
    timeframes or trades that take a while to resolve. For very short
    windows (a handful of candles), a plain Python loop is actually
    faster since there's no array-allocation overhead to pay. This
    function auto-selects: short windows use the plain-loop fast path,
    long windows use the full NumPy path -- so you get the best of both
    without having to choose.
    """
    n = min(len(future_high), max_scan)

    if n == 0:
        still_open = (TradeOutcome.STILL_OPEN, None, None, None)
        return {m: still_open for m in ALL_EXIT_MODELS}

    # ---- FAST PATH: short scan windows -- plain loop beats NumPy here ----
    SHORT_SCAN_THRESHOLD = 64  # benchmarked crossover point, adjust if needed

    if n <= SHORT_SCAN_THRESHOLD:
        return _resolve_short_scan(direction, sl, target, future_high, future_low,
                                    future_open, future_close, future_timestamps, n)

    # ---- VECTORIZED PATH: long scan windows -- NumPy wins here ----
    high = future_high[:n]
    low = future_low[:n]
    op = future_open[:n]
    cl = future_close[:n]

    if direction == logic.TradeDirection.BUY:
        sl_mask = low <= sl
        target_mask = high >= target
    else:
        sl_mask = high >= sl
        target_mask = low <= target

    sl_hit_any = bool(sl_mask.any())
    target_hit_any = bool(target_mask.any())

    sl_idx = int(np.argmax(sl_mask)) if sl_hit_any else None
    target_idx = int(np.argmax(target_mask)) if target_hit_any else None

    def _make_result(outcome: TradeOutcome, idx: Optional[int], exit_price: Optional[float]):
        if idx is None:
            return (outcome, None, None, None)
        return (outcome, exit_price, future_timestamps[idx], idx + 1)

    # ---- case: neither ever hit ----
    if sl_idx is None and target_idx is None:
        result = (TradeOutcome.STILL_OPEN, None, None, None)
        return {m: result for m in ALL_EXIT_MODELS}

    # ---- case: only target hit ----
    if sl_idx is None:
        result = _make_result(TradeOutcome.WIN, target_idx, target)
        return {m: result for m in ALL_EXIT_MODELS}

    # ---- case: only SL hit ----
    if target_idx is None:
        result = _make_result(TradeOutcome.LOSS, sl_idx, sl)
        return {m: result for m in ALL_EXIT_MODELS}

    # ---- case: SL hit strictly before target ----
    if sl_idx < target_idx:
        result = _make_result(TradeOutcome.LOSS, sl_idx, sl)
        return {m: result for m in ALL_EXIT_MODELS}

    # ---- case: target hit strictly before SL ----
    if target_idx < sl_idx:
        result = _make_result(TradeOutcome.WIN, target_idx, target)
        return {m: result for m in ALL_EXIT_MODELS}

    # ---- case: SAME candle -- genuinely ambiguous, resolve per model ----
    idx = sl_idx  # == target_idx
    candle_is_green = bool(cl[idx] > op[idx])

    results: Dict[ExitModel, Tuple] = {}
    results[ExitModel.WORST_CASE] = _make_result(TradeOutcome.LOSS, idx, sl)
    results[ExitModel.BEST_CASE] = _make_result(TradeOutcome.WIN, idx, target)

    if direction == logic.TradeDirection.BUY:
        if candle_is_green:
            results[ExitModel.CANDLE_BIAS] = _make_result(TradeOutcome.WIN, idx, target)
        else:
            results[ExitModel.CANDLE_BIAS] = _make_result(TradeOutcome.LOSS, idx, sl)
    else:  # SELL
        if not candle_is_green:  # red candle
            results[ExitModel.CANDLE_BIAS] = _make_result(TradeOutcome.WIN, idx, target)
        else:
            results[ExitModel.CANDLE_BIAS] = _make_result(TradeOutcome.LOSS, idx, sl)

    return results


def _resolve_short_scan(
    direction: "logic.TradeDirection",
    sl: float,
    target: float,
    future_high: np.ndarray,
    future_low: np.ndarray,
    future_open: np.ndarray,
    future_close: np.ndarray,
    future_timestamps: List[datetime],
    n: int,
) -> Dict[ExitModel, Tuple[TradeOutcome, Optional[float], Optional[datetime], Optional[int]]]:
    """
    Plain-loop fast path for short scan windows (see SHORT_SCAN_THRESHOLD
    in resolve_all_exit_models_for_trade). Produces IDENTICAL results to
    the vectorized path -- this is purely a performance optimization for
    small n, not a different algorithm. Verified against the vectorized
    path in tests.
    """
    for i in range(n):
        h, l, o, c = future_high[i], future_low[i], future_open[i], future_close[i]

        if direction == logic.TradeDirection.BUY:
            hit_sl = l <= sl
            hit_target = h >= target
        else:
            hit_sl = h >= sl
            hit_target = l <= target

        if hit_sl and hit_target:
            candle_is_green = bool(c > o)
            results: Dict[ExitModel, Tuple] = {
                ExitModel.WORST_CASE: (TradeOutcome.LOSS, sl, future_timestamps[i], i + 1),
                ExitModel.BEST_CASE: (TradeOutcome.WIN, target, future_timestamps[i], i + 1),
            }
            if direction == logic.TradeDirection.BUY:
                results[ExitModel.CANDLE_BIAS] = (
                    (TradeOutcome.WIN, target, future_timestamps[i], i + 1) if candle_is_green
                    else (TradeOutcome.LOSS, sl, future_timestamps[i], i + 1)
                )
            else:
                results[ExitModel.CANDLE_BIAS] = (
                    (TradeOutcome.WIN, target, future_timestamps[i], i + 1) if not candle_is_green
                    else (TradeOutcome.LOSS, sl, future_timestamps[i], i + 1)
                )
            return results

        elif hit_sl:
            result = (TradeOutcome.LOSS, sl, future_timestamps[i], i + 1)
            return {m: result for m in ALL_EXIT_MODELS}

        elif hit_target:
            result = (TradeOutcome.WIN, target, future_timestamps[i], i + 1)
            return {m: result for m in ALL_EXIT_MODELS}

    result = (TradeOutcome.STILL_OPEN, None, None, None)
    return {m: result for m in ALL_EXIT_MODELS}


# ============================================================================
# SECTION 5: POSITION SIZING
# ============================================================================

def _sizing_mode(config: BacktestConfig) -> PositionSizingMode:
    mode = config.position_sizing_mode
    if isinstance(mode, PositionSizingMode):
        return mode
    return PositionSizingMode(str(mode))


def validate_position_sizing_config(config: BacktestConfig) -> Optional[str]:
    """Human-readable error if Run Settings cannot size trades correctly."""
    try:
        mode = _sizing_mode(config)
    except ValueError:
        return f"Unknown position sizing mode: {config.position_sizing_mode!r}"

    if config.starting_capital <= 0:
        return "Starting Capital must be greater than 0."
    if config.commission_per_trade < 0:
        return "Commission per Trade cannot be negative."
    if config.slippage_usd < 0:
        return "Slippage cannot be negative."

    if mode == PositionSizingMode.FIXED_UNITS:
        if config.position_size <= 0 or not math.isfinite(config.position_size):
            return "Fixed Units mode needs Position Size > 0."
    elif mode == PositionSizingMode.FIXED_RISK_USD:
        if config.fixed_risk_usd <= 0 or not math.isfinite(config.fixed_risk_usd):
            return "Fixed Risk ($) mode needs Fixed Risk per Trade > 0."
    elif mode == PositionSizingMode.PERCENT_OF_EQUITY:
        if config.risk_pct_of_equity <= 0 or not math.isfinite(config.risk_pct_of_equity):
            return "Percent of Equity mode needs Risk per Trade (%) > 0."
        if config.risk_pct_of_equity > 100:
            return "Risk per Trade (%) cannot exceed 100."
        if config.max_equity_multiple <= 0:
            return "Max Equity Multiple must be greater than 0 (try 50)."
    return None


def calculate_position_size(
    trade_signal: "logic.TradeSignal",
    config: BacktestConfig,
    current_equity: float,
) -> Tuple[float, float]:
    """
    Returns (position_size, risk_usd_for_this_trade).

    FIXED_UNITS: constant lots/units. Running equity never changes size.
    FIXED_RISK_USD: constant $ risk. Size = risk_usd / (entry-to-SL distance).
        Running equity is a scoreboard only — it does not compound size.
    PERCENT_OF_EQUITY: risk_usd = current equity * risk%. Size grows/shrinks
        with the account. Halted at equity_floor_usd. Sizing equity is capped
        at starting_capital * max_equity_multiple so high trade counts cannot
        explode into astronomical lots.
    """
    risk_per_unit = float(trade_signal.risk)
    if not math.isfinite(risk_per_unit) or risk_per_unit <= 0:
        return 0.0, 0.0

    mode = _sizing_mode(config)

    if mode == PositionSizingMode.FIXED_UNITS:
        size = float(config.position_size)
        if not math.isfinite(size) or size <= 0:
            return 0.0, 0.0
        return size, size * risk_per_unit

    if mode == PositionSizingMode.FIXED_RISK_USD:
        risk_usd = float(config.fixed_risk_usd)
        if not math.isfinite(risk_usd) or risk_usd <= 0:
            return 0.0, 0.0
        return risk_usd / risk_per_unit, risk_usd

    if mode == PositionSizingMode.PERCENT_OF_EQUITY:
        if current_equity <= config.equity_floor_usd or current_equity <= 0:
            return 0.0, 0.0
        multiple = float(config.max_equity_multiple)
        if not math.isfinite(multiple) or multiple <= 0:
            multiple = 50.0
        equity_cap = config.starting_capital * multiple
        sizing_equity = min(float(current_equity), equity_cap)
        pct = float(config.risk_pct_of_equity)
        if not math.isfinite(pct) or pct <= 0:
            return 0.0, 0.0
        risk_usd = sizing_equity * (pct / 100.0)
        return risk_usd / risk_per_unit, risk_usd

    raise ValueError(f"Unknown position_sizing_mode: {config.position_sizing_mode}")


def calculate_pnl(
    trade_signal: "logic.TradeSignal",
    outcome: TradeOutcome,
    exit_price: Optional[float],
    position_size: float,
    config: BacktestConfig,
) -> float:
    """Computes $ P&L for one trade under one exit outcome."""
    if outcome == TradeOutcome.STILL_OPEN or exit_price is None:
        return 0.0

    direction = trade_signal.direction
    entry = trade_signal.entry_price

    entry_adj = entry + config.slippage_usd if direction == logic.TradeDirection.BUY else entry - config.slippage_usd
    exit_adj = exit_price - config.slippage_usd if direction == logic.TradeDirection.BUY else exit_price + config.slippage_usd

    if direction == logic.TradeDirection.BUY:
        gross_pnl = (exit_adj - entry_adj) * position_size
    else:
        gross_pnl = (entry_adj - exit_adj) * position_size

    return gross_pnl - config.commission_per_trade


# ============================================================================
# SECTION 6: MAIN BACKTEST LOOP (per timeframe)
# ============================================================================

def market_preferred_for_config(config: "BacktestConfig") -> str:
    """spot | futures — never 'both' (use sub-config per market for BOTH runs)."""
    md = getattr(config, "market_data", MarketDataSource.SPOT)
    if isinstance(md, MarketDataSource):
        pref = md.value
    else:
        pref = str(md or MarketDataSource.SPOT.value).strip().lower()
    if pref in ("", "auto", "legacy", "default", "both"):
        return MarketDataSource.SPOT.value
    return pref


def simulate_timeframe_outcomes(
    timeframe_folder: str,
    config: BacktestConfig,
) -> Tuple[Dict[ExitModel, List[Tuple]], List["logic.TradeSignal"]]:
    """
    Load candles, generate signals, resolve SL/TP under all exit models,
    and apply per-timeframe overlap. Does not size positions.
    """
    logic_label = config.timeframe_folder_to_logic_label.get(timeframe_folder, timeframe_folder)
    preferred = market_preferred_for_config(config)

    df = load_candles_df(
        config.data_root, config.symbol, timeframe_folder,
        config.start_date, config.end_date,
        preferred=preferred,
    )

    if df.height < 3:
        print(f"[SKIP] {timeframe_folder}: not enough candle data "
              f"({df.height} candles found).")
        empty = {m: [] for m in ALL_EXIT_MODELS}
        return empty, []

    candles = df_to_candles(df)

    # Precompute NumPy arrays ONCE for the whole timeframe (shared across
    # every trade's forward-walk -- avoids re-extracting arrays per trade)
    all_high = df["high"].to_numpy()
    all_low = df["low"].to_numpy()
    all_open = df["open"].to_numpy()
    all_close = df["close"].to_numpy()
    all_timestamps = df["datetime"].to_list()

    if config.pattern_type == "doji":
        all_signals = doji_logic.run_strategy(
            candles, timeframe=logic_label, config=config.strategy_config,
        )
    elif hammer_context_logic.is_context_pattern_type(config.pattern_type):
        all_signals = hammer_context_logic.run_strategy(
            candles, timeframe=logic_label, config=config.strategy_config,
        )
    else:
        all_signals = logic.run_strategy(
            candles, timeframe=logic_label, config=config.strategy_config,
        )

    if config.indicator_stack.enabled_indicator_ids():
        all_signals = apply_indicator_filters(all_signals, df, config.indicator_stack)

    enabled_sessions = set(config.sessions_enabled or sessions.SESSION_ORDER)
    session_skipped, ist_skipped = sessions.apply_session_and_time_filters(
        all_signals,
        sessions_enabled=list(enabled_sessions),
        session_clock=getattr(config, "session_clock", sessions.CLOCK_BROKER),
        broker_utc_offset_hours=getattr(config, "broker_utc_offset_hours", None),
        ist_time_filter_enabled=bool(getattr(config, "ist_time_filter_enabled", False)),
        ist_time_start=getattr(config, "ist_time_start", "00:00"),
        ist_time_end=getattr(config, "ist_time_end", "23:59"),
    )
    if session_skipped or ist_skipped:
        clock = getattr(config, "session_clock", sessions.CLOCK_BROKER)
        enabled_names = [s for s in sessions.SESSION_ORDER if s in enabled_sessions]
        bits = []
        if session_skipped:
            bits.append(
                f"session skipped {session_skipped} ({', '.join(enabled_names) or 'none'}; clock={clock})"
            )
        if ist_skipped:
            bits.append(
                f"IST time skipped {ist_skipped} "
                f"({getattr(config, 'ist_time_start', '00:00')}–{getattr(config, 'ist_time_end', '23:59')} IST)"
            )
        print(f"    [{timeframe_folder}] Filter: " + "; ".join(bits))

    taken_signals = [s for s in all_signals if not s.ignored]
    ignored_signals = [s for s in all_signals if s.ignored]

    timestamp_to_index = {ts: i for i, ts in enumerate(all_timestamps)}

    open_until: Dict[ExitModel, Optional[datetime]] = {m: None for m in ALL_EXIT_MODELS}

    taken_signals.sort(key=lambda s: s.entry_candle.timestamp)

    # ---- PASS 1: resolve every trade's raw outcome (SL/Target hit, when,
    #      at what price) under all 3 exit models. This does NOT depend on
    #      position sizing or equity at all, so it's safe to do up front
    #      for every trade regardless of overlap settings. ----
    raw_results: List[Tuple["logic.TradeSignal", Dict]] = []
    for sig in taken_signals:
        entry_idx = timestamp_to_index.get(sig.entry_candle.timestamp)
        if entry_idx is None:
            print(
                f"    [WARN] {timeframe_folder}: dropped signal at "
                f"{sig.entry_candle.timestamp} — entry bar not in candle index."
            )
            continue

        fill_idx = entry_idx
        if getattr(sig, "await_limit_fill", False):
            found = hammer_context_logic.find_limit_fill_index(
                sig.direction,
                float(sig.entry_price),
                float(sig.stop_loss),
                all_high,
                all_low,
                all_open,
                entry_idx,
                config.max_forward_candles,
            )
            if found is None:
                # Limit never filled — treat as ignored for this timeframe pass
                continue
            fill_idx = found
            fill_open = float(all_open[fill_idx])
            limit_px = float(sig.entry_price)
            sl_px = float(sig.stop_loss)
            # Gap into the limit: fill at open when it is a better price still above SL.
            # Keep target fixed (RR from signal entry) — pullback must not move TP.
            if sig.direction == logic.TradeDirection.BUY:
                if fill_open <= limit_px and fill_open > sl_px:
                    sig.entry_price = fill_open
                sig.risk = float(sig.entry_price) - sl_px
            else:
                if fill_open >= limit_px and fill_open < sl_px:
                    sig.entry_price = fill_open
                sig.risk = sl_px - float(sig.entry_price)
            # Align overlap / event timing to the actual fill bar
            try:
                sig.entry_candle = logic.Candle(
                    all_timestamps[fill_idx],
                    fill_open,
                    float(all_high[fill_idx]),
                    float(all_low[fill_idx]),
                    float(all_close[fill_idx]),
                )
            except Exception:
                pass

        start = fill_idx + 1
        end = min(start + config.max_forward_candles, len(all_high))

        raw_outcomes = resolve_all_exit_models_for_trade(
            direction=sig.direction, sl=sig.stop_loss, target=sig.target,
            future_high=all_high[start:end], future_low=all_low[start:end],
            future_open=all_open[start:end], future_close=all_close[start:end],
            future_timestamps=all_timestamps[start:end],
            max_scan=config.max_forward_candles,
        )
        raw_results.append((sig, raw_outcomes))

    # ---- overlap filtering: mark SKIPPED_OVERLAP where a prior trade is
    #      still open at this signal's entry time (per exit model) ----
    filtered_outcomes: Dict[ExitModel, List[Tuple["logic.TradeSignal", TradeOutcome, Optional[float], Optional[datetime], Optional[int]]]] = {
        m: [] for m in ALL_EXIT_MODELS
    }
    for sig, raw_outcomes in raw_results:
        for exit_model in ALL_EXIT_MODELS:
            if not config.allow_overlapping_trades:
                still_open_blocking = (open_until[exit_model] is not None and
                                        sig.entry_candle.timestamp < open_until[exit_model])
                if still_open_blocking:
                    filtered_outcomes[exit_model].append((sig, TradeOutcome.SKIPPED_OVERLAP, None, None, None))
                    continue

            outcome, exit_price, exit_time, bars_held = raw_outcomes[exit_model]
            filtered_outcomes[exit_model].append((sig, outcome, exit_price, exit_time, bars_held))

            if not config.allow_overlapping_trades and exit_time is not None:
                open_until[exit_model] = exit_time

    return filtered_outcomes, ignored_signals


def settle_position_sizing(
    per_tf_filtered: List[Tuple[str, Dict[ExitModel, List[Tuple]]]],
    config: BacktestConfig,
) -> List[SimulatedTrade]:
    """
    Apply position sizing + P&L on ONE shared account across all timeframes.

    Each timeframe still applies its own overlap filter (a 3m trade does not
    block a 1h trade). Equity and compounding then run in true chronological
    order across every taken trade so PERCENT_OF_EQUITY cannot compound the
    same starting capital once per timeframe.
    """
    per_model_rows: Dict[ExitModel, List[Tuple]] = {m: [] for m in ALL_EXIT_MODELS}
    for tf_folder, filtered_outcomes in per_tf_filtered:
        for exit_model in ALL_EXIT_MODELS:
            for sig, outcome, exit_price, exit_time, bars_held in filtered_outcomes[exit_model]:
                per_model_rows[exit_model].append(
                    (tf_folder, sig, outcome, exit_price, exit_time, bars_held)
                )

    merged: Dict[int, SimulatedTrade] = {}

    for exit_model in ALL_EXIT_MODELS:
        rows = per_model_rows[exit_model]
        events = []
        for i, (tf_folder, sig, outcome, exit_price, exit_time, bars_held) in enumerate(rows):
            events.append((sig.entry_candle.timestamp, 0, i, "entry", tf_folder or ""))
            if outcome in (TradeOutcome.WIN, TradeOutcome.LOSS) and exit_time is not None:
                events.append((exit_time, 1, i, "exit", tf_folder or ""))
        events.sort(key=lambda e: (e[0], e[1], e[4], e[2]))

        equity = config.starting_capital
        sized: Dict[int, Tuple[float, float]] = {}

        for _, _, i, kind, _tf in events:
            tf_folder, sig, outcome, exit_price, exit_time, bars_held = rows[i]

            if kind == "entry":
                if outcome == TradeOutcome.SKIPPED_OVERLAP:
                    sized[i] = (0.0, 0.0)
                    continue
                sized[i] = calculate_position_size(sig, config, equity)
            else:
                pos_size, _risk_usd = sized.get(i, (0.0, 0.0))
                equity += calculate_pnl(sig, outcome, exit_price, pos_size, config)

        equity_replay = config.starting_capital
        equity_after_by_idx: Dict[int, float] = {}
        for _, _, i, kind, _tf in events:
            if kind == "exit":
                tf_folder, sig, outcome, exit_price, exit_time, bars_held = rows[i]
                pos_size, _risk_usd = sized.get(i, (0.0, 0.0))
                equity_replay += calculate_pnl(sig, outcome, exit_price, pos_size, config)
                equity_after_by_idx[i] = equity_replay

        for i, (tf_folder, sig, outcome, exit_price, exit_time, bars_held) in enumerate(rows):
            pos_size, risk_usd = sized.get(i, (0.0, 0.0))
            trade_pnl = (
                calculate_pnl(sig, outcome, exit_price, pos_size, config)
                if outcome in (TradeOutcome.WIN, TradeOutcome.LOSS) else 0.0
            )
            eq_after = equity_after_by_idx.get(i, equity_replay)
            key = id(sig)
            if key not in merged:
                merged[key] = SimulatedTrade(
                    signal=sig,
                    timeframe_folder=tf_folder,
                    entry_time=sig.entry_candle.timestamp,
                    outcomes={},
                    position_size={},
                    risk_usd={},
                    pnl={},
                    equity_after={},
                )
            merged[key].outcomes[exit_model] = (outcome, exit_price, exit_time, bars_held)
            merged[key].position_size[exit_model] = pos_size
            merged[key].risk_usd[exit_model] = risk_usd
            merged[key].pnl[exit_model] = trade_pnl
            merged[key].equity_after[exit_model] = eq_after

    return sorted(merged.values(), key=lambda t: (t.entry_time, t.timeframe_folder))


def backtest_single_timeframe(
    timeframe_folder: str,
    config: BacktestConfig,
) -> Tuple[List[SimulatedTrade], List["logic.TradeSignal"]]:
    """
    Runs the full pipeline for ONE timeframe:
        load candles -> signals -> forward exits -> size on that TF alone.
    Prefer run_full_backtest() so sizing uses one shared account.
    """
    filtered, ignored = simulate_timeframe_outcomes(timeframe_folder, config)
    ledger = settle_position_sizing([(timeframe_folder, filtered)], config)
    return ledger, ignored


def run_full_backtest(config: BacktestConfig) -> Tuple[List[SimulatedTrade], List["logic.TradeSignal"]]:
    """Run every timeframe, then size all trades on one shared account."""
    sizing_err = validate_position_sizing_config(config)
    if sizing_err:
        raise ValueError(sizing_err)

    per_tf_filtered: List[Tuple[str, Dict[ExitModel, List[Tuple]]]] = []
    full_ignored: List["logic.TradeSignal"] = []

    for tf_folder in config.timeframes_to_test:
        print(f"\n--- Backtesting {config.symbol} [{tf_folder}] ---")
        filtered, ignored = simulate_timeframe_outcomes(tf_folder, config)
        worst = filtered[ExitModel.WORST_CASE]
        skipped_count = sum(1 for row in worst if row[1] == TradeOutcome.SKIPPED_OVERLAP)
        open_count = sum(1 for row in worst if row[1] == TradeOutcome.STILL_OPEN)
        win_loss = sum(
            1 for row in worst
            if row[1] in (TradeOutcome.WIN, TradeOutcome.LOSS)
        )
        print(f"    Signals: {len(worst)} | Ignored (by logic.py): {len(ignored)} | "
              f"Skipped (overlap, worst_case view): {skipped_count} | "
              f"Still open (worst_case view): {open_count} | "
              f"Results Total Trades (WIN+LOSS): {win_loss}")
        per_tf_filtered.append((tf_folder, filtered))
        full_ignored.extend(ignored)

    ledger = settle_position_sizing(per_tf_filtered, config)
    return ledger, full_ignored


# ============================================================================
# SECTION 7: LEDGER -> POLARS DATAFRAME (bridge into vectorized metrics)
# ============================================================================

def ledger_to_polars(
    trades: List[SimulatedTrade],
    *,
    session_clock: str = "broker",
    broker_utc_offset_hours: Optional[float] = None,
) -> pl.DataFrame:
    """
    Flattens the SimulatedTrade list into a long-format Polars DataFrame
    with ONE ROW PER (trade, exit_model) -- this shape is what makes all
    downstream metrics computable with a single group_by().agg() call
    per breakdown, instead of separate Python loops per exit model.
    """
    if not trades:
        return pl.DataFrame(schema={
            "timeframe": pl.Utf8, "direction": pl.Utf8, "hammer_color": pl.Utf8,
            "pattern_variant": pl.Utf8,
            "entry_time": pl.Datetime, "entry_time_ist": pl.Datetime,
            "entry_price": pl.Float64,
            "stop_loss": pl.Float64, "target": pl.Float64,
            "risk_price_distance": pl.Float64, "rr_multiple_target": pl.Float64,
            "position_size": pl.Float64, "risk_usd": pl.Float64,
            "exit_model": pl.Utf8, "outcome": pl.Utf8,
            "exit_price": pl.Float64, "exit_time": pl.Datetime, "exit_time_ist": pl.Datetime,
            "bars_held": pl.Int64, "pnl_usd": pl.Float64, "equity_after": pl.Float64,
            "year": pl.Int32, "month_key": pl.Utf8, "session": pl.Utf8,
            "session_clock": pl.Utf8,
        })

    rows = []
    for t in trades:
        sig = t.signal
        entry_ist = sessions.broker_to_ist(t.entry_time, broker_utc_offset_hours)
        base = {
            "timeframe": t.timeframe_folder,
            "direction": sig.direction.value,
            "hammer_color": "GREEN" if sig.hammer_candle.is_green else (
                "RED" if sig.hammer_candle.is_red else "DOJI"),
            "pattern_variant": getattr(sig, "pattern_variant", None) or "",
            "entry_time": t.entry_time,
            "entry_time_ist": entry_ist,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "target": sig.target,
            "risk_price_distance": sig.risk,
            "rr_multiple_target": sig.rr_multiple,
            "year": t.entry_time.year,
            "month_key": f"{t.entry_time.year:04d}-{t.entry_time.month:02d}",
            "session_clock": session_clock,
        }
        for exit_model in ALL_EXIT_MODELS:
            outcome, exit_price, exit_time, bars_held = t.outcomes[exit_model]
            pos_size = t.position_size.get(exit_model, 0.0) if isinstance(t.position_size, dict) else t.position_size
            risk_usd = t.risk_usd.get(exit_model, 0.0) if isinstance(t.risk_usd, dict) else t.risk_usd
            row = dict(base)
            row["position_size"] = pos_size
            row["risk_usd"] = risk_usd
            row["exit_model"] = exit_model.value
            row["outcome"] = outcome.value
            row["exit_price"] = exit_price
            row["exit_time"] = exit_time
            row["exit_time_ist"] = sessions.broker_to_ist(exit_time, broker_utc_offset_hours)
            row["bars_held"] = bars_held
            row["pnl_usd"] = t.pnl[exit_model]
            row["equity_after"] = t.equity_after[exit_model]
            rows.append(row)

    df = pl.DataFrame(rows)
    return sessions.add_session_column(
        df,
        clock=session_clock,
        broker_utc_offset_hours=broker_utc_offset_hours,
    )


# ============================================================================
# SECTION 8: VECTORIZED METRICS ENGINE (Polars group_by + agg expressions)
# ============================================================================

def _add_helper_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Adds boolean/derived columns used repeatedly by the metrics
    expressions below, computed once as vectorized Polars expressions."""
    return df.with_columns([
        (pl.col("outcome") == "WIN").alias("is_win"),
        (pl.col("outcome") == "LOSS").alias("is_loss"),
        (pl.col("outcome") == "STILL_OPEN").alias("is_still_open"),
        (pl.col("outcome") == "SKIPPED_OVERLAP").alias("is_skipped"),
        pl.when(pl.col("outcome") == "WIN").then(pl.col("pnl_usd")).otherwise(None).alias("win_pnl"),
        pl.when(pl.col("outcome") == "LOSS").then(pl.col("pnl_usd")).otherwise(None).alias("loss_pnl"),
        pl.when(pl.col("outcome") == "WIN").then(pl.col("bars_held")).otherwise(None).alias("win_bars"),
        pl.when(pl.col("outcome") == "LOSS").then(pl.col("bars_held")).otherwise(None).alias("loss_bars"),
        pl.when(pl.col("risk_usd") > 0)
          .then(pl.col("pnl_usd") / pl.col("risk_usd"))
          .otherwise(None).alias("r_multiple"),
    ])


def _streaks_and_drawdown(sub: pl.DataFrame, starting_capital: float) -> Dict[str, float]:
    """
    Streaks and drawdown are inherently sequential (each step depends on
    the running state from the previous one), so unlike the other metrics
    they can't be a single stateless Polars expression. This function
    still uses vectorized NumPy array ops (not a manual Python loop with
    per-element branching) to compute both in one pass.
    """
    if sub.height == 0:
        return {
            "max_drawdown_usd": 0.0, "max_drawdown_pct": 0.0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "ending_capital": starting_capital,
        }

    ordered = sub.sort("entry_time")
    outcomes = ordered["outcome"].to_numpy()
    pnls = ordered["pnl_usd"].to_numpy()

    # keep only resolved trades (WIN/LOSS) for equity curve + drawdown
    resolved_mask = np.isin(outcomes, ["WIN", "LOSS"])
    resolved_pnls = pnls[resolved_mask]

    equity_curve = np.concatenate(([starting_capital], starting_capital + np.cumsum(resolved_pnls)))
    running_peak = np.maximum.accumulate(equity_curve)
    drawdown_usd = running_peak - equity_curve
    drawdown_pct = np.where(running_peak > 0, drawdown_usd / running_peak * 100.0, 0.0)

    max_dd_usd = float(drawdown_usd.max()) if len(drawdown_usd) else 0.0
    max_dd_pct = float(drawdown_pct.max()) if len(drawdown_pct) else 0.0
    ending_capital = float(equity_curve[-1]) if len(equity_curve) else starting_capital

    # streaks via vectorized run-length encoding on the resolved outcomes only
    resolved_outcomes = outcomes[resolved_mask]
    is_win = (resolved_outcomes == "WIN").astype(np.int8)
    is_loss = (resolved_outcomes == "LOSS").astype(np.int8)

    def _max_streak(binary_arr: np.ndarray) -> int:
        if binary_arr.size == 0:
            return 0
        # run-length via diff on a padded array (fully vectorized)
        padded = np.concatenate(([0], binary_arr, [0]))
        diffs = np.diff(padded)
        run_starts = np.where(diffs == 1)[0]
        run_ends = np.where(diffs == -1)[0]
        if run_starts.size == 0:
            return 0
        return int((run_ends - run_starts).max())

    return {
        "max_drawdown_usd": max_dd_usd,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_wins": _max_streak(is_win),
        "max_consecutive_losses": _max_streak(is_loss),
        "ending_capital": ending_capital,
    }


def compute_metrics_grouped(
    df: pl.DataFrame,
    group_cols: List[str],
    starting_capital: float,
    total_signals_by_group: Optional[Dict] = None,
) -> pl.DataFrame:
    """
    Computes the FULL metrics table for every (exit_model, *group_cols)
    combination using Polars group_by().agg() -- a single vectorized
    aggregation pass instead of looping through groups in Python.

    group_cols examples: [] for overall, ["year"], ["month_key"],
    ["timeframe"], ["direction"].

    Streaks/drawdown are appended afterward per-group (see
    _streaks_and_drawdown) since those are sequential by nature.
    """
    if df.height == 0:
        return pl.DataFrame()

    df = _add_helper_columns(df)
    keys = ["exit_model"] + group_cols

    agg_df = df.group_by(keys).agg([
        pl.len().alias("total_rows"),
        pl.col("is_win").sum().alias("wins"),
        pl.col("is_loss").sum().alias("losses"),
        pl.col("is_still_open").sum().alias("still_open"),
        pl.col("is_skipped").sum().alias("skipped_overlap"),
        pl.col("win_pnl").sum().alias("gross_profit"),
        pl.col("loss_pnl").sum().alias("gross_loss"),
        pl.col("win_pnl").max().alias("largest_win_usd"),
        pl.col("loss_pnl").min().alias("largest_loss_usd"),
        pl.col("win_bars").mean().alias("avg_bars_held_win"),
        pl.col("loss_bars").mean().alias("avg_bars_held_loss"),
        pl.col("r_multiple").filter(pl.col("outcome").is_in(["WIN", "LOSS"])).mean().alias("avg_rr_achieved"),
        pl.when(pl.col("outcome").is_in(["WIN", "LOSS"]))
          .then(pl.col("pnl_usd")).otherwise(None).mean().alias("avg_trade_usd"),
        pl.when(pl.col("outcome").is_in(["WIN", "LOSS"]))
          .then(pl.col("pnl_usd")).otherwise(None).std().alias("pnl_std"),
        pl.when(pl.col("outcome") == "LOSS")
          .then(pl.col("pnl_usd")).otherwise(None).std().alias("downside_std"),
    ])

    agg_df = agg_df.with_columns([
        (pl.col("wins") + pl.col("losses")).alias("total_trades"),
        pl.col("gross_profit").fill_null(0.0),
        pl.col("gross_loss").fill_null(0.0),
    ])

    agg_df = agg_df.with_columns([
        pl.when(pl.col("wins") > 0)
          .then(pl.col("gross_profit") / pl.col("wins"))
          .otherwise(None).alias("avg_win_usd"),
        pl.when(pl.col("losses") > 0)
          .then(pl.col("gross_loss") / pl.col("losses"))
          .otherwise(None).alias("avg_loss_usd"),
    ])

    agg_df = agg_df.with_columns([
        (pl.col("gross_profit") + pl.col("gross_loss")).alias("net_pnl"),
        pl.when(pl.col("total_trades") > 0)
          .then(pl.col("wins") / pl.col("total_trades") * 100.0)
          .otherwise(0.0).alias("win_rate_pct"),
        pl.when(pl.col("gross_loss") != 0)
          .then(pl.col("gross_profit") / pl.col("gross_loss").abs())
          .otherwise(None).alias("profit_factor"),
        pl.when(pl.col("avg_loss_usd") != 0)
          .then(pl.col("avg_win_usd") / pl.col("avg_loss_usd").abs())
          .otherwise(None).alias("payoff_ratio"),
    ])

    agg_df = agg_df.with_columns([
        pl.col("avg_trade_usd").fill_null(0.0).alias("expectancy_usd"),
        # ---- GUARD against near-zero std blowing up Sharpe/Sortino ----
        # With FIXED_RISK_USD or FIXED_UNITS sizing, every losing trade
        # can be numerically identical (e.g. always exactly -100.0), so
        # the true standard deviation is legitimately 0 -- but floating
        # point arithmetic over large trade counts leaves tiny non-zero
        # noise (~1e-15) instead of an exact 0. Dividing by that noise
        # produces meaningless numbers in the quadrillions. A relative
        # epsilon (scaled to the actual size of avg_trade_usd, not a
        # fixed absolute constant) correctly treats "effectively zero
        # variance" as "ratio undefined" and reports null instead of a
        # nonsense figure -- the mathematically honest outcome, since
        # Sharpe/Sortino are genuinely undefined when volatility is zero.
        pl.when(pl.col("pnl_std") > (pl.col("avg_trade_usd").abs().clip(lower_bound=1.0) * 1e-9))
          .then(pl.col("avg_trade_usd") / pl.col("pnl_std"))
          .otherwise(None).alias("sharpe_ratio"),
        pl.when(pl.col("downside_std") > (pl.col("avg_trade_usd").abs().clip(lower_bound=1.0) * 1e-9))
          .then(pl.col("avg_trade_usd") / pl.col("downside_std"))
          .otherwise(None).alias("sortino_ratio"),
    ])

    # ---- sequential metrics: drawdown + streaks, appended per group ----
    extra_rows = []
    key_combos = agg_df.select(keys).unique().rows(named=True)
    for combo in key_combos:
        filt = df
        for k, v in combo.items():
            filt = filt.filter(pl.col(k) == v)
        stats = _streaks_and_drawdown(filt, starting_capital)
        row = dict(combo)
        row.update(stats)
        extra_rows.append(row)

    extra_df = pl.DataFrame(extra_rows)
    result = agg_df.join(extra_df, on=keys, how="left")

    result = result.with_columns([
        pl.lit(starting_capital).alias("starting_capital"),
        pl.when(starting_capital > 0)
          .then((pl.col("ending_capital") - starting_capital) / starting_capital * 100.0)
          .otherwise(0.0).alias("total_return_pct"),
    ])

    if total_signals_by_group is not None:
        sig_counts = []
        for combo in key_combos:
            group_key = tuple(combo.get(c) for c in group_cols) if group_cols else ()
            sig_counts.append({**combo, "total_signals": total_signals_by_group.get(group_key, combo.get("total_trades", 0) + combo.get("skipped_overlap", 0) + combo.get("still_open", 0))})
        sig_df = pl.DataFrame(sig_counts)
        result = result.join(sig_df, on=keys, how="left")
    else:
        result = result.with_columns(
            (pl.col("total_trades") + pl.col("skipped_overlap") + pl.col("still_open")).alias("total_signals")
        )

    group_label_expr = pl.lit("ALL") if not group_cols else pl.concat_str(
        [pl.col(c).cast(pl.Utf8) for c in group_cols], separator="_"
    )
    result = result.with_columns(group_label_expr.alias("group"))

    final_cols = [
        "group", "exit_model", "total_signals", "total_trades", "skipped_overlap",
        "still_open", "wins", "losses", "win_rate_pct", "gross_profit", "gross_loss",
        "net_pnl", "avg_win_usd", "avg_loss_usd", "avg_trade_usd", "largest_win_usd",
        "largest_loss_usd", "profit_factor", "expectancy_usd", "payoff_ratio",
        "avg_rr_achieved", "avg_bars_held_win", "avg_bars_held_loss",
        "max_drawdown_usd", "max_drawdown_pct", "max_consecutive_wins",
        "max_consecutive_losses", "sharpe_ratio", "sortino_ratio",
        "starting_capital", "ending_capital", "total_return_pct",
    ]
    for c in final_cols:
        if c not in result.columns:
            result = result.with_columns(pl.lit(None).alias(c))

    result = result.select(final_cols).sort(["exit_model", "group"])
    if group_cols == ["timeframe"] and "group" in result.columns:
        mapping = {
            "1min": "1m", "3min": "3m", "5min": "5m", "10min": "10m",
            "15min": "15m", "30min": "30m", "1hour": "1h",
        }
        result = result.with_columns(
            pl.col("group").replace(mapping).alias("group")
        )
    if group_cols == ["session"] and "group" in result.columns:
        order = {name: i for i, name in enumerate(sessions.SESSION_ORDER)}
        result = result.with_columns(
            pl.col("group").replace(order).cast(pl.Int32).alias("_session_ord")
        ).sort(["exit_model", "_session_ord"]).drop("_session_ord")
    return result


# ============================================================================
# SECTION 9: EXPORT HELPERS
# ============================================================================

def export_trade_ledger(df: pl.DataFrame, filepath: str) -> None:
    df.write_csv(filepath)


def export_ignored_signals(ignored: List["logic.TradeSignal"], filepath: str) -> None:
    if not ignored:
        pl.DataFrame(schema={
            "timeframe": pl.Utf8, "direction": pl.Utf8, "hammer_color": pl.Utf8,
            "pattern_variant": pl.Utf8,
            "hammer_time": pl.Utf8, "ignore_reason": pl.Utf8,
        }).write_csv(filepath)
        return

    rows = [{
        "timeframe": sig.timeframe,
        "direction": sig.direction.value if sig.direction else "",
        "hammer_color": "GREEN" if sig.hammer_candle.is_green else (
            "RED" if sig.hammer_candle.is_red else "DOJI"),
        "pattern_variant": getattr(sig, "pattern_variant", None) or "",
        "hammer_time": str(sig.hammer_candle.timestamp),
        "ignore_reason": sig.ignore_reason or "",
    } for sig in ignored]
    pl.DataFrame(rows).write_csv(filepath)


def export_skipped_and_open(df: pl.DataFrame, out_dir: str) -> None:
    """
    Exports every SKIPPED_OVERLAP row and every STILL_OPEN row separately,
    per exit model, so nothing that got excluded from the win/loss
    metrics is silently lost -- you can inspect exactly what was skipped
    and why.
    """
    if df.height == 0:
        pl.DataFrame().write_csv(os.path.join(out_dir, "skipped_overlap.csv"))
        pl.DataFrame().write_csv(os.path.join(out_dir, "still_open.csv"))
        return

    skipped = df.filter(pl.col("outcome") == "SKIPPED_OVERLAP")
    still_open = df.filter(pl.col("outcome") == "STILL_OPEN")
    skipped.write_csv(os.path.join(out_dir, "skipped_overlap.csv"))
    still_open.write_csv(os.path.join(out_dir, "still_open.csv"))


# ============================================================================
# SECTION 10: TOP-LEVEL RUNNER
# ============================================================================

def run_backtest_and_export(config: BacktestConfig) -> Dict[str, pl.DataFrame]:
    """
    Full pipeline: run_full_backtest() -> ledger_to_polars() ->
    compute_metrics_grouped() for every breakdown -> export everything
    to CSV under output/<run_name>/.
    Returns the metrics tables as Polars DataFrames.

    When market_data is BOTH, runs spot and futures separately (whichever
    exist), tags each trade with a market column, and merges results.
    """
    print("=" * 70)
    print(f"BACKTEST: {config.symbol} | Pattern: {config.pattern_type} | "
          f"Timeframes: {config.timeframes_to_test}")
    market_setting = getattr(config, "market_data", MarketDataSource.SPOT)
    market_label = (
        market_setting.value if isinstance(market_setting, MarketDataSource)
        else str(market_setting)
    )
    print(f"Market data: {market_label}")
    if hammer_context_logic.is_context_pattern_type(config.pattern_type):
        try:
            print(hammer_context_logic.describe_hammer_context_rules(config.strategy_config))
            print(hammer_context_logic.describe_hammer_context_entry_exit(config.strategy_config))
        except Exception as e:
            print(f"[WARN] Could not print Hammer-with-candles rules: {e}")
    print(f"Overlap allowed: {config.allow_overlapping_trades} | "
          f"Sizing: {_sizing_mode(config).value}")
    print(sessions.describe_sessions(
        clock=getattr(config, "session_clock", "broker"),
        broker_utc_offset_hours=getattr(config, "broker_utc_offset_hours", None),
    ))
    if getattr(config, "ist_time_filter_enabled", False):
        print(
            f"IST time filter: {config.ist_time_start}–{config.ist_time_end} IST "
            f"(IC Markets → IST auto GMT+2/GMT+3 by bar date)"
        )
    print("=" * 70)

    targets = expand_market_targets(
        config.data_root, config.symbol, market_setting,
    )
    if not targets:
        # Fall back to whatever resolve returns so empty-run messaging still works
        eff_root, eff_sym = resolve_symbol_data_root(
            config.data_root, config.symbol, preferred=market_setting,
        )
        targets = [(market_label if market_label != "both" else "spot", eff_root, eff_sym)]

    frames: List[pl.DataFrame] = []
    all_ignored: List = []
    for label, eff_root, eff_sym in targets:
        print(f"\n--- Market: {label} → {eff_root}/{eff_sym} ---")
        sub = replace(
            config,
            data_root=eff_root,
            symbol=eff_sym,
            market_data=(
                MarketDataSource.FUTURES if label == "futures"
                else MarketDataSource.SPOT
            ),
        )
        ledger, ignored = run_full_backtest(sub)
        all_ignored.extend(ignored)
        part = ledger_to_polars(
            ledger,
            session_clock=getattr(config, "session_clock", "broker"),
            broker_utc_offset_hours=getattr(config, "broker_utc_offset_hours", None),
        )
        if part.height == 0:
            print(f"[WARN] No trades for market={label}.")
            continue
        part = part.with_columns(pl.lit(label).alias("market"))
        frames.append(part)

    if frames:
        df = pl.concat(frames, how="diagonal_relaxed")
    else:
        df = ledger_to_polars(
            [],
            session_clock=getattr(config, "session_clock", "broker"),
            broker_utc_offset_hours=getattr(config, "broker_utc_offset_hours", None),
        )
        if "market" not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("market"))

    if df.height == 0:
        print("\n[WARN] No trades were generated. Check your data folder, "
              "symbol name, and date range.")

    out_dir = os.path.join(config.output_root, config.run_name)
    os.makedirs(out_dir, exist_ok=True)

    export_trade_ledger(df, os.path.join(out_dir, "trade_ledger.csv"))
    export_ignored_signals(all_ignored, os.path.join(out_dir, "ignored_signals.csv"))
    export_skipped_and_open(df, out_dir)

    tables: Dict[str, pl.DataFrame] = {
        "overall": compute_metrics_grouped(df, [], config.starting_capital),
        "by_year": compute_metrics_grouped(df, ["year"], config.starting_capital),
        "by_month": compute_metrics_grouped(df, ["month_key"], config.starting_capital),
        "by_timeframe": compute_metrics_grouped(df, ["timeframe"], config.starting_capital),
        "by_direction": compute_metrics_grouped(df, ["direction"], config.starting_capital),
        "by_session": compute_metrics_grouped(df, ["session"], config.starting_capital),
    }
    if "market" in df.columns and df.height and df["market"].n_unique() > 1:
        tables["by_market"] = compute_metrics_grouped(
            df, ["market"], config.starting_capital,
        )

    for name, table in tables.items():
        table.write_csv(os.path.join(out_dir, f"summary_{name}.csv"))

    print(f"\n[OK] Exported to: {out_dir}/")
    print("    trade_ledger.csv, ignored_signals.csv, skipped_overlap.csv, still_open.csv,")
    print("    summary_overall.csv, summary_by_year.csv, summary_by_month.csv,")
    print("    summary_by_timeframe.csv, summary_by_direction.csv, summary_by_session.csv")
    if "by_market" in tables:
        print("    summary_by_market.csv")

    if tables["overall"].height:
        print("\n--- QUICK OVERALL SUMMARY (all 3 exit models) ---")
        for row in tables["overall"].rows(named=True):
            pf = f"{row['profit_factor']:.2f}" if row["profit_factor"] is not None else "N/A"
            print(f"  [{row['exit_model']:12}] Trades={row['total_trades']:>5} | "
                  f"WinRate={row['win_rate_pct']:5.1f}% | NetPnL=${row['net_pnl']:>10.2f} | "
                  f"PF={pf:>5} | MaxDD=${row['max_drawdown_usd']:>9.2f} | "
                  f"Return={row['total_return_pct']:6.2f}%")

        sess = tables.get("by_session")
        if sess is not None and sess.height:
            print(f"\n--- BY SESSION ({sessions.DATA_SOURCE_NOTE}) ---")
            wc = sess.filter(pl.col("exit_model") == "worst_case").sort("group")
            for row in wc.iter_rows(named=True):
                pf = f"{row['profit_factor']:.2f}" if row.get("profit_factor") is not None else "N/A"
                print(
                    f"  [{row['group']:6}] Trades={row['total_trades']:>5} | "
                    f"WinRate={row['win_rate_pct']:5.1f}% | NetPnL=${row['net_pnl']:>10.2f} | PF={pf}"
                )

        by_m = tables.get("by_market")
        if by_m is not None and by_m.height:
            print("\n--- BY MARKET ---")
            wc = by_m.filter(pl.col("exit_model") == "worst_case").sort("group")
            for row in wc.iter_rows(named=True):
                pf = f"{row['profit_factor']:.2f}" if row.get("profit_factor") is not None else "N/A"
                print(
                    f"  [{row['group']:8}] Trades={row['total_trades']:>5} | "
                    f"WinRate={row['win_rate_pct']:5.1f}% | NetPnL=${row['net_pnl']:>10.2f} | PF={pf}"
                )

    tables["ledger"] = df
    return tables


# ============================================================================
# SECTION 11: DEMO / EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # ------------------------------------------------------------------
    # RUN 1: no overlap, fixed $ risk per trade
    # ------------------------------------------------------------------
    config_no_overlap = BacktestConfig(
        symbol="XAUUSD",
        timeframes_to_test=["3min", "5min", "1hour"],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
        allow_overlapping_trades=False,
        run_name="run_no_overlap",
    )
    run_backtest_and_export(config_no_overlap)

    print("\n\n")

    # ------------------------------------------------------------------
    # RUN 2: overlapping trades allowed, percent-of-equity sizing
    #        (compounding) -- compare against Run 1
    # ------------------------------------------------------------------
    # NOTE: PERCENT_OF_EQUITY + allow_overlapping_trades=True is the
    # highest-trade-count, highest-compounding combination. With very
    # large trade counts (tens of thousands, realistic on 3m/5m data
    # across years), any sustained edge compounds into very large numbers
    # very quickly -- this is correct math, not unrealistic simulation
    # bugs, but it stops being a meaningful "account projection" past a
    # certain point. max_equity_multiple (default 50x starting_capital)
    # keeps this bounded and readable. Raise/lower it in BacktestConfig
    # if you want a different ceiling, or switch to FIXED_RISK_USD for a
    # sizing mode that's immune to compounding blowup entirely.
    config_overlap = BacktestConfig(
        symbol="XAUUSD",
        timeframes_to_test=["3min", "5min", "1hour"],
        position_sizing_mode=PositionSizingMode.PERCENT_OF_EQUITY,
        risk_pct_of_equity=1.0,
        starting_capital=10000.0,
        max_equity_multiple=50.0,
        allow_overlapping_trades=True,
        run_name="run_with_overlap",
    )
    run_backtest_and_export(config_overlap)