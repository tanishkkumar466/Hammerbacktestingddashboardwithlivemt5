"""
Trading session labels and entry-time filters for backtest / live.

IC Markets MT5 CSV timestamps match **broker server time** (GMT+2 in winter,
GMT+3 when US daylight saving is active). Your `data/` CSV `datetime` column
is the same clock as the MT5 terminal (XAUUSD first hourly bar is typically
~01:00 server time).

Each trade is assigned to exactly one session from the **entry bar** open time.

Non-overlapping hour buckets (broker server clock):

    Asian  — 00:00–07:59
    London — 08:00–15:59
    US     — 16:00–23:59

**Indian time (IST) filter:** converts each broker bar to IST automatically.
IC Markets GMT+2 / GMT+3 is inferred from the bar date (US daylight saving
calendar) — no manual GMT checkbox needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import polars as pl

# Exclusive upper bounds (hour 0–23 on the active clock).
ASIAN_END_HOUR = 8      # hours 0..7
LONDON_END_HOUR = 16    # hours 8..15

SESSION_ASIAN = "Asian"
SESSION_LONDON = "London"
SESSION_US = "US"

SESSION_ORDER: List[str] = [SESSION_ASIAN, SESSION_LONDON, SESSION_US]

CLOCK_BROKER = "broker"
CLOCK_IST = "ist"
CLOCK_MODES: List[str] = [CLOCK_BROKER, CLOCK_IST]

# Broker local = UTC + this many hours (IC Markets).
BROKER_UTC_OFFSET_WINTER = 2.0   # GMT+2 → IST = broker + 3h30
BROKER_UTC_OFFSET_SUMMER = 3.0   # GMT+3 → IST = broker + 2h30

IST_UTC_OFFSET_HOURS = 5.5  # India Standard Time (no DST)

BROKER_TIME_LABEL = "IC Markets server (GMT+2 / GMT+3)"
IST_TIME_LABEL = "Indian Standard Time (IST, UTC+5:30)"

SESSION_WINDOWS: List[Tuple[str, str, str]] = [
    (SESSION_ASIAN, "00:00", "07:59"),
    (SESSION_LONDON, "08:00", "15:59"),
    (SESSION_US, "16:00", "23:59"),
]

DATA_SOURCE_NOTE = (
    "Session = entry bar Asian 00–07 · London 08–15 · US 16–23 on broker clock. "
    "Indian-time filter converts IC Markets → IST automatically (GMT+2/GMT+3 by date)."
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """n-th weekday in month (Mon=0 … Sun=6), as date at 00:00."""
    d = datetime(year, month, 1)
    delta = (weekday - d.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    return datetime(year, month, day)


def us_dst_active(dt: datetime) -> bool:
    """
    US daylight saving (Energy Policy Act): 2nd Sunday in March → 1st Sunday in November.
    IC Markets switches GMT+2 ↔ GMT+3 with this calendar.
    """
    y = dt.year
    start = _nth_weekday(y, 3, 6, 2)   # 2nd Sunday March
    end = _nth_weekday(y, 11, 6, 1)    # 1st Sunday November
    return start <= datetime(dt.year, dt.month, dt.day) < end


def ic_markets_utc_offset_hours(dt: Optional[datetime]) -> float:
    """GMT+3 in US DST, else GMT+2 — matches IC Markets MT5 server."""
    if dt is None:
        return BROKER_UTC_OFFSET_WINTER
    return BROKER_UTC_OFFSET_SUMMER if us_dst_active(dt) else BROKER_UTC_OFFSET_WINTER


def broker_to_ist(
    dt: Optional[datetime],
    broker_utc_offset_hours: Optional[float] = None,
) -> Optional[datetime]:
    """
    Convert naive broker-server datetime → naive IST datetime.

    If broker_utc_offset_hours is None, IC Markets GMT+2/GMT+3 is chosen
    automatically from the bar date (US daylight saving).
    """
    if dt is None:
        return None
    if broker_utc_offset_hours is None:
        offset = ic_markets_utc_offset_hours(dt)
    else:
        try:
            offset = float(broker_utc_offset_hours)
        except (TypeError, ValueError):
            offset = ic_markets_utc_offset_hours(dt)
    utc = dt - timedelta(hours=offset)
    return utc + timedelta(hours=IST_UTC_OFFSET_HOURS)


def entry_clock_datetime(
    dt: Optional[datetime],
    *,
    clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: Optional[float] = None,
) -> Optional[datetime]:
    """Return datetime on the clock used for session / IST time filters."""
    if dt is None:
        return None
    mode = (clock or CLOCK_BROKER).strip().lower()
    if mode == CLOCK_IST:
        return broker_to_ist(dt, broker_utc_offset_hours)
    return dt


def classify_hour(hour: int) -> str:
    h = int(hour) % 24
    if h < ASIAN_END_HOUR:
        return SESSION_ASIAN
    if h < LONDON_END_HOUR:
        return SESSION_LONDON
    return SESSION_US


def classify_session(
    dt: Optional[datetime],
    *,
    clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: Optional[float] = None,
) -> str:
    """Return Asian | London | US from entry bar datetime on the chosen clock."""
    if dt is None:
        return SESSION_ASIAN
    local = entry_clock_datetime(
        dt, clock=clock, broker_utc_offset_hours=broker_utc_offset_hours,
    )
    if local is None:
        return SESSION_ASIAN
    return classify_hour(local.hour)


def parse_hhmm(text: str, default_minutes: int = 0) -> int:
    """Parse 'HH:MM' or 'H:MM' into minutes since midnight (0–1439)."""
    raw = (text or "").strip()
    if not raw:
        return int(default_minutes) % (24 * 60)
    try:
        if ":" in raw:
            hh_s, mm_s = raw.split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
        else:
            hh, mm = int(raw), 0
        hh = max(0, min(23, hh))
        mm = max(0, min(59, mm))
        return hh * 60 + mm
    except (TypeError, ValueError):
        return int(default_minutes) % (24 * 60)


def format_hhmm(minutes: int) -> str:
    m = int(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def in_time_window(
    dt: Optional[datetime],
    start_hhmm: str,
    end_hhmm: str,
    *,
    clock: str = CLOCK_IST,
    broker_utc_offset_hours: Optional[float] = None,
) -> bool:
    """
    True if dt (on chosen clock) falls in [start, end] inclusive by minute.
    Supports overnight windows (e.g. 22:00–06:00).
    """
    local = entry_clock_datetime(
        dt, clock=clock, broker_utc_offset_hours=broker_utc_offset_hours,
    )
    if local is None:
        return False
    cur = local.hour * 60 + local.minute
    start = parse_hhmm(start_hhmm, 0)
    end = parse_hhmm(end_hhmm, 23 * 60 + 59)
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def session_polars_expr(
    entry_col: str = "entry_time",
    *,
    clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: float = BROKER_UTC_OFFSET_WINTER,
) -> pl.Expr:
    """
    Vectorized session label from a datetime column.
    Broker mode (default for Results By Session) uses .dt.hour() directly.
    """
    mode = (clock or CLOCK_BROKER).strip().lower()
    if mode == CLOCK_IST:
        shift_min = int(round((IST_UTC_OFFSET_HOURS - float(broker_utc_offset_hours)) * 60))
        hour = (pl.col(entry_col) + pl.duration(minutes=shift_min)).dt.hour()
    else:
        hour = pl.col(entry_col).dt.hour()
    return (
        pl.when(hour < ASIAN_END_HOUR)
        .then(pl.lit(SESSION_ASIAN))
        .when(hour < LONDON_END_HOUR)
        .then(pl.lit(SESSION_LONDON))
        .otherwise(pl.lit(SESSION_US))
        .alias("session")
    )


def add_session_column(
    df: pl.DataFrame,
    entry_col: str = "entry_time",
    *,
    clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: float = BROKER_UTC_OFFSET_WINTER,
) -> pl.DataFrame:
    if df.height == 0:
        return df.with_columns(pl.lit(None).cast(pl.Utf8).alias("session"))
    if "session" in df.columns:
        return df
    return df.with_columns(
        session_polars_expr(
            entry_col, clock=clock, broker_utc_offset_hours=broker_utc_offset_hours,
        )
    )


def describe_sessions(
    *,
    clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: Optional[float] = None,
) -> str:
    clock_label = IST_TIME_LABEL if (clock or "").lower() == CLOCK_IST else BROKER_TIME_LABEL
    parts = [f"{name} ({start}–{end})" for name, start, end in SESSION_WINDOWS]
    extra = ""
    if (clock or "").lower() == CLOCK_IST:
        extra = " [IC Markets → IST auto GMT+2/GMT+3]"
    return f"Sessions (entry bar, {clock_label}{extra}): " + " · ".join(parts)


def apply_session_and_time_filters(
    signals,
    *,
    sessions_enabled: Optional[List[str]] = None,
    session_clock: str = CLOCK_BROKER,
    broker_utc_offset_hours: Optional[float] = None,
    ist_time_filter_enabled: bool = False,
    ist_time_start: str = "00:00",
    ist_time_end: str = "23:59",
) -> Tuple[int, int]:
    """
    Mutate signals in place: set ignored for session / IST time window misses.
    Returns (session_skipped, ist_time_skipped).

    IST conversion uses per-bar IC Markets offset (GMT+2/GMT+3) when
    broker_utc_offset_hours is None.
    """
    enabled = set(sessions_enabled or SESSION_ORDER)
    session_skipped = 0
    ist_skipped = 0
    clock = (session_clock or CLOCK_BROKER).strip().lower()

    filter_sessions = enabled != set(SESSION_ORDER)
    filter_ist = bool(ist_time_filter_enabled)

    if not filter_sessions and not filter_ist:
        return 0, 0

    for sig in signals:
        if getattr(sig, "ignored", False):
            continue
        ts = sig.entry_candle.timestamp

        if filter_sessions:
            sess = classify_session(
                ts, clock=clock, broker_utc_offset_hours=broker_utc_offset_hours,
            )
            if sess not in enabled:
                sig.ignored = True
                clock_tag = "IST" if clock == CLOCK_IST else "broker"
                sig.ignore_reason = f"Session filter ({sess} not enabled, {clock_tag} clock)"
                session_skipped += 1
                continue

        if filter_ist:
            # Always auto IC Markets offset for IST window (None)
            if not in_time_window(
                ts, ist_time_start, ist_time_end,
                clock=CLOCK_IST,
                broker_utc_offset_hours=None,
            ):
                ist_local = broker_to_ist(ts, None)
                hm = ist_local.strftime("%H:%M") if ist_local else "?"
                sig.ignored = True
                sig.ignore_reason = (
                    f"IST time filter ({hm} outside {ist_time_start}–{ist_time_end} IST)"
                )
                ist_skipped += 1

    return session_skipped, ist_skipped
