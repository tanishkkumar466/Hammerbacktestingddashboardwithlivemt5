"""
fetcher.py -- uploaded version, saved here for review/testing
"""

import os
import sys
import time
import argparse
import random
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

import csv

try:
    from broker import ensure_mt5_symbol_visible
except ImportError:
    ensure_mt5_symbol_visible = None  # type: ignore


@contextmanager
def _hold_mt5_lock(lock):
    """Serialize MT5 API calls when sharing a Live broker connection."""
    if lock is None:
        yield
    else:
        with lock:
            yield


# ============================================================================
# SECTION 1: CONFIG
# ============================================================================

SYMBOLS: List[str] = [
    "XAUUSD",
]

TIMEFRAMES: Dict[str, str] = {
    "1min":  "TIMEFRAME_M1",
    "3min":  "TIMEFRAME_M3",
    "5min":  "TIMEFRAME_M5",
    "10min": "TIMEFRAME_M10",
    "15min": "TIMEFRAME_M15",
    "30min": "TIMEFRAME_M30",
    "1hour": "TIMEFRAME_H1",
}

START_DATE = datetime(2005, 1, 1, tzinfo=timezone.utc)
END_DATE = datetime.now(timezone.utc)

OUTPUT_ROOT = "data"

CHUNK_BY = "month"

# Spot vs futures live in separate subfolders under the data root:
#   data/spot/<symbol>/…
#   data/futures/<symbol>/…
# Legacy layout data/<symbol>/… is treated as spot when updating.
MARKET_SPOT = "spot"
MARKET_FUTURES = "futures"
MARKET_TYPES = (MARKET_SPOT, MARKET_FUTURES)

# ---- retry settings for MT5 requests (server hiccups happen over a
#      20-year pull, so failed chunks get retried instead of silently
#      skipped) ----
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# ---- data quality check settings ----
# A "gap" is flagged if the time between two consecutive candles is
# bigger than expected_gap_multiplier x the timeframe's normal spacing.
# Weekend closures (market shut Fri evening -> Sun evening) are excluded
# automatically so they don't get flagged as errors.
EXPECTED_GAP_MULTIPLIER = 2.0

TIMEFRAME_MINUTES: Dict[str, int] = {
    "1min": 1, "3min": 3, "5min": 5, "10min": 10, "15min": 15, "30min": 30, "1hour": 60,
}


# ============================================================================
# SECTION 2: FOLDER / FILE STRUCTURE HELPERS
# ============================================================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _root(output_root: Optional[str] = None) -> str:
    return (output_root or OUTPUT_ROOT).strip() or OUTPUT_ROOT


def normalize_market_type(market_type: Optional[str]) -> str:
    mt = (market_type or MARKET_SPOT).strip().lower()
    if mt in ("future", "fut", "fwd", "forward"):
        mt = MARKET_FUTURES
    if mt not in MARKET_TYPES:
        return MARKET_SPOT
    return mt


def market_data_root(
    output_root: Optional[str] = None,
    market_type: Optional[str] = None,
) -> str:
    """
    Absolute/relative path where symbol folders live for this market type.
    If output_root already ends with spot/ or futures/, it is adjusted to
    the requested market type instead of nesting again.
    """
    root = os.path.normpath(_root(output_root))
    mt = normalize_market_type(market_type)
    base = os.path.basename(root).lower()
    if base in MARKET_TYPES:
        parent = os.path.dirname(root) or "."
        return os.path.normpath(os.path.join(parent, mt))
    return os.path.normpath(os.path.join(root, mt))


def classify_market_type(
    symbol: str,
    mt5_info: Optional[object] = None,
) -> str:
    """
    Guess spot vs futures from MT5 symbol_info (when available) or the name.

    Returns MARKET_SPOT or MARKET_FUTURES. Prefer explicit MT5 fields; fall
    back to path/description keywords and symbol-name heuristics.
    """
    sym = (symbol or "").strip()
    # --- MT5 symbol_info hints ---
    if mt5_info is not None:
        try:
            exp = int(getattr(mt5_info, "expiration_time", 0) or 0)
            if exp > 0:
                return MARKET_FUTURES
        except (TypeError, ValueError):
            pass
        path = str(getattr(mt5_info, "path", "") or "").lower()
        desc = str(getattr(mt5_info, "description", "") or "").lower()
        blob = f"{path} {desc} {sym.lower()}"
        fut_keys = (
            "future", "futures", "fwd", "forward", "comex", "cme", "nymex",
            "expiry", "expiration", "contango",
        )
        spot_keys = ("spot", "forex", "fx ", "cfd", "metal", "metals", "otc")
        if any(k in blob for k in fut_keys):
            return MARKET_FUTURES
        if any(k in blob for k in spot_keys):
            return MARKET_SPOT

    # --- name heuristics (offline / no MT5) ---
    s = sym.upper().replace(" ", "")
    if not s:
        return MARKET_SPOT
    # Contract-month style: XAUz25, GCZ5, CLH6, XAUUSD-JUN25
    if re.search(r"(20\d{2}|[FGHJKMNQUVXZ]\d{1,2})$", s):
        # Avoid treating plain XAUUSD / EURUSD as futures
        if re.search(r"[FGHJKMNQUVXZ]\d{1,2}$", s) or re.search(r"20\d{2}$", s):
            if not re.fullmatch(r"[A-Z]{6}", s):  # 6-letter FX pairs stay spot
                return MARKET_FUTURES
    if any(tok in s for tok in ("FUT", "FUTURE", "_F", "-F", ".F")):
        return MARKET_FUTURES
    if s.endswith("SPOT") or ".S" in s or s.endswith("_S"):
        return MARKET_SPOT
    return MARKET_SPOT


