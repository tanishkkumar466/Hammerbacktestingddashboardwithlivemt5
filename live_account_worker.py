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
            log=log,
        )

        def _run():
            try:
                engine.run()
            except Exception as exc:
                log(f"[CRASH] Slot {slot_id}: {exc}\n{traceback.format_exc()}")
                emit("slot_crashed", slot_id=slot_id, message=str(exc))
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
            elif cmd == "ping":
                emit("pong")
            elif cmd == "shutdown":
                stop_all = True
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
