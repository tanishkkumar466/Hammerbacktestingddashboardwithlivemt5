"""Multi-account live worker IPC (process-per-MT5-login)."""

from __future__ import annotations

import time
from queue import Empty
from multiprocessing import Queue

from live_account_ipc import AccountWorkerHandle, apply_worker_event
from live_account_worker import account_worker_main


def test_apply_worker_event_connected_and_slots():
    h = AccountWorkerHandle(account_id="a1", account_name="Demo")
    apply_worker_event(h, {"kind": "connected", "message": "ok", "account_info": {"login": 42}})
    assert h.connected is True
    assert h.account_info.get("login") == 42
    apply_worker_event(h, {"kind": "slot_started", "slot_id": "s1"})
    assert "s1" in h.running_slots
    apply_worker_event(h, {"kind": "slot_stopped", "slot_id": "s1"})
    assert "s1" not in h.running_slots
    apply_worker_event(h, {"kind": "disconnected", "message": "bye"})
    assert h.connected is False
    assert not h.running_slots


def test_account_worker_process_ping_and_shutdown():
    """Spawn a real worker process — no MT5 required for ping/shutdown."""
    cmd_q: Queue = Queue()
    evt_q: Queue = Queue()
    from live_account_ipc import _ctx

    ctx = _ctx()
    proc = ctx.Process(
        target=account_worker_main,
        args=("accX", "TestAcc", cmd_q, evt_q),
        daemon=True,
    )
    proc.start()
    try:
        # Wait for ready log
        deadline = time.time() + 8
        saw_ready = False
        while time.time() < deadline:
            try:
                evt = evt_q.get(timeout=0.5)
            except Empty:
                continue
            if evt.get("kind") == "log" and "ready" in str(evt.get("message", "")).lower():
                saw_ready = True
                break
        assert saw_ready, "worker did not become ready"
        cmd_q.put({"cmd": "ping"})
        deadline = time.time() + 5
        saw_pong = False
        while time.time() < deadline:
            try:
                evt = evt_q.get(timeout=0.5)
            except Empty:
                continue
            if evt.get("kind") == "pong":
                saw_pong = True
                break
        assert saw_pong
        cmd_q.put({"cmd": "shutdown"})
        proc.join(timeout=5)
        assert not proc.is_alive()
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)


def test_handle_start_shutdown_roundtrip():
    h = AccountWorkerHandle(account_id="h2", account_name="HandleTest")
    h.start()
    assert h.is_alive()
    # Drain a few events
    time.sleep(0.8)
    evts = h.poll_events(max_n=20)
    assert any(e.get("kind") in ("log", "heartbeat") for e in evts)
    h.shutdown(timeout=5)
    assert not h.is_alive()
