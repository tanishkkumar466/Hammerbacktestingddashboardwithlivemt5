"""
Trading session labels for backtest / live analytics.

IC Markets MT5 CSV timestamps match **broker server time** (GMT+2 in winter,
GMT+3 when US daylight saving is active — e.g. from 8 March 2026 per IC Markets).
Your `data/` CSV `datetime` column is the same clock as the MT5 terminal
(XAUUSD first hourly bar is typically ~01:00–01:02 server time).

Each trade is assigned to exactly one session from the **entry bar** open time.

Non-overlapping server-hour buckets (standard FX-style split for XAUUSD):

    Asian  — 00:00–07:59 server  (Sydney / Tokyo)
    London — 08:00–15:59 server  (London open through NY morning)
    US     — 16:00–23:59 server  (New York afternoon / close)

Adjust ASIAN_END_HOUR / LONDON_END_HOUR if your broker uses different boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

import polars as pl

# Exclusive upper bounds (hour 0–23 on broker server clock).
ASIAN_END_HOUR = 8      # hours 0..7
LONDON_END_HOUR = 16    # hours 8..15

SESSION_ASIAN = "Asian"
SESSION_LONDON = "London"
SESSION_US = "US"

SESSION_ORDER: List[str] = [SESSION_ASIAN, SESSION_LONDON, SESSION_US]

BROKER_TIME_LABEL = "IC Markets server (GMT+2 / GMT+3)"

SESSION_WINDOWS: List[Tuple[str, str, str]] = [
    (SESSION_ASIAN, "00:00", "07:59 server"),
    (SESSION_LONDON, "08:00", "15:59 server"),
    (SESSION_US, "16:00", "23:59 server"),
]

DATA_SOURCE_NOTE = (
    f"Session = entry bar {BROKER_TIME_LABEL}. "
    "Asian 00:00–07:59 · London 08:00–15:59 · US 16:00–23:59."
)


def classify_session(dt: Optional[datetime]) -> str:
    """Return Asian | London | US from entry bar datetime (server clock hour)."""
    if dt is None:
        return SESSION_ASIAN
    return classify_hour(dt.hour)


def classify_hour(hour: int) -> str:
    h = int(hour) % 24
    if h < ASIAN_END_HOUR:
        return SESSION_ASIAN
    if h < LONDON_END_HOUR:
        return SESSION_LONDON
    return SESSION_US


def session_polars_expr(entry_col: str = "entry_time") -> pl.Expr:
    """Vectorized session label from a datetime column."""
    hour = pl.col(entry_col).dt.hour()
    return (
        pl.when(hour < ASIAN_END_HOUR)
        .then(pl.lit(SESSION_ASIAN))
        .when(hour < LONDON_END_HOUR)
        .then(pl.lit(SESSION_LONDON))
        .otherwise(pl.lit(SESSION_US))
        .alias("session")
    )


def add_session_column(df: pl.DataFrame, entry_col: str = "entry_time") -> pl.DataFrame:
    if df.height == 0:
        return df.with_columns(pl.lit(None).cast(pl.Utf8).alias("session"))
    if "session" in df.columns:
        return df
    return df.with_columns(session_polars_expr(entry_col))


def describe_sessions() -> str:
    parts = [f"{name} ({start}–{end})" for name, start, end in SESSION_WINDOWS]
    return f"Sessions (entry bar, {BROKER_TIME_LABEL}): " + " · ".join(parts)