def detect_market_type_from_mt5(mt5, symbol: str) -> str:
    """Classify using live MT5 symbol_info when connected; else name only."""
    info = None
    try:
        if mt5 is not None and symbol:
            info = mt5.symbol_info(symbol)
    except Exception:
        info = None
    return classify_market_type(symbol, info)


def resolve_write_root(
    output_root: Optional[str],
    symbol: str,
    market_type: Optional[str],
    *,
    prefer_legacy_spot: bool = True,
    flat_under_root: bool = False,
) -> str:
    """
    Folder that should contain <symbol>/<timeframe>/ for this fetch.

    flat_under_root=True (per Live account): write directly under output_root
        data/<account_slug>/<symbol>/…
    without nesting spot/futures.

    Spot + prefer_legacy_spot: if legacy data/<symbol> exists and
    data/spot/<symbol> does not, keep writing into the legacy folder so
    existing client data updates in place. New installs use data/spot/.
    """
    if flat_under_root:
        root = os.path.normpath(_root(output_root))
        # Peel accidental …/spot or …/futures so account roots stay flat
        base = os.path.basename(root).lower()
        if base in MARKET_TYPES:
            root = os.path.dirname(root) or root
        ensure_dir(root)
        return root

    mt = normalize_market_type(market_type)
    preferred = market_data_root(output_root, mt)
    preferred_sym = os.path.join(preferred, symbol)
    if mt == MARKET_SPOT and prefer_legacy_spot:
        legacy = os.path.join(_root(output_root), symbol)
        if os.path.isdir(legacy) and not os.path.isdir(preferred_sym):
            return _root(output_root)
    ensure_dir(preferred)
    return preferred


def month_folder(symbol: str, timeframe_label: str, year: int,
                 output_root: Optional[str] = None) -> str:
    path = os.path.join(_root(output_root), symbol, timeframe_label, str(year))
    ensure_dir(path)
    return path


def monthly_csv_path(symbol: str, timeframe_label: str, year: int, month: int,
                     output_root: Optional[str] = None) -> str:
    folder = month_folder(symbol, timeframe_label, year, output_root=output_root)
    filename = f"{symbol}_{timeframe_label}_{year:04d}-{month:02d}.csv"
    return os.path.join(folder, filename)


def full_history_csv_path(symbol: str, timeframe_label: str,
                          output_root: Optional[str] = None) -> str:
    folder = os.path.join(_root(output_root), symbol, timeframe_label)
    ensure_dir(folder)
    filename = f"{symbol}_{timeframe_label}_FULL.csv"
    return os.path.join(folder, filename)


# ============================================================================
# SECTION 3: DATE-RANGE CHUNKING
# ============================================================================

def iter_month_chunks(start: datetime, end: datetime):
    current = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    while current <= end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        chunk_start = max(current, start)
        chunk_end = min(next_month - timedelta(seconds=1), end)

        yield chunk_start, chunk_end, current.year, current.month
        current = next_month


def iter_year_chunks(start: datetime, end: datetime):
    current = datetime(start.year, 1, 1, tzinfo=timezone.utc)
    while current <= end:
        next_year = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        chunk_start = max(current, start)
        chunk_end = min(next_year - timedelta(seconds=1), end)
        yield chunk_start, chunk_end, current.year
        current = next_year


# ============================================================================
# SECTION 4: DATA SOURCE -- REAL MT5 FETCH
# ============================================================================
#
# IMPORTANT: connect_to_mt5() is called ONCE at the start of the whole run
# (see run_fetcher), and disconnect_from_mt5() ONCE at the end. Individual
# fetch_from_mt5() calls reuse that single open connection instead of
# re-initializing per month/timeframe -- initializing hundreds of times
# over a full historical pull is slow and can make the terminal unstable.

def connect_to_mt5(
    terminal_path: str = "",
    login: int = 0,
    password: str = "",
    server: str = "",
    log=None,
):
    """
    Initializes ONE connection to the local MT5 terminal for this process.

    MetaTrader5's Python API is process-global: a second initialize() can
    switch terminals, and shutdown() kills every user of that connection
    (including Live). Prefer reusing an already-open Live broker module
    via run_fetch_job(mt5_module=..., own_connection=False).

    If terminal_path is set, initialize that terminal; otherwise attach
    to the nearest already-running MT5 instance. Optional login/password/
    server match Live Settings so Fetch and Live hit the same account.
    """
    _log = log or (lambda msg: print(msg))
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise RuntimeError(
            "MetaTrader5 package not found. Install it with:\n"
            "    pip install MetaTrader5\n"
            "and make sure you're running this on Windows with the MT5 "
            "terminal installed and logged in."
        )

    path = (terminal_path or "").strip()
    ok = mt5.initialize(path=path) if path else mt5.initialize()
    if not ok:
        raise RuntimeError(
            f"MT5 initialize() failed, error code: {mt5.last_error()}. "
            f"Make sure the MT5 terminal is open and logged into an account."
        )

    if login:
        authorized = mt5.login(
            login=int(login),
            password=password or "",
            server=(server or "").strip() or None,
        )
        if not authorized:
            err = mt5.last_error()
            try:
                mt5.shutdown()
            except Exception:
                pass
            raise RuntimeError(f"MT5 login failed: {err}")

    account_info = mt5.account_info()
    term = None
    try:
        term = mt5.terminal_info()
    except Exception:
        term = None
    if account_info is not None:
        path_note = ""
        if term is not None and getattr(term, "path", None):
            path_note = f" | Terminal: {term.path}"
        _log(
            f"[OK] Connected to MT5 -- Account: {account_info.login} | "
            f"Server: {account_info.server}{path_note}"
        )
    else:
        _log("[OK] Connected to MT5 (no account info available).")

    return mt5


