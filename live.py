"""
live.py — Live strategy loop: poll MT5, run hammer/doji rules, optional orders.

Uses the same strategy / indicator config objects as the backtest dashboard.
Risk limits are enforced only from LiveRunConfig (Live tab), not backtest settings.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, List, Optional, Set, Tuple

import polars as pl

import doji_logic
import logic
from broker import MT5Broker, TIMEFRAME_MT5_MAP
from indicators.filter import apply_indicator_filters
from live_journal import append_trade_row


LogFn = Callable[[str], None]

MAX_CONSECUTIVE_POLL_ERRORS = 25
SIGNAL_COMPUTE_TIMEOUT_SEC = 90.0


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
            "volume": 0.0,
        })
    return pl.DataFrame(rows)


def _signal_key(sig: logic.TradeSignal) -> Tuple:
    return (sig.hammer_candle.timestamp, sig.direction.value, sig.timeframe)


def find_actionable_signal(
    closed: List[logic.Candle],
    forming: logic.Candle,
    timeframe_logic_label: str,
    pattern_type: str,
    strategy_config,
    indicator_stack,
) -> Optional[logic.TradeSignal]:
    """Signal on last closed bar with entry on the forming bar."""
    if len(closed) < 3:
        return None

    extended = closed + [forming]
    if pattern_type == "doji":
        signals = doji_logic.run_strategy(
            extended, timeframe=timeframe_logic_label, config=strategy_config,
        )
    else:
        signals = logic.run_strategy(
            extended, timeframe=timeframe_logic_label, config=strategy_config,
        )

    if indicator_stack.enabled_indicator_ids():
        df = _candles_to_polars(closed, forming)
        signals = apply_indicator_filters(signals, df, indicator_stack)

    signal_bar_ts = closed[-1].timestamp
    entry_bar_ts = forming.timestamp

    for sig in reversed(signals):
        if sig.ignored:
            continue
        if sig.hammer_candle.timestamp != signal_bar_ts:
            continue
        if sig.entry_candle.timestamp != entry_bar_ts:
            continue
        return sig
    return None


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
        self._stop = False
        self._last_closed_bar_ts: Optional[object] = None
        self._active_symbol: str = (live_config.symbol or "").strip()
        self._traded_keys: Set[Tuple] = set()
        self._trades_today: int = 0
        self._calendar_day: Optional[date] = None
        self._session_start_equity: Optional[float] = None
        self._last_order_time: float = 0.0
        self._broker_lock = threading.RLock()
        self._consecutive_errors = 0
        self.last_heartbeat_mono: float = time.monotonic()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._ray_remote = None
        if live_config.use_thread_pool_signal_cpu:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-signal")
        if live_config.use_ray_if_available:
            self._try_init_ray()

    def _try_init_ray(self):
        try:
            import ray  # type: ignore

            if not ray.is_initialized():
                ray.init(ignore_reinit_error=True, num_cpus=2, include_dashboard=False, logging_level="ERROR")

            @ray.remote
            def _ray_find(closed, forming, logic_tf, pattern_type, strategy_config, indicator_stack):
                return find_actionable_signal(
                    closed, forming, logic_tf, pattern_type, strategy_config, indicator_stack,
                )

            self._ray_remote = _ray_find
            self.log("[LIVE] Ray optional CPU worker enabled (falls back to threads if Ray fails).")
        except Exception as e:
            self.log(f"[WARN] Ray not used: {e}")

    def request_stop(self):
        self._stop = True

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
            self.touch_heartbeat()
            self.log("[LIVE] Worker exited.")

    def _run_impl(self):
        cfg = self.live_config
        _, logic_tf = TIMEFRAME_MT5_MAP[cfg.timeframe_label]

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

        inds = ", ".join(self.indicator_stack.enabled_indicator_ids()) or "none"
        if self._journal_dir:
            self.log(f"[LIVE] Journal folder: {self._journal_dir}")
        self.log(
            f"[LIVE] Strategy: {self.pattern_label} ({self.pattern_type}) | "
            f"Indicators: {inds} | {self._active_symbol} {cfg.timeframe_label} | "
            f"lots={cfg.volume} | dry_run={cfg.dry_run} | order={cfg.order_mode}"
        )

        while not self._stop:
            self.touch_heartbeat()
            try:
                self._poll_once(logic_tf)
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

    def _compute_signal(
        self,
        closed: List[logic.Candle],
        forming: logic.Candle,
        logic_tf: str,
    ) -> Optional[logic.TradeSignal]:
        args = (
            closed, forming, logic_tf, self.pattern_type,
            self.strategy_config, self.indicator_stack,
        )
        if self._ray_remote is not None:
            try:
                import ray  # type: ignore

                return ray.get(self._ray_remote.remote(*args), timeout=SIGNAL_COMPUTE_TIMEOUT_SEC)
            except Exception as e:
                self.log(f"[WARN] Ray signal step failed, using thread pool: {e}")

        if self._executor is not None:
            fut = self._executor.submit(find_actionable_signal, *args)
            try:
                return fut.result(timeout=SIGNAL_COMPUTE_TIMEOUT_SEC)
            except FuturesTimeoutError:
                self.log("[ERROR] Signal computation timed out.")
                return None

        return find_actionable_signal(*args)

    def _poll_once(self, logic_tf: str):
        cfg = self.live_config
        sym = self._active_symbol or cfg.symbol
        with self._broker_lock:
            closed, forming, err = self.broker.fetch_rates(
                sym, cfg.timeframe_label, cfg.history_bars,
            )
        if err:
            self.log(f"[WARN] {sym}: {err}")
            return
        if not closed:
            return

        last_ts = closed[-1].timestamp
        if self._last_closed_bar_ts is None:
            self._last_closed_bar_ts = last_ts
            self.log(f"[LIVE] Watching bars — last closed {last_ts}")
            return

        if last_ts == self._last_closed_bar_ts:
            return

        self._last_closed_bar_ts = last_ts
        self.log(f"[LIVE] New closed bar {last_ts}")

        sig = self._compute_signal(closed, forming, logic_tf)
        if sig is None:
            self.log("[LIVE] No new valid signal on this bar.")
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

        self.log(
            f"[SIGNAL] {sig.direction.value} entry≈{sig.entry_price:.2f} "
            f"SL={sig.stop_loss:.2f} TP={sig.target:.2f} TF={sig.timeframe}"
        )
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

        self.log(f"[ORDER] Sending {sig.direction.value} {volume} lot(s) via {cfg.order_mode} on {sym}…")
        with self._broker_lock:
            ok, msg, order_id = self.broker.send_trade_order(
                sym,
                sig.direction,
                volume,
                sig.stop_loss,
                sig.target,
                cfg.magic,
                order_mode=cfg.order_mode,
                limit_price=sig.entry_price,
                limit_offset_points=cfg.limit_offset_points,
                comment=cfg.order_comment,
                deviation=cfg.price_deviation_points,
            )
        if ok:
            self.log(f"[ORDER] {msg}")
            self._record_trade_event(
                "ORDER", sig, volume=volume, dry_run=False,
                mt5_order_id=order_id, mt5_message=msg,
            )
            self._traded_keys.add(key)
            self._trades_today += 1
            self._last_order_time = time.monotonic()
        elif (
            cfg.order_mode != "market"
            and cfg.fallback_to_market_on_limit_fail
        ):
            self.log(f"[ORDER FAIL] {msg}")
            self.log("[ORDER] Limit failed — trying one market fallback…")
            with self._broker_lock:
                ok, msg, order_id = self.broker.send_trade_order(
                    sym,
                    sig.direction,
                    volume,
                    sig.stop_loss,
                    sig.target,
                    cfg.magic,
                    order_mode="market",
                    limit_price=sig.entry_price,
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
