"""
Child process: one MT5 login + N slot engines (threads).

MetaTrader5 Python allows only one connection per process — so each Hammer
Live account gets its own worker process. Slots on that account still share
one broker (with api_lock) via threads inside this process.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from queue import Empty
from typing import Any, Dict, Optional


def account_worker_main(account_id: str, account_name: str, cmd_q, evt_q) -> None:
    """Entry point for multiprocessing (must be importable / picklable)."""
    # Avoid Qt in the child
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.pop("QT_QPA_PLATFORM", None)

    def emit(kind: str, **payload: Any) -> None:
        try:
            evt_q.put({"kind": kind, "account_id": account_id, **payload})
        except Exception:
            pass

    def log(msg: str) -> None:
        emit("log", message=str(msg))

    try:
        from broker import BrokerCredentials, MT5Broker
        import live as live_trading
    except Exception as exc:
        emit("error", message=f"Worker import failed: {exc}")
        return

    broker = MT5Broker()
    slots: Dict[str, Dict[str, Any]] = {}
    stop_all = False
    fetch_stop = threading.Event()
    fetch_thread: Optional[threading.Thread] = None

    def _account_info() -> Dict[str, Any]:
        try:
            return broker.account_info_dict() or {}
        except Exception:
            return {}

    def _stop_slot(slot_id: str, join_sec: float = 6.0) -> None:
        worker = slots.pop(slot_id, None)
        if worker is None:
            return
        eng = worker.get("engine")
        th = worker.get("thread")
        try:
            if eng is not None:
                eng.request_stop()
        except Exception:
            pass
        try:
            if th is not None and th.is_alive():
                th.join(timeout=join_sec)
        except Exception:
            pass
        emit("slot_stopped", slot_id=slot_id)

    def _stop_all_slots() -> None:
        for sid in list(slots.keys()):
            _stop_slot(sid)

    def _on_connect(msg: Dict[str, Any]) -> None:
        creds = BrokerCredentials(
            terminal_path=str(msg.get("terminal_path") or ""),
            login=int(msg.get("login") or 0),
            password=str(msg.get("password") or ""),
            server=str(msg.get("server") or ""),
        )
        log(f"Connecting MT5 for '{account_name}'…")
        ok, text = broker.connect(creds)
        log(text)
        if ok:
            emit("connected", message=text, account_info=_account_info())
        else:
            emit("error", message=text)
            emit("disconnected", message=text)

    def _on_disconnect() -> None:
        _stop_all_slots()
        broker.disconnect()
        emit("disconnected", message="Disconnected from MT5.")
        log("Disconnected from MT5.")

    def _on_start_slot(msg: Dict[str, Any]) -> None:
        slot_id = str(msg.get("slot_id") or "")
        if not slot_id:
            emit("error", message="start_slot missing slot_id")
            return
        if slot_id in slots:
            emit("error", message=f"Slot {slot_id} already running")
            return
        if not broker.is_connected:
            emit("error", message="Not connected — Connect MT5 first on this account.")
            return
        try:
            live_cfg = msg["live_config"]
            pattern_type = msg["pattern_type"]
            pattern_label = msg.get("pattern_label") or pattern_type
            strategy_config = msg["strategy_config"]
            indicator_stack = msg["indicator_stack"]
        except KeyError as exc:
            emit("error", message=f"start_slot missing field: {exc}")
            return

        engine = live_trading.LiveTradingEngine(
            broker=broker,
            live_config=live_cfg,
            pattern_type=pattern_type,
            pattern_label=pattern_label,
            strategy_config=strategy_config,
            indicator_stack=indicator_stack,
            log=lambda msg, _sid=slot_id: emit("log", message=str(msg), slot_id=_sid),
        )

        def _run():
            try:
                engine.run()
            except Exception as exc:
                emit(
                    "log",
                    message=f"[CRASH] Slot {slot_id}: {exc}\n{traceback.format_exc()}",
                    slot_id=slot_id,
                )
                emit("slot_crashed", slot_id=slot_id, message=str(exc))
                try:
                    engine._record_skip("ERROR", f"Slot crash: {exc}")
                except Exception:
                    pass
            finally:
                slots.pop(slot_id, None)
                emit("slot_stopped", slot_id=slot_id)

        th = threading.Thread(
            target=_run,
            name=f"live-slot-{slot_id}",
            daemon=True,
        )
        slots[slot_id] = {"engine": engine, "thread": th}
        th.start()
        emit("slot_started", slot_id=slot_id)
        log(f"Slot {slot_id} started on '{account_name}'.")

    def _on_fetch_job(msg: Dict[str, Any]) -> None:
        nonlocal fetch_thread
        if fetch_thread is not None and fetch_thread.is_alive():
            emit("fetch_failed", message="Fetch already running on this account.")
            return
        if not broker.is_connected or broker.raw_mt5 is None:
            emit(
                "fetch_failed",
                message="Not connected — Connect MT5 on this Live account first.",
            )
            return
        # Never open a second MT5 session — reuse Live's connection + lock.
        job = {
            "output_root": str(msg.get("output_root") or ""),
            "symbols": list(msg.get("symbols") or ["XAUUSD"]),
            "timeframes": list(msg.get("timeframes") or ["1min"]),
            "update_existing": bool(msg.get("update_existing", True)),
            "start_date": msg.get("start_date"),
            "use_mock": False,
            "market_type": msg.get("market_type"),
            "auto_detect_market": bool(msg.get("auto_detect_market", True)),
            "flat_under_root": bool(msg.get("flat_under_root", False)),
        }
        if not job["output_root"]:
            emit("fetch_failed", message="fetch_job missing output_root")
            return

        fetch_stop.clear()

        def _run_fetch() -> None:
            try:
                import fetch as data_fetcher

                result = data_fetcher.run_fetch_job(
                    output_root=job["output_root"],
                    symbols=job["symbols"],
                    timeframes=job["timeframes"],
                    update_existing=job["update_existing"],
                    start_date=job.get("start_date"),
                    use_mock=False,
                    market_type=job.get("market_type"),
                    auto_detect_market=job["auto_detect_market"],
                    flat_under_root=job["flat_under_root"],
                    mt5_module=broker.raw_mt5,
                    own_connection=False,
                    api_lock=getattr(broker, "api_lock", None),
                    log=lambda m: emit("fetch_log", message=str(m)),
                    should_stop=lambda: fetch_stop.is_set() or stop_all,
                )
                emit("fetch_done", result=result if isinstance(result, dict) else {})
            except Exception as exc:
                emit(
                    "fetch_failed",
                    message=f"{exc}\n{traceback.format_exc()}",
                )

        fetch_thread = threading.Thread(
            target=_run_fetch,
            name=f"fetch-{account_id}",
            daemon=True,
        )
        fetch_thread.start()
        emit(
            "fetch_log",
            message=(
                f"[INFO] Fetch via Live worker for '{account_name}' "
                "(shared MT5 — Live keeps running)."
            ),
        )

    def _on_fetch_stop() -> None:
        fetch_stop.set()
        emit("fetch_log", message="[STOP] Stop requested — finishing current chunk…")

    def _on_emergency(msg: Dict[str, Any]) -> None:
        magics = [int(m) for m in (msg.get("magics") or [])]
        _stop_all_slots()
        if not broker.is_connected:
            log("[emergency] Slots stopped. Not connected — cannot close positions.")
            return
        closed, failed, detail = broker.close_positions_for_magics(magics)
        log(f"[emergency] closed {closed}, failed {failed}. {detail}")
        emit("emergency_done", closed=closed, failed=failed, detail=detail)

    log(f"Account worker ready for '{account_name}' ({account_id}).")
    last_hb = 0.0
    while not stop_all:
        now = time.monotonic()
        if now - last_hb >= 2.0:
            last_hb = now
            emit(
                "heartbeat",
                connected=broker.is_connected,
                running_slots=list(slots.keys()),
                account_info=_account_info() if broker.is_connected else {},
            )
        try:
            msg = cmd_q.get(timeout=0.4)
        except Empty:
            continue
        except Exception as exc:
            emit("error", message=f"cmd queue error: {exc}")
            break

        cmd = str(msg.get("cmd") or "")
        try:
            if cmd == "connect":
                _on_connect(msg)
            elif cmd == "disconnect":
                _on_disconnect()
            elif cmd == "start_slot":
                _on_start_slot(msg)
            elif cmd == "stop_slot":
                _stop_slot(str(msg.get("slot_id") or ""))
            elif cmd == "stop_all_slots":
                _stop_all_slots()
            elif cmd == "emergency_close":
                _on_emergency(msg)
            elif cmd == "update_telegram":
                bots = msg.get("notification_bots")
                enabled = bool(msg.get("enabled", True))
                token = str(msg.get("bot_token") or "")
                chat_id = str(msg.get("chat_id") or "")
                for entry in slots.values():
                    eng = entry.get("engine")
                    if eng is None or not hasattr(eng, "update_telegram_settings"):
                        continue
                    try:
                        eng.update_telegram_settings(
                            enabled=enabled,
                            bot_token=token,
                            chat_id=chat_id,
                            notification_bots=bots,
                        )
                    except Exception as exc:
                        log(f"[TELEGRAM] update failed: {exc}")
            elif cmd == "set_notify_enabled":
                sid = str(msg.get("slot_id") or "")
                entry = slots.get(sid)
                eng = entry.get("engine") if entry else None
                if eng is not None and hasattr(eng, "set_notify_enabled"):
                    try:
                        eng.set_notify_enabled(bool(msg.get("notify_enabled", True)))
                    except Exception as exc:
                        log(f"[TELEGRAM] set_notify_enabled failed: {exc}")
                elif eng is not None and hasattr(eng, "live_config"):
                    eng.live_config.notify_enabled = bool(msg.get("notify_enabled", True))
            elif cmd == "update_strategy":
                sid = str(msg.get("slot_id") or "")
                entry = slots.get(sid)
                eng = entry.get("engine") if entry else None
                if eng is None:
                    emit("error", message=f"update_strategy: slot {sid} not running")
                else:
                    try:
                        eng.update_runtime_strategy(
                            msg["strategy_config"],
                            msg["indicator_stack"],
                            str(msg.get("pattern_type") or eng.pattern_type),
                            str(msg.get("pattern_label") or eng.pattern_label),
                            sessions_enabled=msg.get("sessions_enabled"),
                            session_clock=msg.get("session_clock"),
                            broker_utc_offset_hours=msg.get("broker_utc_offset_hours"),
                            ist_time_filter_enabled=msg.get("ist_time_filter_enabled"),
                            ist_time_start=msg.get("ist_time_start"),
                            ist_time_end=msg.get("ist_time_end"),
                            telegram_enabled=msg.get("telegram_enabled"),
                            telegram_bot_token=msg.get("telegram_bot_token"),
                            telegram_chat_id=msg.get("telegram_chat_id"),
                            notification_bots=msg.get("notification_bots"),
                        )
                        log(f"Strategy hot-reloaded on slot {sid}.")
                    except Exception as exc:
                        log(f"[STRATEGY] update failed: {exc}\n{traceback.format_exc()}")
                        emit("error", message=f"update_strategy failed: {exc}")
            elif cmd == "update_safety":
                sid = str(msg.get("slot_id") or "")
                targets = []
                if sid:
                    entry = slots.get(sid)
                    if entry:
                        targets = [entry]
                else:
                    targets = list(slots.values())
                for entry in targets:
                    eng = entry.get("engine") if entry else None
                    if eng is None or not hasattr(eng, "update_runtime_safety"):
                        continue
                    try:
                        kwargs = {}
                        if "max_spread_points" in msg:
                            kwargs["max_spread_points"] = float(msg["max_spread_points"])
                        if "min_minutes_between_trades" in msg:
                            kwargs["min_minutes_between_trades"] = float(
                                msg["min_minutes_between_trades"]
                            )
                        if "max_daily_trades" in msg:
                            kwargs["max_daily_trades"] = int(msg["max_daily_trades"])
                        if "max_daily_loss_usd" in msg:
                            kwargs["max_daily_loss_usd"] = float(msg["max_daily_loss_usd"])
                        if "max_lot_size" in msg:
                            kwargs["max_lot_size"] = float(msg["max_lot_size"])
                        if "demo_accounts_only" in msg:
                            kwargs["demo_accounts_only"] = bool(msg["demo_accounts_only"])
                        if kwargs:
                            eng.update_runtime_safety(**kwargs)
                    except Exception as exc:
                        log(f"[SAFETY] update failed: {exc}")
            elif cmd == "fetch_job":
                _on_fetch_job(msg)
            elif cmd == "fetch_stop":
                _on_fetch_stop()
            elif cmd == "ping":
                emit("pong")
            elif cmd == "shutdown":
                stop_all = True
                fetch_stop.set()
            else:
                emit("error", message=f"Unknown cmd: {cmd}")
        except Exception as exc:
            log(f"[WORKER ERROR] {exc}\n{traceback.format_exc()}")
            emit("error", message=str(exc))

    _stop_all_slots()
    try:
        broker.disconnect()
    except Exception:
        pass
    emit("disconnected", message="Worker shutdown")
    log(f"Account worker for '{account_name}' stopped.")


if __name__ == "__main__":
    # Manual debug: python live_account_worker.py
    print("live_account_worker is launched via multiprocessing, not as a script.", file=sys.stderr)
    sys.exit(1)
