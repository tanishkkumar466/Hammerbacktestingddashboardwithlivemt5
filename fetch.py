"""
fetcher.py -- uploaded version, saved here for review/testing
"""

import os
import sys
import time
import argparse
import random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import csv


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
    "3min": 3, "5min": 5, "10min": 10, "15min": 15, "30min": 30, "1hour": 60,
}


# ============================================================================
# SECTION 2: FOLDER / FILE STRUCTURE HELPERS
# ============================================================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def month_folder(symbol: str, timeframe_label: str, year: int) -> str:
    path = os.path.join(OUTPUT_ROOT, symbol, timeframe_label, str(year))
    ensure_dir(path)
    return path


def monthly_csv_path(symbol: str, timeframe_label: str, year: int, month: int) -> str:
    folder = month_folder(symbol, timeframe_label, year)
    filename = f"{symbol}_{timeframe_label}_{year:04d}-{month:02d}.csv"
    return os.path.join(folder, filename)


def full_history_csv_path(symbol: str, timeframe_label: str) -> str:
    folder = os.path.join(OUTPUT_ROOT, symbol, timeframe_label)
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

def connect_to_mt5():
    """
    Initializes the connection to the local MT5 terminal ONCE for the
    whole script run. Returns the mt5 module itself (so callers don't
    need to re-import it), or raises RuntimeError if it can't connect.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise RuntimeError(
            "MetaTrader5 package not found. Install it with:\n"
            "    pip install MetaTrader5\n"
            "and make sure you're running this on Windows with the MT5 "
            "terminal installed and logged in."
        )

    if not mt5.initialize():
        raise RuntimeError(
            f"MT5 initialize() failed, error code: {mt5.last_error()}. "
            f"Make sure the MT5 terminal is open and logged into an account."
        )

    account_info = mt5.account_info()
    if account_info is not None:
        print(f"[OK] Connected to MT5 -- Account: {account_info.login} | "
              f"Server: {account_info.server}")
    else:
        print("[OK] Connected to MT5 (no account info available).")

    return mt5


def disconnect_from_mt5(mt5):
    """Cleanly shuts down the single MT5 connection at the very end."""
    mt5.shutdown()
    print("[OK] MT5 connection closed.")


def verify_symbol(mt5, symbol: str) -> bool:
    """
    Confirms the symbol exists and is enabled in Market Watch before we
    waste time looping through 20 years of chunks for a symbol that
    doesn't exist on this broker.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"[FAIL] Symbol '{symbol}' not found on this broker. "
              f"Check the exact symbol name in MT5's Market Watch panel "
              f"(some brokers use 'XAUUSD.m', 'GOLD', etc.).")
        return False

    if not info.visible:
        print(f"[INFO] Symbol '{symbol}' not visible in Market Watch, enabling it...")
        if not mt5.symbol_select(symbol, True):
            print(f"[FAIL] Could not enable symbol '{symbol}'.")
            return False

    return True


def fetch_from_mt5(mt5, symbol: str, mt5_timeframe_name: str, start: datetime, end: datetime):
    """
    Pulls one chunk of candles using the ALREADY-OPEN mt5 connection
    (passed in, not re-initialized here). Retries on failure instead of
    silently treating a server hiccup as "no data for this period" --
    over a 20-year pull, transient failures WILL happen occasionally.
    """
    timeframe_const = getattr(mt5, mt5_timeframe_name)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        rates = mt5.copy_rates_range(symbol, timeframe_const, start, end)

        if rates is not None:
            if len(rates) == 0:
                return []  # genuinely no data for this period (e.g. before history starts)

            candles = []
            for r in rates:
                candles.append({
                    # MT5 timestamps are in the BROKER'S SERVER time, not
                    # UTC. We keep it timezone-naive here and label it
                    # clearly so nobody mistakes it for true UTC -- if you
                    # need true UTC, convert using your broker's known
                    # server UTC offset.
                    "datetime": datetime.fromtimestamp(r["time"], tz=timezone.utc).replace(tzinfo=None),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": int(r["tick_volume"]),
                })
            return candles

        # rates is None -> request failed, retry
        last_error = mt5.last_error()
        print(f"    [WARN] copy_rates_range failed (attempt {attempt}/{MAX_RETRIES}), "
              f"error: {last_error}. Retrying in {RETRY_DELAY_SECONDS}s...")
        time.sleep(RETRY_DELAY_SECONDS)

    print(f"    [FAIL] Giving up on {symbol} {mt5_timeframe_name} "
          f"{start.date()}->{end.date()} after {MAX_RETRIES} attempts. "
          f"Last error: {last_error}")
    return None  # explicit failure, distinct from [] (genuinely empty period)


