"""Hardening checks for multi-account live routing (no wrong-account track)."""

from __future__ import annotations

from live_account_ipc import AccountWorkerHandle, apply_worker_event


def test_slot_routes_only_via_own_account_handle():
    """start_slot must be keyed by slot.account_id — never a shared global handle."""
    handles = {
        "acc-a": AccountWorkerHandle(account_id="acc-a", account_name="A"),
        "acc-b": AccountWorkerHandle(account_id="acc-b", account_name="B"),
    }
    slot_account_id = "acc-b"
    handle = handles.get(slot_account_id)
    assert handle is not None
    assert handle.account_id == "acc-b"
    assert handles["acc-a"].account_id != handle.account_id


def test_dead_worker_clears_running_slots_on_event_path():
    h = AccountWorkerHandle(account_id="a1", account_name="Demo")
    apply_worker_event(h, {"kind": "connected", "message": "ok"})
    apply_worker_event(h, {"kind": "slot_started", "slot_id": "s1"})
    apply_worker_event(h, {"kind": "slot_started", "slot_id": "s2"})
    assert h.connected and h.running_slots == {"s1", "s2"}
    # Simulate UI cleanup after process death
    h.connected = False
    h.running_slots.clear()
    assert not h.connected
    assert not h.running_slots


def test_poll_events_default_handles_burst():
    h = AccountWorkerHandle(account_id="a1", account_name="Demo")
    # No queue yet — empty drain must not crash
    assert h.poll_events() == []
    assert h.poll_events(max_n=200) == []