def disconnect_from_mt5(mt5, *, owned: bool = True, log=None):
    """
    Shut down the MT5 connection only if this caller owns it.

    owned=False: shared with Live (or another owner) — do not shutdown.
    """
    _log = log or (lambda msg: print(msg))
    if not owned:
        _log("[OK] Leaving shared MT5 connection open (Live/other owner).")
        return
    if mt5 is None:
        return
    try:
        mt5.shutdown()
    except Exception:
        pass
    _log("[OK] MT5 connection closed.")


def _safe_print(msg: str) -> None:
    """print() can abort a windowed frozen exe when stdout is None/closed."""
    try:
        out = getattr(sys, "stdout", None)
        if out is None:
            return
        print(msg)
    except OSError:
        pass


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mt5_timestamp(dt: datetime) -> int:
    """Unix seconds for MT5 API (reliable on Windows vs naive/aware datetime)."""
    return int(_as_utc(dt).timestamp())


def _rates_to_candles(rates) -> List[dict]:
    """
    Convert MT5 copy_rates_* output to candle dicts without crashing.

    Native numpy records can carry bad timestamps; Windows fromtimestamp()
    on those values raises OSError and has been seen to abort the process
    if left uncaught around the C extension.
    """
    candles: List[dict] = []
    if rates is None:
        return candles
    try:
        n = len(rates)
    except TypeError:
        return candles
    if n == 0:
        return candles
    for r in rates:
        try:
            ts = int(r["time"])
            if ts <= 0 or ts >= 4_102_444_800:  # year 2100
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            candles.append({
                "datetime": dt,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(float(r["tick_volume"])),
            })
        except (OSError, OverflowError, ValueError, KeyError, TypeError, IndexError):
            continue
    return candles


def _copy_rates_range_safe(mt5, symbol: str, timeframe_const, start: datetime, end: datetime):
    """copy_rates_range can abort the process on some Windows MT5 builds."""
    start_u = _as_utc(start)
    end_u = _as_utc(end)
    if end_u <= start_u:
        return None
    try:
        rates = mt5.copy_rates_range(symbol, timeframe_const, start_u, end_u)
    except Exception:
        try:
            rates = mt5.copy_rates_range(
                symbol, timeframe_const, int(start_u.timestamp()), int(end_u.timestamp()),
            )
        except Exception:
            return None
    return rates


def iter_fetch_subchunks(chunk_start: datetime, chunk_end: datetime, timeframe_label: str):
    """
    Split a month into week windows for small timeframes.

    Requesting a full month of M1/M3 via one copy_rates_range has crashed
    the MetaTrader5 C module (and the whole Hammer exe) on Windows.
    """
    minutes = TIMEFRAME_MINUTES.get(timeframe_label, 60)
    if minutes > 5:
        yield chunk_start, chunk_end
        return
    cur = chunk_start
    step = timedelta(days=7)
    while cur <= chunk_end:
        piece_end = min(cur + step - timedelta(seconds=1), chunk_end)
        if piece_end < cur:
            break
        yield cur, piece_end
        cur = piece_end + timedelta(seconds=1)


def verify_symbol(mt5, symbol: str, log=None, api_lock=None) -> Tuple[bool, str]:
    """
    Confirm symbol exists, resolve broker aliases (XAUUSD → XAUUSDm), enable
    in Market Watch. Returns (ok, resolved_symbol).
    """
    _log = log or (lambda msg: print(msg))
    with _hold_mt5_lock(api_lock):
        if ensure_mt5_symbol_visible is not None:
            ok, resolved_or_msg = ensure_mt5_symbol_visible(mt5, symbol)
            if not ok:
                _log(f"[FAIL] {resolved_or_msg}")
                return False, symbol
            if resolved_or_msg != symbol:
                _log(f"[INFO] Symbol resolved: {symbol} → {resolved_or_msg}")
            else:
                _log(f"[OK] Symbol ready: {resolved_or_msg}")
            return True, resolved_or_msg

        info = mt5.symbol_info(symbol)
        if info is None:
            _log(
                f"[FAIL] Symbol '{symbol}' not found on this broker. "
                f"Check the exact symbol name in MT5's Market Watch panel "
                f"(some brokers use 'XAUUSD.m', 'GOLD', etc.)."
            )
            return False, symbol

        if not info.visible:
            _log(f"[INFO] Symbol '{symbol}' not visible in Market Watch, enabling it...")
            if not mt5.symbol_select(symbol, True):
                _log(f"[FAIL] Could not enable symbol '{symbol}'.")
                return False, symbol

        return True, symbol


def warmup_mt5_history(mt5, symbol: str, mt5_timeframe_name: str, log=None, api_lock=None) -> None:
    """
    Nudge MT5 to download history for symbol/timeframe before copy_rates_range.
    Empty [] results are common on Windows when the terminal never loaded that chart.
    """
    _log = log or (lambda msg: print(msg))
    try:
        with _hold_mt5_lock(api_lock):
            tf = getattr(mt5, mt5_timeframe_name)
            mt5.symbol_select(symbol, True)
            try:
                seed = mt5.copy_rates_from_pos(symbol, tf, 0, 200)
            except Exception as exc:
                _log(f"[WARN] History warm-up failed for {symbol}: {exc}")
                return
            if seed is not None and len(seed) > 0:
                _log(f"[OK] MT5 history warm-up: {len(seed)} recent bar(s) for {symbol} [{mt5_timeframe_name}]")
            else:
                _log(
                    f"[WARN] MT5 returned no recent bars for {symbol} [{mt5_timeframe_name}]. "
                    "Open that symbol on a chart in MT5, wait for history to load, then retry."
                )
    except Exception as exc:
        _log(f"[WARN] History warm-up failed for {symbol}: {exc}")