# ============================================================================
# SECTION 5: DATA SOURCE -- MOCK FETCH
# ============================================================================

def fetch_from_mock(symbol: str, timeframe_label: str, start: datetime, end: datetime,
                     seed_price: float = 100.0):
    minutes_per_candle = {
        "3min": 3, "5min": 5, "10min": 10, "15min": 15, "30min": 30, "1hour": 60,
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
    print(f"  [QUALITY] {symbol} | {report['timeframe']}: "
          f"{report['total_candles']} candles | "
          f"dupes={report['duplicate_timestamps']} | "
          f"bad_prices={report['invalid_price_rows']} | "
          f"high<low={report['high_low_violations']} | "
          f"gaps={report['suspicious_gaps']} | {status}")


# ============================================================================
# SECTION 7: MAIN FETCH PIPELINE
# ============================================================================

def fetch_and_save(mt5, symbol: str, timeframe_label: str, mt5_timeframe_name: str,
                    start: datetime, end: datetime, use_mock: bool) -> dict:
    """
    Fetches + saves one symbol+timeframe across the full date range.

    Returns a summary dict instead of just a total count, so the caller
    can tell the difference between "no data existed for this period"
    (normal, e.g. before broker history starts) and "the request failed
    after retries" (a real problem worth re-running).
    """
    total_saved = 0
    failed_chunks = []   # list of (period_label,) that failed even after retries
    all_candles: List[dict] = []   # kept in memory briefly for the quality check

    full_path = full_history_csv_path(symbol, timeframe_label)

    if os.path.exists(full_path):
        os.remove(full_path)

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

        if use_mock:
            candles = fetch_from_mock(symbol, timeframe_label, chunk_start, chunk_end)
        else:
            candles = fetch_from_mt5(mt5, symbol, mt5_timeframe_name, chunk_start, chunk_end)

        if candles is None:
            # Explicit failure after retries -- distinct from a genuinely
            # empty period. Flagged so you know to re-run just this chunk.
            failed_chunks.append(period_label)
            continue

        if not candles:
            continue  # genuinely empty period (e.g. before broker's history starts)

        if CHUNK_BY == "month":
            out_path = monthly_csv_path(symbol, timeframe_label, year, month)
        else:
            folder = month_folder(symbol, timeframe_label, year)
            out_path = os.path.join(folder, f"{symbol}_{timeframe_label}_{year}.csv")

        write_csv(out_path, candles)
        append_to_full_history(full_path, candles)
        all_candles.extend(candles)

        total_saved += len(candles)
        print(f"  [{symbol} | {timeframe_label}] {period_label}: "
              f"{len(candles)} candles -> {out_path}")

    if failed_chunks:
        print(f"  [WARN] {symbol} | {timeframe_label}: "
              f"{len(failed_chunks)} period(s) FAILED after retries: {failed_chunks}")

    quality = check_data_quality(timeframe_label, all_candles)

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

    # ---- Connect to MT5 ONCE for the whole run (not per month/timeframe) ----
    mt5 = None
    if not use_mock:
        mt5 = connect_to_mt5()

    all_results = []   # (symbol, timeframe_label, result_dict)

    try:
        for symbol in SYMBOLS:
            if not use_mock:
                if not verify_symbol(mt5, symbol):
                    print(f"[SKIP] Skipping symbol '{symbol}' entirely -- not found/enabled.")
                    continue

            for timeframe_label, mt5_timeframe_name in TIMEFRAMES.items():
                print(f"\nFetching {symbol} [{timeframe_label}] ...")
                result = fetch_and_save(
                    mt5=mt5,
                    symbol=symbol,
                    timeframe_label=timeframe_label,
                    mt5_timeframe_name=mt5_timeframe_name,
                    start=START_DATE,
                    end=END_DATE,
                    use_mock=use_mock,
                )
                all_results.append((symbol, timeframe_label, result))
                print(f"  -> Total candles saved for {symbol} [{timeframe_label}]: "
                      f"{result['total_saved']}")
                print_quality_report(symbol, result["quality"])
    finally:
        if mt5 is not None:
            disconnect_from_mt5(mt5)

    # ---- Final summary ----
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