"""
API fleet orchestrator — multi-account connect with staggered starts.

Does NOT replace Live trading. Uses HeadlessEngineClient → C++ engine.
Strategy loops still attach later via bindings; this module coordinates
safe multi-account terminal bring-up and parallel order fan-out requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .accounts import ApiAccount, ApiAccountStore
from .bindings import BindingStore, StrategyBinding
from .service import HeadlessEngineClient, build_connect_payload


# Broker firewall safeguard: stagger account process launches
DEFAULT_STAGGER_MS = 100
# Parallel order dispatch (C++ thread pool); Python only posts the batch
DEFAULT_ORDER_PARALLEL = True


@dataclass
class FleetAccountSpec:
    account_id: str
    login: str = ""
    password: str = ""
    server: str = ""
    terminal_path: str = ""
    name: str = ""
    # Optional strategy binding snapshot for the engine (audit / future use)
    preset_file: str = ""
    timeframe: str = ""
    symbol: str = ""
    magic: int = 0


@dataclass
class FleetStartPlan:
    """Payload for POST /fleet/connect — staggered multi-account launch."""

    accounts: List[FleetAccountSpec] = field(default_factory=list)
    stagger_ms: int = DEFAULT_STAGGER_MS

    def to_payload(self) -> Dict[str, Any]:
        return {
            "stagger_ms": max(0, int(self.stagger_ms)),
            "accounts": [
                {
                    "account_id": a.account_id,
                    "name": a.name,
                    "login": a.login,
                    "password": a.password,
                    "server": a.server,
                    "terminal_path": a.terminal_path,
                    "mt5_path": a.terminal_path,
                    "preset_file": a.preset_file,
                    "timeframe": a.timeframe,
                    "symbol": a.symbol,
                    "magic": int(a.magic or 0),
                }
                for a in self.accounts
                if a.account_id
            ],
        }


@dataclass
class OrderIntent:
    account_id: str
    symbol: str
    side: str  # buy | sell
    volume: str
    order_type: str = "market"  # market | limit
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = "hammer_api"


def build_fleet_plan_from_stores(
    accounts: ApiAccountStore,
    bindings: BindingStore,
    *,
    terminal_path: str = "",
    stagger_ms: int = DEFAULT_STAGGER_MS,
    only_enabled: bool = True,
) -> FleetStartPlan:
    """
    One fleet row per enabled binding (account can appear multiple times if
    multiple bindings — engine dedupes by account_id on connect).
    """
    by_id = {a.id: a for a in accounts.accounts}
    seen_accounts: set = set()
    specs: List[FleetAccountSpec] = []
    rows = bindings.enabled_bindings() if only_enabled else list(bindings.bindings)
    for b in rows:
        acc = by_id.get(b.account_id)
        if acc is None or not acc.enabled:
            continue
        if b.account_id in seen_accounts:
            # Same terminal once; keep first binding's strategy meta
            continue
        seen_accounts.add(b.account_id)
        path = (acc.terminal_path or "").strip() or (terminal_path or "").strip()
        specs.append(
            FleetAccountSpec(
                account_id=acc.id,
                name=acc.name,
                login=acc.login,
                password=acc.password,
                server=acc.server,
                terminal_path=path,
                preset_file=b.preset_file,
                timeframe=b.timeframe,
                symbol=b.symbol,
                magic=int(b.magic),
            )
        )
    return FleetStartPlan(accounts=specs, stagger_ms=stagger_ms)


def build_order_batch_payload(
    orders: List[OrderIntent],
    *,
    parallel: bool = DEFAULT_ORDER_PARALLEL,
) -> Dict[str, Any]:
    """POST /orders/batch — C++ fires all intents via thread pool (same ms window)."""
    return {
        "parallel": bool(parallel),
        "orders": [
            {
                "account_id": o.account_id,
                "symbol": o.symbol,
                "side": (o.side or "").lower(),
                "volume": o.volume,
                "order_type": o.order_type,
                "price": float(o.price or 0),
                "sl": float(o.sl or 0),
                "tp": float(o.tp or 0),
                "magic": int(o.magic or 0),
                "comment": o.comment or "hammer_api",
            }
            for o in orders
        ],
    }


class FleetClient:
    """Thin wrapper over HeadlessEngineClient for fleet + batch orders."""

    def __init__(self, engine: Optional[HeadlessEngineClient] = None):
        self.engine = engine or HeadlessEngineClient()

    def connect_fleet(self, plan: FleetStartPlan):
        return self.engine.request_fleet_connect(plan.to_payload())

    def connect_one(self, acc: ApiAccount, terminal_path: str = ""):
        path = (acc.terminal_path or "").strip() or (terminal_path or "").strip()
        payload = build_connect_payload(
            account_id=acc.id,
            name=acc.name,
            login=acc.login,
            password=acc.password,
            server=acc.server,
            terminal_path=path,
        )
        return self.engine.request_connect_account(payload)

    def place_orders_parallel(self, orders: List[OrderIntent], *, parallel: bool = True):
        return self.engine.request_orders_batch(
            build_order_batch_payload(orders, parallel=parallel)
        )