def fetch_from_mt5(mt5, symbol: str, mt5_timeframe_name: str, start: datetime, end: datetime,
                 api_lock=None):
    """
    Pulls one chunk of candles using the ALREADY-OPEN mt5 connection
    (passed in, not re-initialized here). Retries on failure instead of
    silently treating a server hiccup as "no data for this period" --
    over a 20-year pull, transient failures WILL happen occasionally.
    """
    try:
        timeframe_const = getattr(mt5, mt5_timeframe_name)
    except AttributeError:
        _safe_print(f"    [FAIL] This MT5 build has no {mt5_timeframe_name}")
        return None

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _hold_mt5_lock(api_lock):
                rates = _copy_rates_range_safe(mt5, symbol, timeframe_const, start, end)
        except Exception as exc:
            last_error = exc
            rates = None

        if rates is not None:
            if len(rates) == 0 and attempt == 1:
                # Terminal may not have synced history yet — trigger download once
                try:
                    with _hold_mt5_lock(api_lock):
                        mt5.copy_rates_from(symbol, timeframe_const, _as_utc(end), 500)
                        rates = _copy_rates_range_safe(mt5, symbol, timeframe_const, start, end)
                except Exception:
                    pass

            if rates is not None and len(rates) == 0:
                return []  # genuinely no data for this period (e.g. before history starts)

            return _rates_to_candles(rates)

        # rates is None -> request failed, retry
        try:
            last_error = mt5.last_error()
        except Exception:
            pass
        _safe_print(
            f"    [WARN] copy_rates_range failed (attempt {attempt}/{MAX_RETRIES}), "
            f"error: {last_error}. Retrying in {RETRY_DELAY_SECONDS}s..."
        )
        time.sleep(RETRY_DELAY_SECONDS)

    _safe_print(
        f"    [FAIL] Giving up on {symbol} {mt5_timeframe_name} "
        f"{start.date()}->{end.date()} after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )
    return None  # explicit failure, distinct from [] (genuinely empty period)


# ============================================================================
# SECTION 5: DATA SOURCE -- MOCK FETCH
# ============================================================================

def fetch_from_mock(symbol: str, timeframe_label: str, start: datetime, end: datetime,
                     seed_price: float = 100.0):
    minutes_per_candle = {
        "1min": 1, "3min": 3, "5min": 5, "10min": 10, "15min": 15, "30min": 30, "1hour": 60,
    }[timeframe_label]

    step = timedelta(minutes=minutes_per_candle)
    candles = []
    t = start
    price = seed_price
    rng = random.Random(f"{symbol}-{timeframe_label}")

    i = 0
    while t <= end:
        i += 1
        if i % 50 == 0:
            rng_range = abs(price) * 0.01 or 1.0
            low = price - rng_range * 0.65
            body_size = rng_range * 0.10
            high = price + body_size + rng_range * 0.25
            if i % 100 == 0:
                open_ = price
                close = price + body_size
            else:
                open_ = price + body_size
                close = price
        else:
            move = rng.uniform(-0.3, 0.3) * (abs(price) * 0.001 or 0.1)
            open_ = price
            close = price + move
            high = max(open_, close) + abs(move) * rng.uniform(0.1, 0.6)
            low = min(open_, close) - abs(move) * rng.uniform(0.1, 0.6)

        candles.append({
            "datetime": t,
            "open": round(open_, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close, 5),
            "volume": rng.randint(50, 500),
        })

        price = close
        t += step

    return candles


# ============================================================================
# SECTION 6: CSV WRITING
# ============================================================================

CSV_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]


def write_csv(filepath: str, candles: List[dict]) -> None:
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for c in candles:
            row = dict(c)
            row["datetime"] = c["datetime"].strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(row)


def append_to_full_history(filepath: str, candles: List[dict]) -> None:
    file_exists = os.path.exists(filepath)
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        for c in candles:
            row = dict(c)
            row["datetime"] = c["datetime"].strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(row)


def read_csv_candles(filepath: str) -> List[dict]:
    """Load a monthly/FULL CSV into candle dicts (timezone-naive datetimes)."""
    if not os.path.isfile(filepath):
        return []
    out: List[dict] = []
    with open(filepath, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.strptime(row["datetime"].strip(), "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError, AttributeError):
                continue
            try:
                out.append({
                    "datetime": dt,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row.get("volume") or 0)),
                })
            except (KeyError, TypeError, ValueError):
                continue
    return out


def merge_candles(existing: List[dict], new: List[dict]) -> List[dict]:
    """Merge by datetime; newer rows overwrite older ones. Sorted ascending."""
    by_dt: Dict[datetime, dict] = {}
    for c in existing:
        by_dt[c["datetime"]] = c
    for c in new:
        by_dt[c["datetime"]] = c
    return [by_dt[k] for k in sorted(by_dt.keys())]


def list_monthly_csv_files(symbol: str, timeframe_label: str,
                           output_root: Optional[str] = None) -> List[str]:
    """All monthly CSVs under data/<symbol>/<tf>/<year>/… (excludes *_FULL.csv)."""
    base = os.path.join(_root(output_root), symbol, timeframe_label)
    if not os.path.isdir(base):
        return []
    files: List[str] = []
    for entry in sorted(os.listdir(base)):
        year_dir = os.path.join(base, entry)
        if not (os.path.isdir(year_dir) and entry.isdigit()):
            continue
        for fname in sorted(os.listdir(year_dir)):
            if fname.endswith(".csv") and "_FULL" not in fname:
                files.append(os.path.join(year_dir, fname))
    return files


