"""
live.py — Live strategy loop: poll MT5, run hammer/doji rules, optional orders.

Uses the same strategy / indicator config objects as the backtest dashboard.
Risk limits are enforced only from LiveRunConfig (Live tab), not backtest settings.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, List, Optional, Set, Tuple

import polars as pl

import doji_logic
import hammer_context_logic
import logic
import sessions
import telegram_notify
from broker import MT5Broker, TIMEFRAME_MT5_MAP
from indicators.filter import apply_indicator_filters, verify_signal_passes_indicators_at_bar
from live_journal import append_session_header, append_trade_row, session_log_path, trades_csv_path


LogFn = Callable[[str], None]

MAX_CONSECUTIVE_POLL_ERRORS = 25
SIGNAL_COMPUTE_TIMEOUT_SEC = 90.0


def _is_frozen_build() -> bool:
    """True when running as a PyInstaller one-file/one-dir exe (Ray must stay off)."""
    return bool(getattr(sys, "frozen", False))


def summarize_strategy_params(strategy_config, timeframe_label: str, pattern_type: str) -> str:
    """One-line snapshot of logic parameters used for signals (matches backtest config)."""
    tf_settings = getattr(strategy_config, "timeframe_settings", None) or {}
    tf_set = tf_settings.get(timeframe_label)
    if tf_set is not None:
        rr = getattr(tf_set, "rr_multiple", "?")
        max_sl = getattr(tf_set, "max_sl_usd", "?")
        tf_part = f"TF {timeframe_label}: RR={rr} max_SL=${max_sl}"
    else:
        tf_part = f"TF {timeframe_label}: (no RR/SL in config)"

    entry_rule = getattr(strategy_config, "entry_rule", None)
    entry_off = getattr(strategy_config, "entry_offset", 0.0)
    buf_mode = getattr(strategy_config, "buffer_mode", None)
    sl_pct = getattr(strategy_config, "sl_buffer_pct", None)
    risk_on = getattr(strategy_config, "enable_risk_limit", True)

    parts = [
        "[PARAMS]",
        tf_part,
        f"risk_limit={'on' if risk_on else 'off'}",
    ]

    if pattern_type == "doji":
        parts.append(doji_logic.describe_doji_detection(strategy_config))
        parts.append(doji_logic.describe_doji_entry_exit(strategy_config))
    elif pattern_type in ("hammer_with_candles", "hammer_context"):
        parts.append(hammer_context_logic.describe_hammer_context_rules(strategy_config))
        parts.append(hammer_context_logic.describe_hammer_context_entry_exit(strategy_config))
    else:
        parts.append(logic.describe_hammer_direction_matrix(strategy_config))
        parts.append(logic.describe_hammer_detection(strategy_config))
        parts.append(logic.describe_hammer_entry_exit(strategy_config))
    return " | ".join(str(p) for p in parts)


@dataclass
class LiveRunConfig:
    symbol: str
    timeframe_label: str  # 1h, 30m, ...
    volume: float
    magic: int
    max_open_positions: int
    poll_interval_sec: float
    dry_run: bool = True
    history_bars: int = 400
    # Safety limits — real orders are blocked unless these are satisfied (see validate_risk_config)
    max_daily_trades: int = 10
    max_daily_loss_usd: float = 100.0
    min_minutes_between_trades: float = 0.0
    max_spread_points: float = 50.0
    demo_accounts_only: bool = False
    max_lot_size: float = 0.10
    use_thread_pool_signal_cpu: bool = True
    use_ray_if_available: bool = False
    journal_dir: str = ""  # output/live — session log + live_trades.csv
    order_mode: str = "market"  # market | limit_entry | limit_offset
    limit_offset_points: float = 0.0
    price_deviation_points: int = 20
    order_comment: str = "HammerDashboard"
    fallback_to_market_on_limit_fail: bool = False
    # If strategy entry (candle open) is farther than this from bid/ask, use market + re-anchored SL/TP
    max_entry_deviation_points: float = 200.0
    # limit_offset: base limit price on current bid/ask instead of strategy entry (live-friendly)
    limit_offset_from_market: bool = True
    # Same Asian/London/US gate as backtest Timeframes tab (server/broker or IST clock)
    sessions_enabled: Optional[List[str]] = None
    session_clock: str = "broker"
    broker_utc_offset_hours: Optional[float] = None  # None = auto IC Markets by bar date
    ist_time_filter_enabled: bool = False
    ist_time_start: str = "00:00"
    ist_time_end: str = "23:59"
    # Telegram alerts (single chat — bot token + chat id from @BotFather / @userinfobot)
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def reanchor_sl_tp_to_fill(
    sig: logic.TradeSignal,
    fill_price: float,
) -> Tuple[float, float]:
    """Keep the same $ risk/reward distances as the signal, anchored to the actual fill price."""
    delta = float(fill_price) - float(sig.entry_price)
    return float(sig.stop_loss) + delta, float(sig.target) + delta


def validate_risk_config(cfg: LiveRunConfig, *, real_money: bool) -> Optional[str]:
    """Return error message if risk settings are insufficient for real orders."""
    if cfg.dry_run or not real_money:
        return None
    if cfg.max_daily_trades <= 0:
        return "Set Max trades / day to a positive number before live orders."
    if cfg.max_daily_loss_usd <= 0:
        return "Set Max daily loss ($) before live orders — required for real accounts."
    if cfg.max_lot_size <= 0 and cfg.volume <= 0:
        return "Set Lot size and/or Max lot cap before live orders."
    if cfg.volume > 0 and cfg.max_lot_size > 0 and cfg.volume > cfg.max_lot_size:
        return "Lot size is above Max lot cap — lower lot size or raise the cap intentionally."
    return None


def _candles_to_polars(closed: List[logic.Candle], forming: logic.Candle) -> pl.DataFrame:
    rows = []
    for c in closed + [forming]:
        rows.append({
            "datetime": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            # Real MT5 tick volume — VWAP is wrong without it.
            "volume": float(getattr(c, "volume", 0.0) or 0.0),
        })
    return pl.DataFrame(rows)


def describe_candle(c: logic.Candle) -> str:
    """Human-readable bar snapshot for the live log (color decides BUY/SELL)."""
    if c.close > c.open:
        color = "GREEN"
    elif c.close < c.open:
        color = "RED"
    else:
        color = "DOJI"
    return (
        f"{c.timestamp} O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} "
        f"C={c.close:.2f} ({color})"
    )


def indicator_snapshot_text(
    closed: List[logic.Candle],
    forming: logic.Candle,
    indicator_stack,
) -> str:
    """
    SuperTrend / VWAP values on the LAST CLOSED bar, exactly as the filter
    sees them. Logged each bar so users can compare with their MT5 chart
    (TradingView uses a different data feed and may disagree).
    """
    if not indicator_stack.enabled_indicator_ids() or len(closed) < 3:
        return ""
    try:
        import numpy as np

        from indicators.supertrend import compute_supertrend
        from indicators.vwap import compute_vwap

        df = _candles_to_polars(closed, forming)
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        vol = df["volume"].to_numpy()
        ts = df["datetime"].to_list()
        idx = len(closed) - 1  # last closed bar (signal bar)
        c = float(close[idx])
        parts: List[str] = []

        st_cfg = indicator_stack.supertrend
        if st_cfg.enabled:
            st_line, st_dir = compute_supertrend(
                high, low, close,
                atr_period=st_cfg.atr_period, multiplier=st_cfg.multiplier,
            )
            line, d = float(st_line[idx]), int(st_dir[idx])
            if np.isnan(line):
                parts.append(f"SuperTrend({st_cfg.atr_period},{st_cfg.multiplier}): warmup (not enough bars)")
            else:
                state = "GREEN (bullish)" if d == 1 else "RED (bearish)"
                # Walk back to the last flip so users can compare the flip
                # time with their chart (feeds can flip minutes apart).
                flip_i = idx
                while flip_i > 0 and st_dir[flip_i - 1] == d:
                    flip_i -= 1
                since = f" since {ts[flip_i]}" if flip_i > 0 else ""
                parts.append(
                    f"SuperTrend({st_cfg.atr_period},{st_cfg.multiplier})={state}{since} "
                    f"line={line:.2f} close={'above' if c > line else 'below'}"
                )

        if indicator_stack.vwap.enabled:
            v = float(compute_vwap(high, low, close, vol, timestamps=ts)[idx])
            if not np.isnan(v):
                parts.append(f"VWAP={v:.2f} close={'above' if c > v else 'below'}")

        return " | ".join(parts)
    except Exception:
        return ""


def _signal_key(sig: logic.TradeSignal) -> Tuple:
    return (sig.hammer_candle.timestamp, sig.direction.value, sig.timeframe)


def find_bar_signal_outcome(
    closed: List[logic.Candle],
    forming: logic.Candle,
    timeframe_logic_label: str,
    pattern_type: str,
    strategy_config,
    indicator_stack,
    sessions_enabled: Optional[List[str]] = None,
    session_clock: str = "broker",
    broker_utc_offset_hours: Optional[float] = None,
    ist_time_filter_enabled: bool = False,
    ist_time_start: str = "00:00",
    ist_time_end: str = "23:59",
) -> Tuple[Optional[logic.TradeSignal], Optional[logic.TradeSignal]]:
    """
    Returns (actionable_signal, ignored_on_this_bar).
    ignored_on_this_bar is set when a pattern matched the last closed bar but was filtered.
    """
    if pattern_type in ("hammer_with_candles", "hammer_context"):
        try:
            lookback = max(0, int(getattr(strategy_config, "lookback_candles", 5)))
        except (TypeError, ValueError):
            lookback = 5
        if len(closed) < lookback + 1:
            return None, None
    elif len(closed) < 3:
        return None, None

    extended = closed + [forming]
    if pattern_type == "doji":
        signals = doji_logic.run_strategy(
            extended, timeframe=timeframe_logic_label, config=strategy_config,
        )
    elif pattern_type in ("hammer_with_candles", "hammer_context"):
        signals = hammer_context_logic.run_strategy(
            extended, timeframe=timeframe_logic_label, config=strategy_config,
        )
    else:
        signals = logic.run_strategy(
            extended, timeframe=timeframe_logic_label, config=strategy_config,
        )

    if indicator_stack.enabled_indicator_ids():
        df = _candles_to_polars(closed, forming)
        signals = apply_indicator_filters(signals, df, indicator_stack)

    sessions.apply_session_and_time_filters(
        signals,
        sessions_enabled=sessions_enabled,
        session_clock=session_clock,
        broker_utc_offset_hours=broker_utc_offset_hours,
        ist_time_filter_enabled=ist_time_filter_enabled,
        ist_time_start=ist_time_start,
        ist_time_end=ist_time_end,
    )

    signal_bar_ts = closed[-1].timestamp
    entry_bar_ts = forming.timestamp

    ignored_match: Optional[logic.TradeSignal] = None
    for sig in reversed(signals):
        if sig.hammer_candle.timestamp != signal_bar_ts:
            continue
        if sig.entry_candle.timestamp != entry_bar_ts:
            continue
        if sig.ignored:
            ignored_match = sig
            continue
        if pattern_type != "doji" and pattern_type not in ("hammer_with_candles", "hammer_context"):
            ok, vmsg = logic.verify_hammer_trade_signal(sig, strategy_config)
            if not ok:
                sig = logic.TradeSignal(
                    direction=sig.direction,
                    hammer_candle=sig.hammer_candle,
                    entry_candle=sig.entry_candle,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    risk=sig.risk,
                    rr_multiple=sig.rr_multiple,
                    target=sig.target,
                    timeframe=sig.timeframe,
                    ignored=True,
                    ignore_reason=vmsg,
                    pattern_variant=getattr(sig, "pattern_variant", None),
                )
                ignored_match = sig
                continue
        return sig, None
    return None, ignored_match


def find_actionable_signal(
    closed: List[logic.Candle],
    forming: logic.Candle,
    timeframe_logic_label: str,
    pattern_type: str,
    strategy_config,
    indicator_stack,
    sessions_enabled: Optional[List[str]] = None,
    session_clock: str = "broker",
    broker_utc_offset_hours: Optional[float] = None,
    ist_time_filter_enabled: bool = False,
    ist_time_start: str = "00:00",
    ist_time_end: str = "23:59",
) -> Optional[logic.TradeSignal]:
    """Signal on last closed bar with entry on the forming bar.

    timeframe_logic_label must match strategy config keys (1m, 1h, …), not MT5 folder names (1min, 1hour).
    """
    actionable, _ignored = find_bar_signal_outcome(
        closed, forming, timeframe_logic_label, pattern_type, strategy_config, indicator_stack,
        sessions_enabled=sessions_enabled,
        session_clock=session_clock,
        broker_utc_offset_hours=broker_utc_offset_hours,
        ist_time_filter_enabled=ist_time_filter_enabled,
        ist_time_start=ist_time_start,
        ist_time_end=ist_time_end,
    )
    return actionable


class LiveTradingEngine:
    """Blocking poll loop — run on a dedicated QThread (never on the UI thread)."""

    def __init__(
        self,
        broker: MT5Broker,
        live_config: LiveRunConfig,
        pattern_type: str,
        pattern_label: str,
        strategy_config,
        indicator_stack,
        log: LogFn,
    ):
        self.broker = broker
        self.live_config = live_config
        self.pattern_type = pattern_type
        self.pattern_label = pattern_label
        self.strategy_config = strategy_config
        self.indicator_stack = indicator_stack
        self.log = log
        self._journal_dir = (live_config.journal_dir or "").strip()
        if self._journal_dir:
            os.makedirs(self._journal_dir, exist_ok=True)
        self._stop = False
        self._last_closed_bar_ts: Optional[object] = None
        self._active_symbol: str = (live_config.symbol or "").strip()
        self._traded_keys: Set[Tuple] = set()
        self._trades_today: int = 0
        self._calendar_day: Optional[date] = None
        self._session_start_equity: Optional[float] = None
        self._last_order_time: float = 0.0
        self._broker_lock = threading.RLock()
        self._strategy_lock = threading.RLock()
        self._consecutive_errors = 0
        self.last_heartbeat_mono: float = time.monotonic()
        self._poll_ticks: int = 0
        self._last_watch_log_mono: float = 0.0
        self._executor: Optional[ThreadPoolExecutor] = None
        self._ray_remote = None
        if live_config.use_thread_pool_signal_cpu:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-signal")
        if live_config.use_ray_if_available:
            self._try_init_ray()
        self._telegram = telegram_notify.TelegramNotifier.from_live_config(live_config, log)

    def _try_init_ray(self):
        if _is_frozen_build():
            self.log(
                "[LIVE] Ray disabled in packaged exe — it spawns console workers that "
                "freeze/crash the app. Thread pool is used instead."
            )
            return
        try:
            import psutil  # noqa: F401 — required by Ray
            import ray  # type: ignore

            if not ray.is_initialized():
                ray.init(
                    ignore_reinit_error=True,
                    num_cpus=2,
                    include_dashboard=False,
                    logging_level="ERROR",
                )

            @ray.remote
            def _ray_find(
                closed, forming, logic_tf, pattern_type, strategy_config, indicator_stack,
                sessions_enabled=None,
                session_clock="broker",
                broker_utc_offset_hours=None,
                ist_time_filter_enabled=False,
                ist_time_start="00:00",
                ist_time_end="23:59",
            ):
                return find_bar_signal_outcome(
                    closed, forming, logic_tf, pattern_type, strategy_config, indicator_stack,
                    sessions_enabled=sessions_enabled,
                    session_clock=session_clock,
                    broker_utc_offset_hours=broker_utc_offset_hours,
                    ist_time_filter_enabled=ist_time_filter_enabled,
                    ist_time_start=ist_time_start,
                    ist_time_end=ist_time_end,
                )

            self._ray_remote = _ray_find
            self.log("[LIVE] Ray optional CPU worker enabled (falls back to threads if Ray fails).")
        except ImportError as e:
            self.log(f"[WARN] Ray not used (missing dependency, e.g. psutil): {e}")
        except Exception as e:
            self.log(f"[WARN] Ray not used: {e}")

    def request_stop(self):
        self._stop = True

    def update_runtime_strategy(
        self,
        strategy_config,
        indicator_stack,
        pattern_type: str,
        pattern_label: str,
        sessions_enabled: Optional[List[str]] = None,
        session_clock: Optional[str] = None,
        broker_utc_offset_hours: Optional[float] = None,
        ist_time_filter_enabled: Optional[bool] = None,
        ist_time_start: Optional[str] = None,
        ist_time_end: Optional[str] = None,
        telegram_enabled: Optional[bool] = None,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ) -> None:
        """Call from the UI thread after parameter changes — no need to Stop/Start live."""
        with self._strategy_lock:
            self.strategy_config = strategy_config
            self.indicator_stack = indicator_stack
            self.pattern_type = pattern_type
            self.pattern_label = pattern_label
            if sessions_enabled is not None:
                self.live_config.sessions_enabled = list(sessions_enabled)
            if session_clock is not None:
                self.live_config.session_clock = session_clock
            # None = auto IC Markets GMT+2/GMT+3 by bar date
            self.live_config.broker_utc_offset_hours = broker_utc_offset_hours
            if ist_time_filter_enabled is not None:
                self.live_config.ist_time_filter_enabled = bool(ist_time_filter_enabled)
            if ist_time_start is not None:
                self.live_config.ist_time_start = str(ist_time_start)
            if ist_time_end is not None:
                self.live_config.ist_time_end = str(ist_time_end)
            if telegram_enabled is not None:
                self.live_config.telegram_enabled = bool(telegram_enabled)
            if telegram_bot_token is not None:
                self.live_config.telegram_bot_token = str(telegram_bot_token)
            if telegram_chat_id is not None:
                self.live_config.telegram_chat_id = str(telegram_chat_id)
            self._telegram.update(
                enabled=self.live_config.telegram_enabled,
                bot_token=self.live_config.telegram_bot_token,
                chat_id=self.live_config.telegram_chat_id,
            )
        extra = ""
        try:
            if hasattr(strategy_config, "lookback_candles"):
                extra = (
                    f"{hammer_context_logic.describe_hammer_context_rules(strategy_config)} | "
                    f"{hammer_context_logic.describe_hammer_context_entry_exit(strategy_config)} | "
                )
            elif hasattr(strategy_config, "classic_green"):
                extra = (
                    f"{logic.describe_hammer_direction_matrix(strategy_config)} | "
                    f"{logic.describe_hammer_entry_exit(strategy_config)} | "
                )
        except Exception:
            extra = ""
        self.log(
            f"[LIVE] Parameters refreshed — {pattern_label} | {extra}"
            f"indicators: {', '.join(indicator_stack.enabled_indicator_ids()) or 'none'}"
        )
        try:
            self.log(summarize_strategy_params(
                strategy_config, self.live_config.timeframe_label, pattern_type,
            ))
        except Exception:
            pass

    def update_telegram_settings(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
    ) -> None:
        """Lightweight Telegram-only refresh — safe from the notifications dialog."""
        self.live_config.telegram_enabled = bool(enabled)
        self.live_config.telegram_bot_token = str(bot_token or "")
        self.live_config.telegram_chat_id = str(chat_id or "")
        self._telegram.update(
            enabled=self.live_config.telegram_enabled,
            bot_token=self.live_config.telegram_bot_token,
            chat_id=self.live_config.telegram_chat_id,
        )
        state = "on" if self._telegram.enabled else "off"
        self.log(f"[TELEGRAM] Alerts {state}.")

    def _record_trade_event(
        self,
        event: str,
        sig: logic.TradeSignal,
        *,
        volume: float = 0.0,
        dry_run: bool = False,
        mt5_order_id: Optional[int] = None,
        mt5_message: str = "",
        order_mode: str = "",
    ) -> None:
        append_trade_row(
            self._journal_dir,
            {
                "logged_at": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "symbol": self._active_symbol or self.live_config.symbol,
                "timeframe": self.live_config.timeframe_label,
                "pattern": self.pattern_label,
                "direction": sig.direction.value,
                "volume": volume,
                "entry_price": round(sig.entry_price, 5),
                "stop_loss": round(sig.stop_loss, 5),
                "target": round(sig.target, 5),
                "risk": round(sig.risk, 5),
                "dry_run": "yes" if dry_run else "no",
                "order_mode": order_mode or self.live_config.order_mode,
                "mt5_order_id": mt5_order_id or "",
                "mt5_message": mt5_message,
                "signal_bar_time": str(sig.hammer_candle.timestamp),
                "entry_bar_time": str(sig.entry_candle.timestamp),
            },
        )
        self._telegram.notify_order_event(
            event,
            symbol=self._active_symbol or self.live_config.symbol,
            timeframe=self.live_config.timeframe_label,
            pattern=self.pattern_label,
            direction=sig.direction.value,
            volume=volume,
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            target=sig.target,
            dry_run=dry_run,
            order_mode=order_mode or self.live_config.order_mode,
            mt5_order_id=mt5_order_id,
            mt5_message=mt5_message,
        )

    def touch_heartbeat(self):
        self.last_heartbeat_mono = time.monotonic()

    def run(self):
        try:
            self._run_impl()
        except Exception as e:
            self.log(f"[FATAL] Live loop stopped: {e}\n{traceback.format_exc()}")
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
            if self._ray_remote is not None:
                try:
                    import ray  # type: ignore

                    if ray.is_initialized():
                        ray.shutdown()
                except Exception:
                    pass
                self._ray_remote = None
            self.touch_heartbeat()
            self.log("[LIVE] Worker exited.")

    def _run_impl(self):
        cfg = self.live_config
        if cfg.timeframe_label not in TIMEFRAME_MT5_MAP:
            self.log(f"[ERROR] Unknown live timeframe label: {cfg.timeframe_label}")
            return

        if not self.broker.is_connected:
            self.log("[ERROR] MT5 not connected — connect in Live panel before Start.")
            return

        with self._broker_lock:
            ok_term, term_msg = self.broker.terminal_allows_trading()
        if not ok_term:
            self.log(f"[ERROR] {term_msg}")
            return

        with self._broker_lock:
            ok, msg, resolved = self.broker.resolve_and_ensure_symbol(cfg.symbol)
        if not ok:
            self.log(f"[ERROR] {msg}")
            return
        self._active_symbol = resolved
        if resolved != (cfg.symbol or "").strip():
            self.log(f"[LIVE] Broker symbol resolved: {cfg.symbol!r} → {resolved!r}")

        with self._broker_lock:
            is_demo = self.broker.account_is_demo()
        real_money = is_demo is False

        if cfg.demo_accounts_only and real_money:
            self.log("[SAFETY] Demo-only mode is on — refusing to run on a LIVE/real account.")
            return

        risk_err = validate_risk_config(cfg, real_money=real_money)
        if risk_err and not cfg.dry_run:
            self.log(f"[SAFETY] {risk_err}")
            return

        if real_money and not cfg.dry_run:
            self.log(
                "[SAFETY] LIVE account — orders use Live tab limits only: "
                f"max {cfg.max_daily_trades} trades/day, "
                f"max ${cfg.max_daily_loss_usd:.2f} daily loss, "
                f"spread≤{cfg.max_spread_points} pts, "
                f"lots≤{cfg.max_lot_size or cfg.volume}."
            )
        elif is_demo is True:
            self.log("[LIVE] Demo account — same risk limits apply when dry run is off.")

        with self._broker_lock:
            info = self.broker.account_info_dict()
        self._session_start_equity = float(info.get("equity") or info.get("balance") or 0.0)
        self._reset_daily_counters_if_needed()

        enabled_inds = self.indicator_stack.enabled_indicator_ids()
        inds = ", ".join(enabled_inds) or "none"
        if not enabled_inds:
            self.log(
                "[WARN] No indicator filters active — SuperTrend/VWAP will NOT gate trades. "
                "To use them: Backtest Parameters → Indicators → Add, then Stop and Start live again."
            )
        if self.indicator_stack.supertrend.enabled and not self.indicator_stack.supertrend.apply_trade_filter:
            self.log(
                "[WARN] SuperTrend is added but 'Apply trade filter' is UNCHECKED — "
                "it will NOT block any trades. Check the box in Indicators and restart live."
            )
        if self.indicator_stack.vwap.enabled and not self.indicator_stack.vwap.apply_trade_filter:
            self.log(
                "[WARN] VWAP is added but 'Apply trade filter' is UNCHECKED — "
                "it will NOT block any trades. Check the box in Indicators and restart live."
            )
        if self._journal_dir:
            self.log(f"[LIVE] Journal folder: {self._journal_dir}")
            self.log(f"[LIVE] Session log: {session_log_path(self._journal_dir)}")
            self.log(f"[LIVE] Trades CSV: {trades_csv_path(self._journal_dir)}")
            append_session_header(
                self._journal_dir,
                f"Live session — {self.pattern_label} on {self._active_symbol} "
                f"@ {cfg.timeframe_label} magic={cfg.magic}",
            )
        else:
            self.log("[WARN] journal_dir not set — live_trades.csv will not be written.")

        self.log(
            f"[LIVE] MT5 account {info.get('login', '?')} @ {info.get('server', '?')} "
            f"({info.get('currency', '')})"
        )
        self.log(
            f"[LIVE] Strategy: {self.pattern_label} ({self.pattern_type}) | "
            f"Indicators: {inds} | {self._active_symbol} {cfg.timeframe_label} | "
            f"lots={cfg.volume} | dry_run={cfg.dry_run} | order={cfg.order_mode}"
        )
        if self.pattern_type == "hammer":
            self.log(
                f"[LIVE] Direction locked for this session: "
                f"{logic.describe_hammer_direction_matrix(self.strategy_config)} "
                f"(signal bar shape+color; entry is next bar). "
                f"Change Parameters then use 'Apply to live' or Stop/Start live."
            )
        elif self.pattern_type in ("hammer_with_candles", "hammer_context"):
            self.log(
                f"[LIVE] Context rules: "
                f"{hammer_context_logic.describe_hammer_context_rules(self.strategy_config)}"
            )
        self.log(summarize_strategy_params(
            self.strategy_config, cfg.timeframe_label, self.pattern_type,
        ))

        while not self._stop:
            self.touch_heartbeat()
            try:
                self._poll_once(cfg.timeframe_label)
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                self.log(f"[ERROR] Poll failed ({self._consecutive_errors}): {e}")
                if self._consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    self.log("[FATAL] Too many consecutive errors — stopping live loop to protect the app.")
                    break
            time.sleep(max(0.5, cfg.poll_interval_sec))

        self.log("[LIVE] Stopped.")

    def _reset_daily_counters_if_needed(self):
        today = date.today()
        if self._calendar_day != today:
            self._calendar_day = today
            self._trades_today = 0
            with self._broker_lock:
                info = self.broker.account_info_dict()
            self._session_start_equity = float(info.get("equity") or info.get("balance") or 0.0)
            self.log(f"[SAFETY] Daily counters reset ({today.isoformat()}).")

    def _safety_blocks_order(self, sig: logic.TradeSignal) -> Optional[str]:
        cfg = self.live_config
        self._reset_daily_counters_if_needed()

        if sig.risk <= 0:
            return "Signal risk is zero or negative — order blocked."

        if cfg.max_daily_trades > 0 and self._trades_today >= cfg.max_daily_trades:
            return f"Max daily trades reached ({cfg.max_daily_trades})."

        if cfg.max_daily_loss_usd > 0 and self._session_start_equity is not None:
            with self._broker_lock:
                info = self.broker.account_info_dict()
            equity = float(info.get("equity") or 0.0)
            loss = self._session_start_equity - equity
            if loss >= cfg.max_daily_loss_usd:
                return f"Max daily loss reached (${loss:.2f} >= ${cfg.max_daily_loss_usd:.2f})."

        if cfg.min_minutes_between_trades > 0:
            elapsed_min = (time.monotonic() - self._last_order_time) / 60.0
            if self._last_order_time > 0 and elapsed_min < cfg.min_minutes_between_trades:
                return f"Cooldown active ({cfg.min_minutes_between_trades:.0f} min between trades)."

        if cfg.max_spread_points > 0:
            with self._broker_lock:
                spread = self.broker.spread_points(self._active_symbol or cfg.symbol)
            if spread is not None and spread > cfg.max_spread_points:
                return (
                    f"Spread too wide ({spread:.1f} pts > max {cfg.max_spread_points:.1f}). "
                    "Raise Max spread (pts) in Safety if intentional."
                )

        return None

    def _compute_signal_outcome(
        self,
        closed: List[logic.Candle],
        forming: logic.Candle,
        logic_tf: str,
    ) -> Tuple[Optional[logic.TradeSignal], Optional[logic.TradeSignal]]:
        with self._strategy_lock:
            args = (
                closed, forming, logic_tf, self.pattern_type,
                self.strategy_config, self.indicator_stack,
                self.live_config.sessions_enabled,
                getattr(self.live_config, "session_clock", "broker"),
                getattr(self.live_config, "broker_utc_offset_hours", None),
                bool(getattr(self.live_config, "ist_time_filter_enabled", False)),
                getattr(self.live_config, "ist_time_start", "00:00"),
                getattr(self.live_config, "ist_time_end", "23:59"),
            )
        if self._ray_remote is not None:
            try:
                import ray  # type: ignore

                return ray.get(self._ray_remote.remote(*args), timeout=SIGNAL_COMPUTE_TIMEOUT_SEC)
            except Exception as e:
                self.log(f"[WARN] Ray signal step failed, using thread pool: {e}")

        if self._executor is not None:
            fut = self._executor.submit(find_bar_signal_outcome, *args)
            try:
                return fut.result(timeout=SIGNAL_COMPUTE_TIMEOUT_SEC)
            except FuturesTimeoutError:
                self.log("[ERROR] Signal computation timed out.")
                return None, None

        return find_bar_signal_outcome(*args)

    def _compute_signal(
        self,
        closed: List[logic.Candle],
        forming: logic.Candle,
        logic_tf: str,
    ) -> Optional[logic.TradeSignal]:
        sig, _ignored = self._compute_signal_outcome(closed, forming, logic_tf)
        return sig

    def _resolve_live_order(
        self,
        sig: logic.TradeSignal,
        sym: str,
    ) -> Optional[Tuple[str, Optional[float], float, float]]:
        """
        Returns (order_mode, limit_price_or_none, sl, tp) for MT5.
        Re-anchors SL/TP to the actual fill/limit price; uses market when strategy entry is far from tick.
        """
        cfg = self.live_config
        with self._broker_lock:
            ticks = self.broker.get_tick_prices(sym)
            point = self.broker.symbol_point(sym)
        if not ticks:
            self.log("[ORDER] No tick data — cannot send order.")
            return None
        bid, ask = ticks
        is_buy = sig.direction == logic.TradeDirection.BUY
        market = ask if is_buy else bid
        strat_entry = float(sig.entry_price)
        pt = point or 0.01
        dev_pts = abs(strat_entry - market) / pt

        order_mode = (cfg.order_mode or "market").strip().lower()
        effective_mode = order_mode
        limit_price: Optional[float] = strat_entry

        max_dev = float(cfg.max_entry_deviation_points or 0)
        force_market_dev = max_dev > 0 and dev_pts > max_dev

        if order_mode == "market" or force_market_dev:
            if order_mode != "market" and force_market_dev:
                self.log(
                    f"[LIVE] Strategy entry {strat_entry:.2f} vs "
                    f"{'ask' if is_buy else 'bid'} {market:.2f} ({dev_pts:.0f} pts) — "
                    f"using MARKET (max deviation {max_dev:.0f} pts)."
                )
            effective_mode = "market"
            fill_ref = market
            sl, tp = reanchor_sl_tp_to_fill(sig, fill_ref)
            limit_price = None
        elif order_mode == "limit_offset":
            base = market if cfg.limit_offset_from_market else strat_entry
            with self._broker_lock:
                lp, _resolved = self.broker.limit_price_with_offset(
                    sym, sig.direction, base, cfg.limit_offset_points,
                )
            if lp is None:
                self.log("[ORDER] Could not compute limit price.")
                return None
            limit_price = lp
            fill_ref = limit_price
            sl, tp = reanchor_sl_tp_to_fill(sig, fill_ref)
            effective_mode = "limit_entry"
        else:
            fill_ref = strat_entry
            sl, tp = reanchor_sl_tp_to_fill(sig, fill_ref)
            limit_price = strat_entry

        side = "ask" if is_buy else "bid"
        self.log(
            f"[LIVE] Tick bid={bid:.2f} ask={ask:.2f} | strategy entry={strat_entry:.2f} | "
            f"exec @ {side} {market:.2f} → SL={sl:.2f} TP={tp:.2f}"
        )
        if effective_mode != "market" and limit_price is not None:
            self.log(f"[LIVE] Limit price {limit_price:.2f} (mode={effective_mode})")

        return effective_mode, limit_price, sl, tp

    def _fetch_rates_for_live(
        self,
        sym: str,
        timeframe_label: str,
        count: int,
    ):
        """
        Fetch OHLC from MT5. On a new closed bar, re-pull once after a short
        delay so MT5 can finalize the bar — closed history is never tick-merged.
        """
        with self._broker_lock:
            closed, forming, err, meta = self.broker.fetch_rates(sym, timeframe_label, count)
        if err or not closed:
            return closed, forming, err, meta

        prev_ts = self._last_closed_bar_ts
        last_ts = closed[-1].timestamp
        if prev_ts is None or last_ts == prev_ts:
            return closed, forming, err, meta

        time.sleep(0.15)
        with self._broker_lock:
            c2, f2, e2, m2 = self.broker.fetch_rates(sym, timeframe_label, count)
        if e2 or not c2 or c2[-1].timestamp != last_ts:
            return closed, forming, err, meta
        if abs(c2[-1].close - closed[-1].close) > 0.009:
            self.log(
                "[LIVE] Refreshed last closed bar from MT5 "
                f"(close {closed[-1].close:.2f} → {c2[-1].close:.2f})."
            )
        return c2, f2, e2, m2

    def _poll_once(self, config_timeframe_label: str):
        """config_timeframe_label: dashboard TF key (1m, 1h, …) matching strategy timeframe_settings."""
        cfg = self.live_config
        if not self.broker.is_connected:
            self.log("[WARN] MT5 disconnected — poll skipped. Reconnect and Start live again.")
            return
        sym = self._active_symbol or cfg.symbol
        closed, forming, err, rate_meta = self._fetch_rates_for_live(
            sym, cfg.timeframe_label, cfg.history_bars,
        )
        if err:
            self.log(f"[WARN] {sym}: {err}")
            return
        if not closed:
            return

        self._poll_ticks += 1
        stale = rate_meta.get("stale_warning")
        if stale:
            self.log(f"[WARN] {stale}")
        if rate_meta.get("forming_tick_merge_skipped"):
            self.log(
                "[WARN] Skipped tick merge on forming bar — bar time is stale "
                "(market break or no quotes). Waiting for fresh bars."
            )

        now_mono = time.monotonic()
        if now_mono - self._last_watch_log_mono >= 30.0:
            self._last_watch_log_mono = now_mono
            bid = rate_meta.get("bid")
            ask = rate_meta.get("ask")
            f_close = rate_meta.get("forming_close")
            lc = rate_meta.get("last_closed_close")
            lc_t = rate_meta.get("last_closed_time")
            tick_part = ""
            if bid is not None and ask is not None:
                tick_part = f" | tick bid={bid:.2f} ask={ask:.2f}"
            self.log(
                f"[LIVE] Data watch | last closed {lc_t} close={lc} | "
                f"forming close≈{f_close}{tick_part} | source={rate_meta.get('rates_source', '?')}"
            )
            gap = rate_meta.get("tick_vs_last_closed")
            if gap is not None and abs(float(gap)) > 20.0:
                self.log(
                    f"[LIVE] Tick vs last closed gap={float(gap):+.2f} — "
                    "expected while price moves in the OPEN 3m bar; "
                    "hammer/signals use the last CLOSED bar only (timestamps are UTC)."
                )

        last_ts = closed[-1].timestamp
        if self._last_closed_bar_ts is None:
            self._last_closed_bar_ts = last_ts
            self.log(f"[LIVE] Watching bars — last closed {describe_candle(closed[-1])}")
            with self._strategy_lock:
                ind_stack = self.indicator_stack
            snapshot = indicator_snapshot_text(closed, forming, ind_stack)
            if snapshot:
                self.log(
                    f"[LIVE] Indicators right now ({cfg.timeframe_label}, broker feed): {snapshot} — "
                    "Bar times are UTC (MT5 server). Compare with your M3 chart on the same symbol."
                )
            return

        if last_ts == self._last_closed_bar_ts:
            return

        self._last_closed_bar_ts = last_ts
        signal_bar = closed[-1]
        if not logic.candle_ohlc_valid(signal_bar):
            self.log(
                f"[WARN] Invalid OHLC on last closed bar — skipping signal check: "
                f"{describe_candle(signal_bar)}"
            )
            return

        # Log the broker's bar explicitly: this is the candle signals are based
        # on. It can differ from TradingView (different feed / bar boundaries).
        self.log(f"[LIVE] New closed bar (broker feed): {describe_candle(signal_bar)}")
        snapshot = indicator_snapshot_text(closed, forming, self.indicator_stack)
        if snapshot:
            self.log(f"[LIVE] Indicators on this bar ({cfg.timeframe_label}): {snapshot}")

        sig, ignored = self._compute_signal_outcome(closed, forming, config_timeframe_label)
        if sig is None:
            if ignored is not None and ignored.ignore_reason:
                variant = getattr(ignored, "pattern_variant", None) or "—"
                self.log(
                    f"[LIVE] Pattern on signal bar but not traded ({variant}): {ignored.ignore_reason}"
                )
                if self.pattern_type == "hammer":
                    self.log(
                        logic.explain_hammer_signal_direction(
                            ignored.hammer_candle,
                            ignored.direction,
                            self.strategy_config,
                            variant,
                        )
                    )
                self.log(
                    f"[LIVE] Entry would be on next bar: {describe_candle(forming)}"
                )
            else:
                self.log(
                    f"[LIVE] No {self.pattern_label} pattern on this closed bar — no trade. "
                    "(Indicators only FILTER pattern signals; SuperTrend/VWAP being bullish "
                    "never opens a trade by itself.)"
                )
                if self.pattern_type == "hammer":
                    self.log(
                        f"[LIVE] Hammer probe: "
                        f"{logic.describe_hammer_probe(signal_bar, self.strategy_config)}"
                    )
                    hr = logic.check_hammer(signal_bar, self.strategy_config)
                    if (
                        hr.hammer_variant is not None
                        and hr.direction is None
                        and hr.color in (logic.CandleColor.GREEN, logic.CandleColor.RED)
                    ):
                        action = logic.hammer_trade_action(
                            self.strategy_config, hr.hammer_variant, hr.color,
                        )
                        if action == logic.TradeAction.NO:
                            self.log(
                                f"[LIVE] {hr.hammer_variant.value} {hr.color.value} matched shape "
                                f"but Direction tab is NO — no order."
                            )
                elif self.pattern_type in ("hammer_with_candles", "hammer_context") and len(closed) >= 2:
                    ctx_cfg = self.strategy_config
                    idx = len(closed) - 1
                    signal_bar = closed[idx]
                    body_pct = hammer_context_logic.body_pct_of_candle(
                        signal_bar, getattr(ctx_cfg, "min_range", 1e-9),
                    )
                    buy_fail = hammer_context_logic.detect_buy_setup(closed, idx, ctx_cfg)
                    sell_fail = hammer_context_logic.detect_sell_setup(closed, idx, ctx_cfg)
                    self.log(
                        f"[LIVE] Signal bar body={body_pct:.1f}% of range "
                        f"(wick-off uses this vs Body tab cap)"
                    )
                    if buy_fail and sell_fail:
                        self.log(
                            f"[LIVE] Context probe: BUY — {buy_fail} | SELL — {sell_fail}"
                        )
            return

        key = _signal_key(sig)
        if key in self._traded_keys:
            self.log("[LIVE] Signal already acted on — skip.")
            return

        with self._broker_lock:
            open_count = self.broker.count_open_positions(sym, cfg.magic)
        if open_count >= cfg.max_open_positions:
            self.log(f"[LIVE] Max open positions ({cfg.max_open_positions}) — skip.")
            return

        # Hard gate (fail-closed): never send an order if indicators reject this bar.
        signal_bar_index = len(closed) - 1
        df_bars = _candles_to_polars(closed, forming)
        ind_ok, ind_reason = verify_signal_passes_indicators_at_bar(
            df_bars,
            signal_bar_index,
            sig.direction,
            self.indicator_stack,
        )
        if not ind_ok:
            self.log(
                f"[LIVE] Order blocked on {cfg.timeframe_label} — {ind_reason}"
            )
            allow_snapshot = indicator_snapshot_text(closed, forming, self.indicator_stack)
            if allow_snapshot:
                self.log(f"[LIVE] Indicators at signal bar: {allow_snapshot}")
            return

        variant = getattr(sig, "pattern_variant", None) or "—"
        self.log(
            f"[SIGNAL] {sig.direction.value} ({variant}) — hammer/signal bar: "
            f"{describe_candle(sig.hammer_candle)}"
        )
        if self.pattern_type == "hammer":
            self.log(logic.explain_hammer_signal_direction(
                sig.hammer_candle, sig.direction, self.strategy_config, variant,
            ))
            self.log(
                f"[SIGNAL] Entry/SL from {'inverted' if variant == 'INVERTED' else 'classic'} "
                f"row: {logic.entry_rule_label_for_variant(self.strategy_config, variant)}"
            )
        elif self.pattern_type in ("hammer_with_candles", "hammer_context"):
            ctx_cfg = self.strategy_config
            n = getattr(ctx_cfg, "lookback_candles", "?")
            body_pct = hammer_context_logic.body_pct_of_candle(
                sig.hammer_candle, getattr(ctx_cfg, "min_range", 1e-9),
            )
            if sig.direction == logic.TradeDirection.BUY:
                wick_note = (
                    "body only, prev red→BUY, signal color ignored"
                    if not getattr(ctx_cfg, "buy_require_wick", True)
                    else "classic wick required"
                )
                self.log(
                    f"[SIGNAL] Context BUY ({wick_note}, body={body_pct:.1f}%): "
                    f"{n} prior close(s) not below hammer low "
                    f"{sig.hammer_candle.low:.2f}"
                )
            else:
                wick_note = (
                    "body only, prev green→SELL, signal color ignored"
                    if not getattr(ctx_cfg, "sell_require_wick", True)
                    else "inverted wick required"
                )
                self.log(
                    f"[SIGNAL] Context SELL ({wick_note}, body={body_pct:.1f}%): "
                    f"{n} prior close(s) not above hammer high "
                    f"{sig.hammer_candle.high:.2f}"
                )
            sl_variant = (
                logic.HammerVariant.INVERTED
                if sig.direction == logic.TradeDirection.SELL
                else logic.HammerVariant.CLASSIC
            )
            trade_cfg = (
                ctx_cfg.to_buy_trade_config()
                if sig.direction == logic.TradeDirection.BUY
                else ctx_cfg.to_sell_trade_config()
            )
            self.log(
                f"[SIGNAL] Entry/SL row: "
                f"{logic.entry_rule_label_for_variant(trade_cfg, sl_variant)}"
            )
        self.log(
            f"[SIGNAL] Entry bar (next candle): {describe_candle(sig.entry_candle)} | "
            f"entry≈{sig.entry_price:.2f} SL={sig.stop_loss:.2f} TP={sig.target:.2f} TF={sig.timeframe}"
        )
        # Record WHY this trade was allowed: indicator values at decision time.
        allow_snapshot = indicator_snapshot_text(closed, forming, self.indicator_stack)
        if allow_snapshot:
            self.log(f"[SIGNAL] Allowed by indicators ({sym} {cfg.timeframe_label}): {allow_snapshot}")
        self._record_trade_event("SIGNAL", sig, volume=cfg.volume, dry_run=cfg.dry_run)

        if cfg.dry_run:
            self.log("[DRY RUN] Order not sent (uncheck Dry run on Live panel to place orders).")
            self._record_trade_event("DRY_RUN", sig, volume=cfg.volume, dry_run=True, mt5_message="dry_run")
            self._traded_keys.add(key)
            return

        block = self._safety_blocks_order(sig)
        if block:
            self.log(f"[SAFETY] Order blocked: {block}")
            self._record_trade_event(
                "SAFETY_BLOCK", sig, volume=cfg.volume, dry_run=False, mt5_message=block,
            )
            return

        volume = cfg.volume
        if cfg.max_lot_size > 0:
            volume = min(volume, cfg.max_lot_size)

        resolved = self._resolve_live_order(sig, sym)
        if resolved is None:
            return
        order_mode, limit_price, sl, tp = resolved

        self.log(f"[ORDER] Sending {sig.direction.value} {volume} lot(s) via {order_mode} on {sym}…")
        with self._broker_lock:
            ok, msg, order_id = self.broker.send_trade_order(
                sym,
                sig.direction,
                volume,
                sl,
                tp,
                cfg.magic,
                order_mode=order_mode,
                limit_price=limit_price if limit_price is not None else sig.entry_price,
                limit_offset_points=cfg.limit_offset_points,
                comment=cfg.order_comment,
                deviation=cfg.price_deviation_points,
            )
        if ok:
            self.log(f"[ORDER] {msg}")
            self._record_trade_event(
                "ORDER", sig, volume=volume, dry_run=False,
                mt5_order_id=order_id, mt5_message=msg, order_mode=order_mode,
            )
            self._traded_keys.add(key)
            self._trades_today += 1
            self._last_order_time = time.monotonic()
        elif (
            order_mode != "market"
            and cfg.fallback_to_market_on_limit_fail
        ):
            self.log(f"[ORDER FAIL] {msg}")
            self.log("[ORDER] Limit failed — trying one market fallback…")
            with self._broker_lock:
                ticks = self.broker.get_tick_prices(sym)
            if not ticks:
                self._record_trade_event(
                    "ORDER_FAIL", sig, volume=volume, dry_run=False, mt5_message=msg,
                )
                return
            bid, ask = ticks
            mkt = ask if sig.direction == logic.TradeDirection.BUY else bid
            sl_m, tp_m = reanchor_sl_tp_to_fill(sig, mkt)
            with self._broker_lock:
                ok, msg, order_id = self.broker.send_trade_order(
                    sym,
                    sig.direction,
                    volume,
                    sl_m,
                    tp_m,
                    cfg.magic,
                    order_mode="market",
                    limit_price=mkt,
                    comment=cfg.order_comment,
                    deviation=cfg.price_deviation_points,
                )
            if ok:
                self.log(f"[ORDER] Market fallback: {msg}")
                self._record_trade_event(
                    "ORDER", sig, volume=volume, dry_run=False,
                    mt5_order_id=order_id, mt5_message=f"fallback market: {msg}",
                )
                self._traded_keys.add(key)
                self._trades_today += 1
                self._last_order_time = time.monotonic()
            else:
                self.log(f"[ORDER FAIL] Market fallback: {msg}")
                self._record_trade_event(
                    "ORDER_FAIL", sig, volume=volume, dry_run=False, mt5_message=msg,
                )
        else:
            self.log(f"[ORDER FAIL] {msg}")
            self._record_trade_event(
                "ORDER_FAIL", sig, volume=volume, dry_run=False, mt5_message=msg,
            )
