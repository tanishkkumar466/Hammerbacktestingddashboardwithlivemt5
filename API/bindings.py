"""
API algo bindings — map each API account to its own preset strategy + timeframe.

This is separate from Live desk slots (live_accounts.py). Live trading is unchanged.
Stored under API/bindings.json.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ORDER_MODES = ("market", "limit_entry", "limit_offset")


def coerce_order_mode(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in ORDER_MODES else "market"


def _float_or(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_bindings_path(app_dir: str) -> str:
    return os.path.join(app_dir, "API", "bindings.json")


@dataclass
class StrategyBinding:
    """One account runs one preset strategy on one timeframe (algo desk)."""

    id: str
    account_id: str
    preset_file: str
    timeframe: str
    symbol: str = "XAUUSD"
    magic: int = 1100
    volume: str = "0.01"
    enabled: bool = True
    dry_run: bool = True  # paper by default; uncheck when ready for real orders
    # Same choices as the Live tab Order type: market | limit_entry | limit_offset
    order_mode: str = "market"
    limit_offset_points: float = 0.0
    notes: str = ""
    # When this binding was last saved / assigned
    updated_at: str = ""
    # Copied from preset JSON at assign time (audit)
    preset_saved_at: str = ""

    @staticmethod
    def new(
        account_id: str,
        preset_file: str,
        timeframe: str,
        *,
        symbol: str = "XAUUSD",
        magic: int = 1100,
        volume: str = "0.01",
    ) -> "StrategyBinding":
        return StrategyBinding(
            id=uuid.uuid4().hex[:10],
            account_id=(account_id or "").strip(),
            preset_file=(preset_file or "").strip(),
            timeframe=(timeframe or "").strip(),
            symbol=(symbol or "XAUUSD").strip() or "XAUUSD",
            magic=int(magic),
            volume=str(volume or "0.01"),
            updated_at=_utc_now_iso(),
        )


@dataclass
class BindingStore:
    bindings: List[StrategyBinding] = field(default_factory=list)

    def by_id(self, binding_id: str) -> Optional[StrategyBinding]:
        for b in self.bindings:
            if b.id == binding_id:
                return b
        return None

    def by_account(self, account_id: str) -> List[StrategyBinding]:
        aid = (account_id or "").strip()
        return [b for b in self.bindings if b.account_id == aid]

    def enabled_bindings(self) -> List[StrategyBinding]:
        return [b for b in self.bindings if b.enabled and b.account_id and b.preset_file and b.timeframe]

    def upsert(self, binding: StrategyBinding) -> None:
        binding.updated_at = _utc_now_iso()
        for i, existing in enumerate(self.bindings):
            if existing.id == binding.id:
                self.bindings[i] = binding
                return
        self.bindings.append(binding)

    def remove(self, binding_id: str) -> bool:
        before = len(self.bindings)
        self.bindings = [b for b in self.bindings if b.id != binding_id]
        return len(self.bindings) < before

    def remove_for_account(self, account_id: str) -> int:
        aid = (account_id or "").strip()
        before = len(self.bindings)
        self.bindings = [b for b in self.bindings if b.account_id != aid]
        return before - len(self.bindings)


def binding_to_dict(b: StrategyBinding) -> Dict[str, Any]:
    return {
        "id": b.id,
        "account_id": b.account_id,
        "preset_file": b.preset_file,
        "timeframe": b.timeframe,
        "symbol": b.symbol,
        "magic": int(b.magic),
        "volume": b.volume,
        "enabled": bool(b.enabled),
        "dry_run": bool(getattr(b, "dry_run", True)),
        "order_mode": coerce_order_mode(b.order_mode),
        "limit_offset_points": float(b.limit_offset_points or 0.0),
        "notes": b.notes,
        "updated_at": b.updated_at,
        "preset_saved_at": b.preset_saved_at,
    }


def binding_from_dict(row: Dict[str, Any]) -> StrategyBinding:
    return StrategyBinding(
        id=str(row.get("id") or uuid.uuid4().hex[:10]),
        account_id=str(row.get("account_id") or "").strip(),
        preset_file=str(row.get("preset_file") or "").strip(),
        timeframe=str(row.get("timeframe") or "").strip(),
        symbol=str(row.get("symbol") or "XAUUSD").strip() or "XAUUSD",
        magic=int(row.get("magic") or 1100),
        volume=str(row.get("volume") or "0.01"),
        enabled=bool(row.get("enabled", True)),
        dry_run=bool(row["dry_run"]) if "dry_run" in row else True,
        order_mode=coerce_order_mode(row.get("order_mode")),
        limit_offset_points=_float_or(row.get("limit_offset_points"), 0.0),
        notes=str(row.get("notes") or "").strip(),
        updated_at=str(row.get("updated_at") or "").strip(),
        preset_saved_at=str(row.get("preset_saved_at") or "").strip(),
    )


def load_bindings(path: str) -> BindingStore:
    store = BindingStore()
    if not path or not os.path.isfile(path):
        return store
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return store
    rows = raw.get("bindings") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return store
    for row in rows:
        if isinstance(row, dict):
            store.bindings.append(binding_from_dict(row))
    return store


def save_bindings(path: str, store: BindingStore) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": _utc_now_iso(),
        "bindings": [binding_to_dict(b) for b in store.bindings],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