def detect_latest_bar(
    symbol: str,
    timeframe_label: str,
    output_root: Optional[str] = None,
) -> Optional[datetime]:
    """
    Latest bar timestamp already on disk for this symbol+timeframe.
    Prefers monthly year folders; falls back to *_FULL.csv.
    """
    latest: Optional[datetime] = None
    for path in list_monthly_csv_files(symbol, timeframe_label, output_root):
        # Fast path: read last non-empty data line instead of whole file when possible
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size <= 0:
                    continue
                chunk = min(size, 8192)
                f.seek(-chunk, os.SEEK_END)
                tail = f.read().decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
            # drop header if it snuck into the tail
            for line in reversed(lines):
                if line.lower().startswith("datetime"):
                    continue
                part = line.split(",", 1)[0].strip()
                try:
                    dt = datetime.strptime(part, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if latest is None or dt > latest:
                    latest = dt
                break
        except OSError:
            continue

    if latest is not None:
        return latest

    full_path = full_history_csv_path(symbol, timeframe_label, output_root=output_root)
    return _last_csv_datetime(full_path)


def _last_csv_datetime(filepath: str) -> Optional[datetime]:
    """Last datetime in a CSV without loading the whole file into RAM."""
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return None
            chunk = min(size, 8192)
            f.seek(-chunk, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed([ln.strip() for ln in tail.splitlines() if ln.strip()]):
            if line.lower().startswith("datetime"):
                continue
            part = line.split(",", 1)[0].strip()
            try:
                return datetime.strptime(part, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    except OSError:
        return None
    return None


def detect_data_folder_symbols(output_root: Optional[str] = None) -> List[str]:
    """Symbol subfolders under a market root (or legacy flat data root)."""
    root = _root(output_root)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith("."):
            continue
        if name.lower() in MARKET_TYPES:
            continue  # skip spot/futures containers when scanning flat root
        out.append(name)
    return out


def detect_market_inventory(output_root: Optional[str] = None) -> Dict[str, List[str]]:
    """
    Symbols found under spot/, futures/, and legacy flat folders.
    Returns {"spot": [...], "futures": [...], "legacy_spot": [...]}.
    """
    root = _root(output_root)
    inv = {MARKET_SPOT: [], MARKET_FUTURES: [], "legacy_spot": []}
    if not os.path.isdir(root):
        return inv
    for mt in MARKET_TYPES:
        mt_root = market_data_root(root, mt)
        inv[mt] = detect_data_folder_symbols(mt_root)
    # Legacy: symbols directly under data/ (not spot/futures)
    inv["legacy_spot"] = detect_data_folder_symbols(root)
    return inv


def write_or_merge_monthly(
    filepath: str,
    candles: List[dict],
    *,
    merge: bool,
) -> int:
    """Write monthly CSV; if merge and file exists, combine + dedupe. Returns row count written."""
    if merge and os.path.isfile(filepath):
        candles = merge_candles(read_csv_candles(filepath), candles)
    write_csv(filepath, candles)
    return len(candles)


# ============================================================================
# SECTION 6B: DATA QUALITY CHECK
# ============================================================================
#
# Runs basic sanity checks on the candles collected for one symbol+timeframe:
#   - duplicate timestamps
#   - zero/negative prices
#   - high < low (impossible candle)
#   - suspicious time gaps (weekends excluded automatically)
# This matters a lot for a hammer-candle backtest specifically, since a
# silently-missing stretch of candles means "the next candle" your
# strategy enters on isn't actually the next real candle -- it's whatever
# came after the gap, which corrupts entry price, SL, and results.

def check_data_quality(timeframe_label: str, candles: List[dict]) -> dict:
    report = {
        "timeframe": timeframe_label,
        "total_candles": len(candles),
        "duplicate_timestamps": 0,
        "invalid_price_rows": 0,
        "high_low_violations": 0,
        "suspicious_gaps": 0,
    }

    if not candles:
        report["is_clean"] = None  # not "dirty", just nothing to check
        return report

    sorted_candles = sorted(candles, key=lambda c: c["datetime"])

    # duplicate timestamps
    seen = set()
    dup_count = 0
    for c in sorted_candles:
        if c["datetime"] in seen:
            dup_count += 1
        seen.add(c["datetime"])
    report["duplicate_timestamps"] = dup_count

    # invalid (zero/negative) prices + high < low violations
    invalid_price_rows = 0
    hl_violations = 0
    for c in sorted_candles:
        if c["open"] <= 0 or c["high"] <= 0 or c["low"] <= 0 or c["close"] <= 0:
            invalid_price_rows += 1
        if c["high"] < c["low"]:
            hl_violations += 1
    report["invalid_price_rows"] = invalid_price_rows
    report["high_low_violations"] = hl_violations

    # gap detection (weekend closures excluded)
    minutes = TIMEFRAME_MINUTES.get(timeframe_label, 60)
    expected_gap = timedelta(minutes=minutes * EXPECTED_GAP_MULTIPLIER)
    weekend_gap_lower = timedelta(hours=40)
    weekend_gap_upper = timedelta(hours=72)

    suspicious = 0
    for i in range(1, len(sorted_candles)):
        diff = sorted_candles[i]["datetime"] - sorted_candles[i - 1]["datetime"]
        if diff > expected_gap and not (weekend_gap_lower <= diff <= weekend_gap_upper):
            suspicious += 1
    report["suspicious_gaps"] = suspicious

    is_clean = (dup_count == 0 and invalid_price_rows == 0 and hl_violations == 0)
    report["is_clean"] = is_clean

    return report


def print_quality_report(symbol: str, report: dict) -> None:
    if report.get("is_clean") is None:
        status = "NO DATA"
    elif report.get("is_clean"):
        status = "CLEAN"
    else:
        status = "NEEDS REVIEW"
    _safe_print(
        f"  [QUALITY] {symbol} | {report['timeframe']}: "
        f"{report['total_candles']} candles | "
        f"dupes={report['duplicate_timestamps']} | "
        f"bad_prices={report['invalid_price_rows']} | "
        f"high<low={report['high_low_violations']} | "
        f"gaps={report['suspicious_gaps']} | {status}"
    )


# ============================================================================
# SECTION 7: MAIN FETCH PIPELINE
# ============================================================================

def fetch_and_save(mt5, symbol: str, timeframe_label: str, mt5_timeframe_name: str,
                    start: datetime, end: datetime, use_mock: bool,
                    output_root: Optional[str] = None,
                    update_existing: bool = False,
                    log=None,
                    history_warmed: bool = False,
                    api_lock=None) -> dict:
    """
    Fetches + saves one symbol+timeframe across the date range.

    update_existing=True: merge into existing monthly CSVs (do not wipe FULL),
    and only fetch from `start` (typically last bar + 1 step) through `end`.
    """
    _log = log or (lambda msg: print(msg))
    total_saved = 0
    failed_chunks = []
    quality_sample: List[dict] = []
    QUALITY_SAMPLE_MAX = 8000

    if not use_mock and mt5 is not None and not history_warmed:
        warmup_mt5_history(mt5, symbol, mt5_timeframe_name, log=_log, api_lock=api_lock)

    full_path = full_history_csv_path(symbol, timeframe_label, output_root=output_root)

    if not update_existing and os.path.exists(full_path):
        os.remove(full_path)

    full_tip = _last_csv_datetime(full_path) if update_existing else None

    if CHUNK_BY == "month":
        chunks = list(iter_month_chunks(start, end))
    else:
        chunks = [(s, e, y, None) for (s, e, y) in iter_year_chunks(start, end)]

    for chunk in chunks:
        if CHUNK_BY == "month":
            chunk_start, chunk_end, year, month = chunk
        else:
            chunk_start, chunk_end, year = chunk
            month = None

        period_label = f"{year}-{month:02d}" if month else str(year)

        candles: List[dict] = []
        sub_failed = False
        if use_mock:
            candles = fetch_from_mock(symbol, timeframe_label, chunk_start, chunk_end)
            if candles is None:
                sub_failed = True
        else:
            for sub_start, sub_end in iter_fetch_subchunks(
                chunk_start, chunk_end, timeframe_label,
            ):
                part = fetch_from_mt5(mt5, symbol, mt5_timeframe_name, sub_start, sub_end, api_lock=api_lock)
                if part is None:
                    sub_failed = True
                    continue
                if part:
                    candles.extend(part)

        if sub_failed and not candles:
            failed_chunks.append(period_label)
            continue

        if not candles:
            continue

        if CHUNK_BY == "month":
            out_path = monthly_csv_path(
                symbol, timeframe_label, year, month, output_root=output_root,
            )
        else:
            folder = month_folder(symbol, timeframe_label, year, output_root=output_root)
            out_path = os.path.join(folder, f"{symbol}_{timeframe_label}_{year}.csv")

        before = 0
        if update_existing and os.path.isfile(out_path):
            before = len(read_csv_candles(out_path))
        written = write_or_merge_monthly(out_path, candles, merge=update_existing)
        new_rows = max(0, written - before) if update_existing else len(candles)

        if update_existing and full_tip is not None:
            to_append = [c for c in candles if c["datetime"] > full_tip]
        else:
            to_append = candles
        if to_append:
            append_to_full_history(full_path, to_append)
            tip_candidate = max(c["datetime"] for c in to_append)
            if full_tip is None or tip_candidate > full_tip:
                full_tip = tip_candidate

        if len(quality_sample) < QUALITY_SAMPLE_MAX:
            room = QUALITY_SAMPLE_MAX - len(quality_sample)
            quality_sample.extend(candles[:room])
        total_saved += new_rows if update_existing else len(candles)
        _log(
            f"  [{symbol} | {timeframe_label}] {period_label}: "
            f"{len(candles)} fetched"
            + (f", +{new_rows} new" if update_existing else "")
            + f" -> {out_path}"
        )

    if failed_chunks:
        _log(
            f"  [WARN] {symbol} | {timeframe_label}: "
            f"{len(failed_chunks)} period(s) FAILED after retries: {failed_chunks}"
        )

    quality = check_data_quality(timeframe_label, quality_sample)

    return {
        "total_saved": total_saved,
        "failed_chunks": failed_chunks,
        "quality": quality,
    }


def run_fetcher(use_mock: bool) -> None:
    ensure_dir(OUTPUT_ROOT)
    mode_label = "MOCK (test data)" if use_mock else "LIVE (real MT5 data)"
    print(f"Starting fetch in {mode_label} mode")
    print(f"Symbols: {SYMBOLS}")
    print(f"Timeframes: {list(TIMEFRAMES.keys())}")
    print(f"Date range: {START_DATE.date()} to {END_DATE.date()}")
    print(f"Output folder: ./{OUTPUT_ROOT}/")
    print("-" * 70)

    mt5 = None
    if not use_mock:
        mt5 = connect_to_mt5()

    all_results = []

    try:
        for symbol in SYMBOLS:
            if not use_mock:
                ok, mt5_symbol = verify_symbol(mt5, symbol)
                if not ok:
                    print(f"[SKIP] Skipping symbol '{symbol}' entirely -- not found/enabled.")
                    continue
            else:
                mt5_symbol = symbol

            for timeframe_label, mt5_timeframe_name in TIMEFRAMES.items():
                print(f"\nFetching {mt5_symbol} [{timeframe_label}] ...")
                result = fetch_and_save(
                    mt5=mt5,
                    symbol=mt5_symbol,
                    timeframe_label=timeframe_label,
                    mt5_timeframe_name=mt5_timeframe_name,
                    start=START_DATE,
                    end=END_DATE,
                    use_mock=use_mock,
                    output_root=OUTPUT_ROOT,
                    update_existing=False,
                )
                all_results.append((symbol, timeframe_label, result))
                print(f"  -> Total candles saved for {symbol} [{timeframe_label}]: "
                      f"{result['total_saved']}")
                print_quality_report(symbol, result["quality"])
    finally:
        if mt5 is not None:
            disconnect_from_mt5(mt5, owned=True)

    print("\n" + "-" * 70)
    grand_total = sum(r["total_saved"] for _, _, r in all_results)
    print(f"DONE. Grand total candles saved across all symbols/timeframes: {grand_total}")
    print(f"Check the '{OUTPUT_ROOT}/' folder for your sorted CSV files.")

    any_failures = [(s, tf, r["failed_chunks"]) for s, tf, r in all_results if r["failed_chunks"]]
    if any_failures:
        print("\n[ATTENTION] Some periods FAILED after retries and were NOT saved:")
        for symbol, tf, chunks in any_failures:
            print(f"    {symbol} [{tf}]: {chunks}")
        print("    Re-run the script (or narrow START_DATE/END_DATE to just these "
              "periods) to retry them.")
    else:
        print("\n[OK] No failed chunks -- every requested period was fetched successfully.")

    any_dirty = [(s, tf) for s, tf, r in all_results if r["quality"].get("is_clean") is False]
    if any_dirty:
        print("\n[ATTENTION] Some datasets need review (duplicates/bad prices/high<low):")
        for symbol, tf in any_dirty:
            print(f"    {symbol} [{tf}]")


def run_fetch_job(
    *,
    output_root: str,
    symbols: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
    update_existing: bool = True,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    use_mock: bool = False,
    terminal_path: str = "",
    login: int = 0,
    password: str = "",
    server: str = "",
    market_type: Optional[str] = None,
    auto_detect_market: bool = True,
    flat_under_root: bool = False,
    mt5_module=None,
    own_connection: Optional[bool] = None,
    api_lock=None,
    log=None,
    should_stop=None,
) -> dict:
    """
    Programmatic fetch used by the dashboard Fetch menu.

    Spot and futures are stored separately:
        <output_root>/spot/<symbol>/…
        <output_root>/futures/<symbol>/…
    Legacy <output_root>/<symbol>/… is treated as spot for updates.

    flat_under_root=True (per Live account history):
        <output_root>/<symbol>/<timeframe>/…  e.g. data/ic_markets/XAUUSD/1hour

    market_type: "spot" | "futures" | None (auto from MT5 / name).

    Connection safety (critical):
      - Prefer mt5_module= from Live (own_connection=False + api_lock) so Fetch
        never initialize()/shutdown() a second terminal and never kills Live.
      - When Live runs in an account worker process, the dashboard routes Fetch
        into that worker (same shared MT5) — Live keeps polling while history downloads.
      - Otherwise Fetch opens ONE connection and shuts it down when done (only when
        Live is not using that account).
    """
    _log = log or (lambda msg: print(msg))
    stop = should_stop or (lambda: False)

    root = os.path.abspath(os.path.expanduser(output_root or OUTPUT_ROOT))
    # If caller already pointed at …/spot or …/futures, peel up to parent data root
    if os.path.basename(root).lower() in MARKET_TYPES:
        if market_type is None:
            market_type = os.path.basename(root).lower()
        root = os.path.dirname(root) or root
    ensure_dir(root)

    syms = list(symbols or SYMBOLS)
    tfs = list(timeframes or list(TIMEFRAMES.keys()))
    for tf in tfs:
        if tf not in TIMEFRAMES:
            raise ValueError(f"Unknown timeframe folder label: {tf}")

    end = end_date or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    default_start = start_date or START_DATE
    if default_start.tzinfo is None:
        default_start = default_start.replace(tzinfo=timezone.utc)

    forced_market = normalize_market_type(market_type) if market_type else None
    mode = "UPDATE existing" if update_existing else "FULL refresh"
    _log(f"Data folder: {root}")
    inv = detect_market_inventory(root)
    _log(
        f"On disk — spot: {', '.join(inv[MARKET_SPOT]) or '(none)'} | "
        f"futures: {', '.join(inv[MARKET_FUTURES]) or '(none)'} | "
        f"legacy: {', '.join(inv['legacy_spot']) or '(none)'}"
    )
    _log(
        f"Mode: {mode} | Market: {forced_market or 'auto-detect'} | "
        f"Symbols: {syms} | Timeframes: {tfs}"
    )
    _log("-" * 60)

    mt5 = None
    owns_mt5 = False
    if not use_mock:
        if mt5_module is not None:
            mt5 = mt5_module
            owns_mt5 = False if own_connection is None else bool(own_connection)
            try:
                info = mt5.account_info()
                if info is not None:
                    _log(
                        f"[OK] Reusing open MT5 connection — Account: {info.login} | "
                        f"Server: {info.server} (shared, will not shut down)"
                    )
                else:
                    _log("[OK] Reusing open MT5 connection (shared, will not shut down)")
            except Exception:
                _log("[OK] Reusing open MT5 connection (shared, will not shut down)")
        else:
            mt5 = connect_to_mt5(
                terminal_path=terminal_path,
                login=int(login or 0),
                password=password or "",
                server=server or "",
                log=_log,
            )
            owns_mt5 = True if own_connection is None else bool(own_connection)

    all_results = []
    write_roots_used = set()
    up_to_date_count = 0
    try:
        for symbol in syms:
            if stop():
                _log("[STOP] Fetch cancelled.")
                break
            mt5_symbol = symbol
            if not use_mock:
                ok, mt5_symbol = verify_symbol(mt5, symbol, log=_log, api_lock=api_lock)
                if not ok:
                    _log(f"[SKIP] Symbol '{symbol}' not found on this broker.")
                    continue

            if forced_market:
                sym_market = forced_market
                detected = (
                    detect_market_type_from_mt5(mt5, mt5_symbol)
                    if (auto_detect_market and mt5) else classify_market_type(symbol)
                )
                if detected != sym_market:
                    _log(
                        f"[INFO] '{symbol}' looks like {detected} from "
                        f"{'MT5' if mt5 else 'name'}, but saving under {sym_market} as selected."
                    )
            else:
                sym_market = (
                    detect_market_type_from_mt5(mt5, mt5_symbol)
                    if mt5 else classify_market_type(symbol)
                )

            write_root = resolve_write_root(
                root, symbol, sym_market, flat_under_root=flat_under_root,
            )
            write_roots_used.add(write_root)
            if flat_under_root:
                _log(f"\n=== {symbol} → account folder @ {write_root} ===")
            else:
                _log(f"\n=== {symbol} → {sym_market.upper()} @ {write_root} ===")
            if mt5_symbol != symbol:
                _log(f"MT5 symbol: {mt5_symbol}")

            history_warmed = False
            for timeframe_label in tfs:
                if stop():
                    _log("[STOP] Fetch cancelled.")
                    break
                mt5_tf = TIMEFRAMES[timeframe_label]
                tf_start = default_start
                updating = False
                if update_existing:
                    latest = detect_latest_bar(symbol, timeframe_label, output_root=write_root)
                    if latest is not None:
                        minutes = TIMEFRAME_MINUTES.get(timeframe_label, 60)
                        naive_start = latest + timedelta(minutes=minutes)
                        tf_start = naive_start.replace(tzinfo=timezone.utc)
                        updating = True
                        _log(
                            f"\n{symbol} [{timeframe_label}]: existing {sym_market} data through "
                            f"{latest} — updating from {naive_start} …"
                        )
                    else:
                        _log(
                            f"\n{symbol} [{timeframe_label}]: no local {sym_market} data — "
                            f"full pull from {tf_start.date()} …"
                        )
                else:
                    _log(
                        f"\n{symbol} [{timeframe_label}]: full {sym_market} refresh "
                        f"from {tf_start.date()} …"
                    )

                if tf_start >= end:
                    _log(f"  Already up to date (latest on disk: {latest if updating else 'n/a'}).")
                    up_to_date_count += 1
                    all_results.append((symbol, timeframe_label, {
                        "total_saved": 0,
                        "failed_chunks": [],
                        "market_type": sym_market,
                        "write_root": write_root,
                        "quality": {
                            "timeframe": timeframe_label,
                            "total_candles": 0,
                            "duplicate_timestamps": 0,
                            "invalid_price_rows": 0,
                            "high_low_violations": 0,
                            "suspicious_gaps": 0,
                            "is_clean": None,
                        },
                    }))
                    continue

                result = fetch_and_save(
                    mt5=mt5,
                    symbol=mt5_symbol,
                    timeframe_label=timeframe_label,
                    mt5_timeframe_name=mt5_tf,
                    start=tf_start,
                    end=end,
                    use_mock=use_mock,
                    output_root=write_root,
                    update_existing=updating or update_existing,
                    log=_log,
                    history_warmed=history_warmed,
                    api_lock=api_lock,
                )
                history_warmed = True
                result["market_type"] = sym_market
                result["write_root"] = write_root
                all_results.append((symbol, timeframe_label, result))
                _log(
                    f"  -> {symbol} [{timeframe_label}] ({sym_market}): "
                    f"{result['total_saved']} candle(s) written"
                )
                print_quality_report(symbol, result["quality"])
    finally:
        if mt5 is not None:
            disconnect_from_mt5(mt5, owned=owns_mt5, log=_log)

    grand = sum(r["total_saved"] for _, _, r in all_results)
    _log("-" * 60)
    _log(f"DONE. Candles written this run: {grand}")
    if grand == 0 and up_to_date_count > 0:
        _log(
            f"[INFO] {up_to_date_count} timeframe(s) already up to date — "
            "0 new bars is normal. Try 'Full re-fetch from start year' for a fresh pull."
        )
    elif grand == 0 and all_results:
        _log(
            "[WARN] 0 candles written. Check: (1) exact symbol in MT5 Market Watch, "
            "(2) open a chart for that symbol and wait for history, "
            "(3) try Full re-fetch mode, (4) some brokers do not offer 3min/10min bars."
        )
    _log(f"Data folder: {root}")
    for wr in sorted(write_roots_used):
        _log(f"  Wrote under: {wr}")
    primary_write = (
        sorted(write_roots_used)[0]
        if write_roots_used
        else market_data_root(root, forced_market or MARKET_SPOT)
    )
    return {
        "output_root": root,
        "write_root": primary_write,
        "market_type": forced_market or (
            all_results[0][2].get("market_type") if all_results else MARKET_SPOT
        ),
        "total_saved": grand,
        "up_to_date_count": up_to_date_count,
        "results": all_results,
    }


# ============================================================================
# SECTION 8: COMMAND-LINE ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch MT5 historical candle data, sorted by timeframe/year/month."
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use fake test data instead of connecting to real MT5.",
    )
    args = parser.parse_args()

    run_fetcher(use_mock=args.mock)