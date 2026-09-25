import json
import os

import live_accounts as la


def test_defaults_have_one_account_and_slot():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    assert len(desk.accounts) == 1
    assert len(desk.slots) == 1
    assert desk.slots[0].account_id == desk.accounts[0].id
    assert desk.magics_unique()


def test_next_magic_skips_used():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.slots[0].magic = la.MAGIC_BASE
    desk.slots.append(la.LiveSlot.new(desk.accounts[0].id, "Slot 2", la.MAGIC_BASE + 1))
    assert desk.next_magic() == la.MAGIC_BASE + 2


def test_roundtrip_json(tmp_path):
    desk = la.LiveDesk()
    desk.ensure_defaults()
    acc = la.LiveAccount.new("Demo 2")
    acc.login = "12345"
    acc.server = "ICMarkets"
    desk.accounts.append(acc)
    desk.slots.append(la.LiveSlot.new(acc.id, "15m", desk.next_magic()))
    path = str(tmp_path / "desk.json")
    la.save_desk(path, desk)
    loaded = la.load_desk(path)
    assert len(loaded.accounts) == 2
    assert len(loaded.slots) == 2
    assert loaded.magics_unique()
    assert any(a.name == "Demo 2" for a in loaded.accounts)
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    assert "password" not in json.dumps(raw)


def test_account_max_spread_points_roundtrip(tmp_path):
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.accounts[0].max_spread_points = 120.0
    path = str(tmp_path / "desk.json")
    la.save_desk(path, desk)
    loaded = la.load_desk(path)
    assert loaded.accounts[0].max_spread_points == 120.0


def test_slot_preset_file_roundtrip(tmp_path):
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.slots[0].preset_file = "hammer_3m_rsi.json"
    path = str(tmp_path / "desk.json")
    la.save_desk(path, desk)
    loaded = la.load_desk(path)
    assert loaded.slots[0].preset_file == "hammer_3m_rsi.json"


def test_next_slot_timeframe_skips_used():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    acc_id = desk.accounts[0].id
    desk.slots[0].timeframe = "3m"
    assert desk.next_slot_timeframe(acc_id) == "5m"
    desk.slots.append(la.LiveSlot.new(acc_id, "Slot 2", desk.next_magic()))
    desk.slots[-1].timeframe = "5m"
    assert desk.next_slot_timeframe(acc_id) == "15m"


def test_password_stripped_from_account_dict():
    acc = la.LiveAccount.new("Secret")
    acc.password = "not-in-file"
    desk = la.LiveDesk(accounts=[acc], slots=[], active_account_id=acc.id)
    desk.ensure_defaults()
    raw = la.desk_to_dict(desk)
    assert all("password" not in a for a in raw["accounts"])


def test_remove_slot_keeps_one_per_account():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    acc = desk.accounts[0].id
    assert desk.remove_slot(desk.slots[0].id) is False
    extra = la.LiveSlot.new(acc, "Slot 2", desk.next_magic())
    desk.slots.append(extra)
    assert desk.remove_slot(extra.id) is True
    assert len(desk.slots) == 1


def test_each_account_gets_its_own_magic_series():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    a1 = desk.accounts[0]
    a2 = la.LiveAccount.new("Two", desk.next_account_magic_base())
    desk.accounts.append(a2)
    desk.slots.append(la.LiveSlot.new(a2.id, "S", desk.next_magic_for_account(a2.id)))
    desk.ensure_magic_series()
    lo1, hi1 = a1.magic_range()
    lo2, hi2 = a2.magic_range()
    assert hi1 < lo2
    assert a1.magic_base == la.MAGIC_BASE
    assert a2.magic_base == la.MAGIC_BASE + la.MAGIC_SERIES_SIZE
    assert all(lo1 <= s.magic <= hi1 for s in desk.slots_for_account(a1.id))
    assert all(lo2 <= s.magic <= hi2 for s in desk.slots_for_account(a2.id))
    assert desk.magics_unique()
    assert desk.magics_in_series()


def test_legacy_large_magics_are_rewritten_into_series():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.slots[0].magic = 88_001_001
    desk.ensure_magic_series()
    lo, hi = desk.accounts[0].magic_range()
    assert lo <= desk.slots[0].magic <= hi
    assert desk.slots[0].magic < la.LEGACY_MAGIC_MIN


