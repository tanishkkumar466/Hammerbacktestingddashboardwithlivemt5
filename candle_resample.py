"""
Resample 1-minute OHLC CSVs into custom N-minute folders for backtests.

Folder naming matches fetch.py: 4min, 12min, ... under
  data/spot/<symbol>/4min/YYYY/<symbol>_4min_YYYY-MM.csv
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import fetch as data_fetcher

LogFn = Callable[[str], None]

# Suggested custom minute sizes (all divide cleanly from 1-minute bars).
DEFAULT_CUSTOM_MINUTES: Tuple[int, ...] = (2, 4, 6, 8, 12, 20)

STANDARD_FOLDERS = frozenset(data_fetcher.TIMEFRAMES.keys())


def folder_for_minutes(minutes: int) -> str:
    m = int(minutes)
    if m < 1:
        raise ValueError("minutes must be >= 1")
    if m == 60:
        return "1hour"
    if m % 60 == 0:
        return f"{m // 60}hour"
    return f"{m}min"


def minutes_from_folder(folder: str) -> Optional[int]:
    f = (folder or "").strip().lower()
    if f in data_fetcher.TIMEFRAME_MINUTES:
        return data_fetcher.TIMEFRAME_MINUTES[f]
    if f.endswith("min") and f[:-3].isdigit():
        return int(f[:-3])
    if f.endswith("hour") and f[:-4].isdigit():
        return int(f[:-4]) * 60
    if f.endswith("h") and f[:-1].isdigit():
        return int(f[:-1]) * 60
    return None


def logic_label_for_folder(folder: str) -> str:
    """Map disk folder (4min) -> strategy key (4m)."""
    f = (folder or "").strip().lower()
    static = {
        "1min": "1m",
        "3min": "3m",
        "5min": "5m",
        "10min": "10m",
        "15min": "15m",
        "30min": "30m",
        "1hour": "1h",
    }
    if f in static:
        return static[f]
    mins = minutes_from_folder(f)
    if mins is None:
        return f
    if mins % 60 == 0:
        hours = mins // 60
        return "1h" if hours == 1 else f"{hours}h"
    return f"{mins}m"


def logic_label_for_minutes(minutes: int) -> str:
    return logic_label_for_folder(folder_for_minutes(minutes))


def folder_for_logic_label(label: str) -> str:
    """Map strategy key (4m / 1h) -> disk folder (4min / 1hour)."""
    raw = (label or "").strip().lower()
    static = {
        "1m": "1min",
        "3m": "3min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1hour",
    }
    if raw in static:
        return static[raw]
    if raw.endswith("h") and raw[:-1].isdigit():
        hours = int(raw[:-1])
        return "1hour" if hours == 1 else f"{hours}hour"
    if raw.endswith("m") and raw[:-1].isdigit():
        return folder_for_minutes(int(raw[:-1]))
    mins = minutes_from_folder(raw)
    if mins is not None:
        return folder_for_minutes(mins)
    return raw


def _bucket_start(dt: datetime, minutes: int) -> datetime:
    """Floor timestamp to the N-minute wall-clock bucket (naive local/broker time)."""
    m = int(minutes)
    if m < 1:
        raise ValueError("minutes must be >= 1")
    # Drop seconds/micros, then align minute-of-day
    base = dt.replace(second=0, microsecond=0)
    minute_of_day = base.hour * 60 + base.minute
    aligned = (minute_of_day // m) * m
    return base.replace(hour=aligned // 60, minute=aligned % 60)


def resample_ohlc_candles(
    candles: Sequence[dict],
    minutes: int,
) -> List[dict]:
    """
    Aggregate 1-minute (or finer) OHLC dicts into N-minute bars.

    open=first, high=max, low=min, close=last, volume=sum.
    Incomplete trailing buckets are kept (same as typical chart platforms).
    """
    m = int(minutes)
    if m < 1:
        raise ValueError("minutes must be >= 1")
    if m == 1:
        return sorted((dict(c) for c in candles), key=lambda c: c["datetime"])

    buckets: Dict[datetime, List[dict]] = defaultdict(list)
    for c in candles:
        dt = c.get("datetime")
        if not isinstance(dt, datetime):
            continue
        buckets[_bucket_start(dt, m)].append(c)

    out: List[dict] = []
    for start in sorted(buckets):
        rows = sorted(buckets[start], key=lambda r: r["datetime"])
        try:
            o = float(rows[0]["open"])
            h = max(float(r["high"]) for r in rows)
            low = min(float(r["low"]) for r in rows)
            cl = float(rows[-1]["close"])
        except (KeyError, TypeError, ValueError):
            continue
        vol = 0
        for r in rows:
            try:
                vol += int(float(r.get("volume") or 0))
            except (TypeError, ValueError):
                pass
        out.append(
            {
                "datetime": start,
                "open": o,
                "high": h,
                "low": low,
                "close": cl,
                "volume": vol,
            }
        )
    return out


def _split_by_month(candles: Sequence[dict]) -> Dict[Tuple[int, int], List[dict]]:
    by_month: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
    for c in candles:
        dt = c["datetime"]
        by_month[(dt.year, dt.month)].append(c)
    return by_month


def load_1min_candles_from_disk(
    market_root: str,
    symbol: str,
) -> List[dict]:
    """Load all 1min CSVs for symbol under a market root (e.g. data/spot)."""
    import backtest as bt

    files = bt.find_csv_files(market_root, symbol, "1min")
    if not files:
        # try resolve_symbol_data_root in case market_root is parent data/
        try:
            eff_root, eff_sym = bt.resolve_symbol_data_root(market_root, symbol)
            files = bt.find_csv_files(eff_root, eff_sym, "1min")
            market_root, symbol = eff_root, eff_sym
        except Exception:
            pass
    candles: List[dict] = []
    for path in files:
        candles.extend(data_fetcher.read_csv_candles(path))
    return data_fetcher.merge_candles([], candles)


def write_resampled_folder(
    market_root: str,
    symbol: str,
    minutes: int,
    candles_1m: Sequence[dict],
    *,
    log: Optional[LogFn] = None,
) -> Tuple[str, int]:
    """
    Resample and write monthly + FULL CSVs under <market_root>/<symbol>/<Nmin>/.
    Returns (folder_name, bar_count).
    """
    _log = log or (lambda _m: None)
    folder = folder_for_minutes(minutes)
    if folder == "1min":
        raise ValueError("Refusing to overwrite 1min from itself")

    resampled = resample_ohlc_candles(candles_1m, minutes)
    if not resampled:
        _log(f"{folder}: no bars produced")
        return folder, 0

    # Write into market_root/symbol/... (fetch helpers take output_root = market_root)
    # monthly_csv_path joins output_root/symbol/tf/year/...
    for (year, month), rows in sorted(_split_by_month(resampled).items()):
        path = data_fetcher.monthly_csv_path(
            symbol, folder, year, month, output_root=market_root,
        )
        data_fetcher.write_csv(path, rows)
        _log(f"Wrote {len(rows)} bars -> {path}")

    full_path = data_fetcher.full_history_csv_path(
        symbol, folder, output_root=market_root,
    )
    data_fetcher.write_csv(full_path, resampled)
    _log(f"Wrote FULL {len(resampled)} bars -> {full_path}")
    return folder, len(resampled)


def convert_1min_to_custom_timeframes(
    data_root: str,
    symbol: str,
    minutes_list: Iterable[int],
    *,
    market_type: Optional[str] = None,
    log: Optional[LogFn] = None,
) -> Dict[str, int]:
    """
    Build custom TF folders from on-disk 1min data.

    data_root: parent data/ or already market-specific .../spot
    market_type: spot|futures|None (auto from path / default spot)
    Returns {folder: bar_count}.
    """
    _log = log or (lambda _m: None)
    mins = sorted({int(m) for m in minutes_list if int(m) >= 2})
    if not mins:
        raise ValueError("Select at least one custom timeframe (>= 2 minutes).")

    mt = data_fetcher.normalize_market_type(market_type) if market_type else None
    root = os.path.normpath(data_root)
    base = os.path.basename(root).lower()
    if base in data_fetcher.MARKET_TYPES:
        market_root = root
        mt = base
    else:
        mt = mt or data_fetcher.MARKET_SPOT
        market_root = data_fetcher.market_data_root(root, mt)

    _log(f"Looking for 1min data under {market_root}/{symbol} ...")
    candles = load_1min_candles_from_disk(market_root, symbol)
    if not candles:
        # Legacy layout data/SYMBOL/1min
        legacy_root = root if base not in data_fetcher.MARKET_TYPES else os.path.dirname(root)
        candles = load_1min_candles_from_disk(legacy_root, symbol)
        if candles:
            market_root = legacy_root
            _log(f"Using legacy layout under {market_root}")
    if not candles:
        raise FileNotFoundError(
            f"No 1min CSV data found for {symbol}. "
            f"Fetch 1min first, then convert."
        )

    _log(f"Loaded {len(candles)} one-minute bars. Building: {mins}")
    results: Dict[str, int] = {}
    for m in mins:
        folder, count = write_resampled_folder(
            market_root, symbol, m, candles, log=_log,
        )
        results[folder] = count
    return results
