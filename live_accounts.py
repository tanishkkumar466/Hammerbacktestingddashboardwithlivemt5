"""Live accounts and strategy slots.

Phase 1: multiple slots (TF/magic) per MT5 login.
Phase 2: each Live account can run in its own worker process so two logins
trade in parallel (MetaTrader5 Python is process-global).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Small magics, reserved series per account: 1100–1199, 1200–1299, …
MAGIC_BASE = 1100
MAGIC_SERIES_SIZE = 100
LEGACY_MAGIC_MIN = 1_000_000


@dataclass
class LiveAccount:
    id: str
    name: str
    login: str = ""
    server: str = ""
    terminal_path: str = ""
    max_daily_loss_usd: float = 100.0
    magic_base: int = MAGIC_BASE
    flatten_hhmm: str = ""  # "15:55" local system clock; empty = off
    # In-memory only — never written to live_desk.json
    password: str = ""

    @staticmethod
    def new(name: str = "Account", magic_base: int = MAGIC_BASE) -> "LiveAccount":
        return LiveAccount(
            id=str(uuid.uuid4())[:8],
            name=name.strip() or "Account",
            magic_base=int(magic_base),
        )

    def magic_range(self) -> Tuple[int, int]:
        base = int(self.magic_base or MAGIC_BASE)
        return base, base + MAGIC_SERIES_SIZE - 1


@dataclass
class LiveSlot:
    id: str
    account_id: str
    name: str
    symbol: str = "XAUUSD"
    timeframe: str = "3m"
    magic: int = MAGIC_BASE
    volume: str = "0.01"
    enabled: bool = True
    # Saved strategy file under presets/ — empty means use the Parameters panel
    preset_file: str = ""

    @staticmethod
    def new(account_id: str, name: str, magic: int) -> "LiveSlot":
        return LiveSlot(
            id=str(uuid.uuid4())[:8],
            account_id=account_id,
            name=name.strip() or "Slot",
            magic=int(magic),
        )


@dataclass
class LiveDesk:
    accounts: List[LiveAccount] = field(default_factory=list)
    slots: List[LiveSlot] = field(default_factory=list)
    active_account_id: str = ""

    def ensure_defaults(self) -> None:
        if not self.accounts:
            acc = LiveAccount.new("Primary", MAGIC_BASE)
            self.accounts.append(acc)
            self.active_account_id = acc.id
        if not self.active_account_id:
            self.active_account_id = self.accounts[0].id
        self.ensure_magic_series()
        if not self.slots:
            acc = self.account_by_id(self.active_account_id) or self.accounts[0]
            self.slots.append(LiveSlot.new(acc.id, "Slot 1", self.next_magic_for_account(acc.id)))

    def ensure_magic_series(self) -> None:
        used_bases: set = set()
        for acc in self.accounts:
            base = int(acc.magic_base or 0)
            if base < MAGIC_BASE or base >= LEGACY_MAGIC_MIN or base in used_bases:
                base = MAGIC_BASE
                while base in used_bases:
                    base += MAGIC_SERIES_SIZE
                acc.magic_base = base
            used_bases.add(acc.magic_base)
            lo, hi = acc.magic_range()
            for i, slot in enumerate(self.slots_for_account(acc.id)):
                mag = int(slot.magic)
                if mag < lo or mag > hi or mag >= LEGACY_MAGIC_MIN:
                    slot.magic = lo + i
        self._uniquify_magics()

    def _uniquify_magics(self) -> None:
        seen: set = set()
        for slot in self.slots:
            acc = self.account_by_id(slot.account_id)
            lo, hi = acc.magic_range() if acc else (MAGIC_BASE, MAGIC_BASE + MAGIC_SERIES_SIZE - 1)
            mag = int(slot.magic)
            if mag in seen or mag < lo or mag > hi:
                mag = lo
                while mag in seen:
                    mag += 1
                slot.magic = mag
            seen.add(int(slot.magic))

    def account_by_id(self, acc_id: str) -> Optional[LiveAccount]:
        for a in self.accounts:
            if a.id == acc_id:
                return a
        return None

    def slot_by_id(self, slot_id: str) -> Optional[LiveSlot]:
        for s in self.slots:
            if s.id == slot_id:
                return s
        return None

    def slots_for_account(self, acc_id: str) -> List[LiveSlot]:
        return [s for s in self.slots if s.account_id == acc_id]

    def next_slot_timeframe(self, account_id: str) -> str:
        used = {s.timeframe for s in self.slots_for_account(account_id)}
        for tf in ("3m", "5m", "15m", "30m", "1h", "10m", "1m"):
            if tf not in used:
                return tf
        return "3m"

    def next_account_magic_base(self) -> int:
        used = {int(a.magic_base or 0) for a in self.accounts}
        n = MAGIC_BASE
        while n in used:
            n += MAGIC_SERIES_SIZE
        return n

    def next_magic_for_account(self, account_id: str) -> int:
        acc = self.account_by_id(account_id)
        lo, hi = acc.magic_range() if acc else (MAGIC_BASE, MAGIC_BASE + MAGIC_SERIES_SIZE - 1)
        used = {int(s.magic) for s in self.slots}
        n = lo
        while n in used:
            n += 1
            if n > hi:
                n = lo
                while n in used:
                    n += 1
                break
        return n

    def next_magic(self) -> int:
        acc_id = self.active_account_id or (self.accounts[0].id if self.accounts else "")
        return self.next_magic_for_account(acc_id)

    def magics_unique(self) -> bool:
        vals = [int(s.magic) for s in self.slots]
        return len(vals) == len(set(vals))

    def magics_in_series(self) -> bool:
        for slot in self.slots:
            acc = self.account_by_id(slot.account_id)
            if acc is None:
                return False
            lo, hi = acc.magic_range()
            if int(slot.magic) < lo or int(slot.magic) > hi:
                return False
        return True

    def remove_slot(self, slot_id: str) -> bool:
        slot = self.slot_by_id(slot_id)
        if slot is None:
            return False
        if len(self.slots_for_account(slot.account_id)) <= 1:
            return False
        self.slots = [s for s in self.slots if s.id != slot_id]
        return True

    def remove_account(self, acc_id: str) -> bool:
        if len(self.accounts) <= 1:
            return False
        if self.account_by_id(acc_id) is None:
            return False
        self.slots = [s for s in self.slots if s.account_id != acc_id]
        self.accounts = [a for a in self.accounts if a.id != acc_id]
        if self.active_account_id == acc_id:
            self.active_account_id = self.accounts[0].id
        return True


def desk_to_dict(desk: LiveDesk) -> Dict[str, Any]:
    accounts = []
    for a in desk.accounts:
        row = asdict(a)
        row.pop("password", None)
        accounts.append(row)
    return {
        "active_account_id": desk.active_account_id,
        "accounts": accounts,
        "slots": [asdict(s) for s in desk.slots],
    }


def desk_from_dict(raw: Optional[Dict[str, Any]]) -> LiveDesk:
    desk = LiveDesk()
    if not raw:
        desk.ensure_defaults()
        return desk
    desk.active_account_id = str(raw.get("active_account_id") or "")
    for row in raw.get("accounts") or []:
        try:
            magic_base = int(row.get("magic_base") or MAGIC_BASE)
        except (TypeError, ValueError):
            magic_base = MAGIC_BASE
        desk.accounts.append(LiveAccount(
            id=str(row.get("id") or uuid.uuid4())[:8],
            name=str(row.get("name") or "Account"),
            login=str(row.get("login") or ""),
            server=str(row.get("server") or ""),
            terminal_path=str(row.get("terminal_path") or ""),
            max_daily_loss_usd=float(row.get("max_daily_loss_usd") or 100.0),
            magic_base=magic_base,
            flatten_hhmm=str(row.get("flatten_hhmm") or ""),
        ))
    for row in raw.get("slots") or []:
        try:
            magic = int(row.get("magic") or MAGIC_BASE)
        except (TypeError, ValueError):
            magic = MAGIC_BASE
        desk.slots.append(LiveSlot(
            id=str(row.get("id") or uuid.uuid4())[:8],
            account_id=str(row.get("account_id") or ""),
            name=str(row.get("name") or "Slot"),
            symbol=str(row.get("symbol") or "XAUUSD"),
            timeframe=str(row.get("timeframe") or "3m"),
            magic=magic,
            volume=str(row.get("volume") or "0.01"),
            enabled=bool(row.get("enabled", True)),
            preset_file=str(row.get("preset_file") or ""),
        ))
    desk.ensure_defaults()
    if desk.slots and not desk.slots[0].account_id and desk.accounts:
        for s in desk.slots:
            if not s.account_id:
                s.account_id = desk.accounts[0].id
    return desk


def load_desk(path: str) -> LiveDesk:
    if not path or not os.path.isfile(path):
        desk = LiveDesk()
        desk.ensure_defaults()
        return desk
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        desk = LiveDesk()
        desk.ensure_defaults()
        return desk
    return desk_from_dict(raw if isinstance(raw, dict) else None)


def save_desk(path: str, desk: LiveDesk) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(desk_to_dict(desk), f, indent=2)
