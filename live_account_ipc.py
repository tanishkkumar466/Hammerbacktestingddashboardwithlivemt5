"""
IPC for one OS process per MT5 login (MetaTrader5 is process-global).

Main dashboard process  ←queues→  account worker process
  (Qt UI / backtest)                 (MT5Broker + LiveTradingEngine threads)
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


def _ctx():
    # spawn = clean interpreter (required on macOS/Windows for safety with Qt)
    return mp.get_context("spawn")


@dataclass
class AccountWorkerHandle:
    account_id: str
    account_name: str
    cmd_q: Any = field(default=None, repr=False)
    evt_q: Any = field(default=None, repr=False)
    proc: Any = field(default=None, repr=False)
    connected: bool = False
    connect_message: str = ""
    running_slots: Set[str] = field(default_factory=set)
    last_heartbeat: float = 0.0
    account_info: Dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            return
        ctx = _ctx()
        self.cmd_q = ctx.Queue()
        self.evt_q = ctx.Queue()
        from live_account_worker import account_worker_main

        self.proc = ctx.Process(
            target=account_worker_main,
            args=(self.account_id, self.account_name, self.cmd_q, self.evt_q),
            name=f"hammer-live-{self.account_id}",
            daemon=True,
        )
        self.proc.start()
        self.last_heartbeat = time.monotonic()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def send(self, cmd: str, **payload: Any) -> None:
        if self.cmd_q is None:
            raise RuntimeError("Account worker not started")
        msg = {"cmd": cmd, **payload}
        self.cmd_q.put(msg)

    def poll_events(self, max_n: int = 50) -> List[Dict[str, Any]]:
        if self.evt_q is None:
            return []
        out: List[Dict[str, Any]] = []
        for _ in range(max_n):
            try:
                out.append(self.evt_q.get_nowait())
            except queue.Empty:
                break
        return out

    def shutdown(self, timeout: float = 5.0) -> None:
        try:
            if self.is_alive():
                self.send("shutdown")
                self.proc.join(timeout=timeout)
        except Exception:
            pass
        if self.proc is not None and self.proc.is_alive():
            try:
                self.proc.terminate()
                self.proc.join(timeout=2.0)
            except Exception:
                pass
        self.proc = None
        self.connected = False
        self.running_slots.clear()


def apply_worker_event(handle: AccountWorkerHandle, evt: Dict[str, Any]) -> None:
    """Update handle state from one worker event (call on UI thread)."""
    kind = evt.get("kind") or ""
    if kind == "heartbeat":
        handle.last_heartbeat = time.monotonic()
        info = evt.get("account_info")
        if isinstance(info, dict):
            handle.account_info = info
    elif kind == "connected":
        handle.connected = True
        handle.connect_message = str(evt.get("message") or "Connected")
        info = evt.get("account_info")
        if isinstance(info, dict):
            handle.account_info = info
    elif kind == "disconnected":
        handle.connected = False
        handle.connect_message = str(evt.get("message") or "Disconnected")
        handle.running_slots.clear()
    elif kind == "slot_started":
        sid = str(evt.get("slot_id") or "")
        if sid:
            handle.running_slots.add(sid)
    elif kind == "slot_stopped":
        sid = str(evt.get("slot_id") or "")
        handle.running_slots.discard(sid)
    elif kind == "error":
        handle.connect_message = str(evt.get("message") or "Error")
