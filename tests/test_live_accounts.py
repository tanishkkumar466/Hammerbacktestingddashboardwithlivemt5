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
