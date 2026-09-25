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


def data_folder_slug(name: str) -> str:
    """
    Filesystem-safe folder under data/ for an account, e.g. 'IC Markets' → 'ic_markets'.
    Layout: data/<slug>/<symbol>/<timeframe>/…
    """
    raw = (name or "").strip().lower()
    if not raw:
        return "account"
    out = []
    prev_us = False
    for ch in raw:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif ch in (" ", "-", "_", ".", "/"):
            if not prev_us and out:
                out.append("_")
                prev_us = True
    slug = "".join(out).strip("_")
    return slug or "account"


def normalize_hhmm(raw: Optional[str]) -> str:
    """Return 'HH:MM' from user text, or '' if empty / invalid."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        if ":" in text:
            hh_s, mm_s = text.split(":", 1)
            hh, mm = int(hh_s), int(mm_s.split()[0] if mm_s else 0)
        else:
            hh, mm = int(text), 0
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return ""
        return f"{hh:02d}:{mm:02d}"
    except (TypeError, ValueError):
        return ""


@dataclass
class LiveAccount:
    id: str
    name: str
    login: str = ""
    server: str = ""
    terminal_path: str = ""
    max_daily_loss_usd: float = 100.0
    # 0 = use Live Settings global default; >0 = this account only (hot-reloadable)
    max_spread_points: float = 0.0
    magic_base: int = MAGIC_BASE
    flatten_hhmm: str = ""  # "15:55" local system clock; empty = off
    # Auto Start all / Stop live at local clock (empty = off)
    schedule_start_hhmm: str = ""
    schedule_stop_hhmm: str = ""
    # Per-account: do not mirror dry-run across other logins
    dry_run: bool = True
    # Optional override for data/<folder>/… (empty = derive from name)
    data_folder: str = ""
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

    def data_slug(self) -> str:
        custom = (self.data_folder or "").strip()
        return data_folder_slug(custom if custom else self.name)


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
    # Telegram: when False, this slot does not send alerts (manager toggle)
    notify_enabled: bool = True

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
    # Last global schedule values shown in Live manager (Apply copies onto accounts)
    global_start_hhmm: str = ""
    global_stop_hhmm: str = ""

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
                mag = None
                for candidate in range(lo, hi + 1):
                    if candidate not in seen:
                        mag = candidate
                        break
                if mag is None:
                    # Series exhausted — keep original and let magics_unique() fail closed
                    mag = int(slot.magic)
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
        for n in range(lo, hi + 1):
            if n not in used:
                return n
        raise ValueError(
            f"Magic series full for this account ({lo}–{hi}). "
            f"Max {MAGIC_SERIES_SIZE} slots per account — remove a slot or add another account."
        )

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

    def apply_global_schedule(
        self,
        *,
        start_hhmm: Optional[str] = None,
        stop_hhmm: Optional[str] = None,
        clear: bool = False,
    ) -> None:
        """Copy global start/stop onto every account (or clear schedules)."""
        if clear:
            self.global_start_hhmm = ""
            self.global_stop_hhmm = ""
            for acc in self.accounts:
                acc.schedule_start_hhmm = ""
                acc.schedule_stop_hhmm = ""
            return
        if start_hhmm is not None:
            self.global_start_hhmm = normalize_hhmm(start_hhmm)
            for acc in self.accounts:
                acc.schedule_start_hhmm = self.global_start_hhmm
        if stop_hhmm is not None:
            self.global_stop_hhmm = normalize_hhmm(stop_hhmm)
            for acc in self.accounts:
                acc.schedule_stop_hhmm = self.global_stop_hhmm

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
        "global_start_hhmm": normalize_hhmm(getattr(desk, "global_start_hhmm", "")),
        "global_stop_hhmm": normalize_hhmm(getattr(desk, "global_stop_hhmm", "")),
        "accounts": accounts,
        "slots": [asdict(s) for s in desk.slots],
    }


def desk_from_dict(raw: Optional[Dict[str, Any]]) -> LiveDesk:
    desk = LiveDesk()
    if not raw:
        desk.ensure_defaults()
        return desk
    desk.active_account_id = str(raw.get("active_account_id") or "")
    desk.global_start_hhmm = normalize_hhmm(str(raw.get("global_start_hhmm") or ""))
    desk.global_stop_hhmm = normalize_hhmm(str(raw.get("global_stop_hhmm") or ""))
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
            max_spread_points=float(row.get("max_spread_points") or 0.0),
            magic_base=magic_base,
            flatten_hhmm=normalize_hhmm(str(row.get("flatten_hhmm") or "")),
            schedule_start_hhmm=normalize_hhmm(str(row.get("schedule_start_hhmm") or "")),
            schedule_stop_hhmm=normalize_hhmm(str(row.get("schedule_stop_hhmm") or "")),
            dry_run=bool(row["dry_run"]) if "dry_run" in row else True,
            data_folder=str(row.get("data_folder") or ""),
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
            notify_enabled=bool(row.get("notify_enabled", True)),
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