def test_remove_account_keeps_one():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    assert desk.remove_account(desk.accounts[0].id) is False
    acc2 = la.LiveAccount.new("Two")
    desk.accounts.append(acc2)
    desk.slots.append(la.LiveSlot.new(acc2.id, "S", desk.next_magic()))
    first = desk.accounts[0].id
    assert desk.remove_account(first) is True
    assert len(desk.accounts) == 1
    assert desk.accounts[0].id == acc2.id
    assert all(s.account_id == acc2.id for s in desk.slots)


def test_next_magic_raises_when_series_full():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    acc = desk.accounts[0]
    lo, hi = acc.magic_range()
    # Fill every magic in the series (including the default slot)
    desk.slots = [
        la.LiveSlot.new(acc.id, f"S{i}", lo + i) for i in range(hi - lo + 1)
    ]
    try:
        desk.next_magic_for_account(acc.id)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "full" in str(exc).lower() or "Max" in str(exc)


def test_many_accounts_get_isolated_magic_series():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    for i in range(5):
        acc = la.LiveAccount.new(f"Acc{i + 2}", desk.next_account_magic_base())
        desk.accounts.append(acc)
        desk.slots.append(la.LiveSlot.new(acc.id, "S1", desk.next_magic_for_account(acc.id)))
    desk.ensure_magic_series()
    bases = [a.magic_base for a in desk.accounts]
    assert len(bases) == len(set(bases))
    assert desk.magics_unique()
    assert desk.magics_in_series()
    # No overlapping ranges
    ranges = [a.magic_range() for a in desk.accounts]
    for i, (lo_a, hi_a) in enumerate(ranges):
        for j, (lo_b, hi_b) in enumerate(ranges):
            if i >= j:
                continue
            assert hi_a < lo_b or hi_b < lo_a


def test_dry_run_persists_per_account(tmp_path):
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.accounts[0].dry_run = False
    acc2 = la.LiveAccount.new("Two", desk.next_account_magic_base())
    acc2.dry_run = True
    desk.accounts.append(acc2)
    desk.slots.append(la.LiveSlot.new(acc2.id, "S", desk.next_magic_for_account(acc2.id)))
    path = str(tmp_path / "desk.json")
    la.save_desk(path, desk)
    loaded = la.load_desk(path)
    by_name = {a.name: a for a in loaded.accounts}
    assert by_name[desk.accounts[0].name].dry_run is False
    assert by_name["Two"].dry_run is True


def test_normalize_hhmm():
    assert la.normalize_hhmm("") == ""
    assert la.normalize_hhmm("9:5") == "09:05"
    assert la.normalize_hhmm("15:55") == "15:55"
    assert la.normalize_hhmm("25:00") == ""
    assert la.normalize_hhmm("bad") == ""


def test_apply_global_schedule_same_and_per_account(tmp_path):
    desk = la.LiveDesk()
    desk.ensure_defaults()
    a2 = la.LiveAccount.new("Two", desk.next_account_magic_base())
    desk.accounts.append(a2)
    desk.slots.append(la.LiveSlot.new(a2.id, "S", desk.next_magic_for_account(a2.id)))
    desk.apply_global_schedule(start_hhmm="09:15", stop_hhmm="15:55")
    assert desk.global_start_hhmm == "09:15"
    assert all(a.schedule_start_hhmm == "09:15" for a in desk.accounts)
    assert all(a.schedule_stop_hhmm == "15:55" for a in desk.accounts)
    # Per-account override
    desk.accounts[0].schedule_start_hhmm = "10:00"
    path = str(tmp_path / "desk.json")
    la.save_desk(path, desk)
    loaded = la.load_desk(path)
    assert loaded.accounts[0].schedule_start_hhmm == "10:00"
    assert loaded.accounts[1].schedule_start_hhmm == "09:15"
    desk.apply_global_schedule(clear=True)
    assert desk.global_start_hhmm == ""
    assert all(a.schedule_start_hhmm == "" for a in desk.accounts)


def test_dashboard_schedule_stop_closes_positions():
    """Global/schedule stop must flatten (close positions), not only halt bots."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(encoding="utf-8")
    marker = "def _check_live_schedule_timers"
    idx = src.find(marker)
    assert idx > 0
    chunk = src[idx : idx + 2500]
    assert "_emergency_flatten_account" in chunk
    assert "stop slots + close positions" in chunk
