"""
API algo desk — C++ fleet (isolated terminals) + parallel strategy workers.

Flow:
  1. C++ hammer_mt5_engine POST /fleet/connect — staggered /portable terminals
  2. One AccountWorkerHandle process per API account (MT5 is process-global)
  3. Each worker loads that account’s saved preset and runs LiveTradingEngine
     against the portable terminal path (isolated data folder + parallel)

Live desk is untouched.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from live_account_ipc import AccountWorkerHandle

from .accounts import ApiAccount, ApiAccountStore
from .bindings import BindingStore, StrategyBinding
from .fleet import DEFAULT_STAGGER_MS, FleetClient, build_fleet_plan_from_stores
from .paths import portable_terminal_path
from .runtime import (
    api_worker_key,
    build_api_trade_start,
    start_slot_kwargs,
)
from .service import HeadlessEngineClient, EngineStatus


LogFn = Callable[[str], None]


@dataclass
class AlgoStartResult:
    ok: bool
    message: str
    started: List[str] = field(default_factory=list)  # account ids
    errors: List[str] = field(default_factory=list)
    fleet_status: Optional[EngineStatus] = None


def _primary_binding(store: BindingStore, account_id: str) -> Optional[StrategyBinding]:
    rows = store.by_account(account_id)
    for b in rows:
        if b.enabled and b.preset_file and b.timeframe:
            return b
    return None


def ensure_api_handle(
    handles: Dict[str, AccountWorkerHandle],
    acc: ApiAccount,
) -> AccountWorkerHandle:
    key = api_worker_key(acc.id)
    handle = handles.get(key)
    if handle is None:
        handle = AccountWorkerHandle(account_id=key, account_name=f"API:{acc.name}")
        handles[key] = handle
    else:
        handle.account_name = f"API:{acc.name}"
    if not handle.is_alive():
        handle.start()
    return handle


def stop_api_trading(
    handles: Dict[str, AccountWorkerHandle],
    *,
    account_id: Optional[str] = None,
    log: Optional[LogFn] = None,
) -> None:
    """Stop strategy workers (and optionally one account). Terminals stay until C++ disconnect."""
    keys = []
    if account_id:
        keys = [api_worker_key(account_id)]
    else:
        keys = [k for k in list(handles.keys()) if str(k).startswith("api:")]
    for key in keys:
        handle = handles.get(key)
        if handle is None:
            continue
        try:
            if handle.is_alive():
                handle.send("stop_all_slots")
                handle.send("disconnect")
                handle.shutdown(timeout=4.0)
        except Exception as exc:
            if log:
                log(f"[API] stop {key}: {exc}")
        handles.pop(key, None)
        if log:
            log(f"[API] Stopped strategy worker {key}")


def start_api_trading_parallel(
    *,
    accounts: ApiAccountStore,
    bindings: BindingStore,
    handles: Dict[str, AccountWorkerHandle],
    presets_dir: str,
    journal_dir: str = "",
    install_terminal_path: str = "",
    engine: Optional[HeadlessEngineClient] = None,
    stagger_ms: int = DEFAULT_STAGGER_MS,
    settle_sec: float = 1.5,
    log: Optional[LogFn] = None,
) -> AlgoStartResult:
    """
    1) C++ staggered fleet launch (isolated portable folders)
    2) Parallel per-account workers running saved presets
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    plan = build_fleet_plan_from_stores(
        accounts,
        bindings,
        terminal_path=install_terminal_path,
        stagger_ms=stagger_ms,
    )
    if not plan.accounts:
        return AlgoStartResult(
            False,
            "No enabled accounts with strategy bindings.",
        )

    client = FleetClient(engine or HeadlessEngineClient())
    fleet_st = client.connect_fleet(plan)
    _log(f"[API] C++ fleet: {fleet_st.message}")
    if not fleet_st.reachable:
        return AlgoStartResult(
            False,
            "C++ API service offline — build/run API/engine (Windows) first.",
            fleet_status=fleet_st,
        )

    # Brief settle so portable terminals finish boot before MT5 initialize
    if settle_sec > 0:
        time.sleep(min(settle_sec, 5.0))

    started: List[str] = []
    errors: List[str] = []
    by_id = {a.id: a for a in accounts.accounts}

    # Start all workers first (parallel processes), then connect/start each.
    jobs = []
    for spec in plan.accounts:
        acc = by_id.get(spec.account_id)
        if acc is None:
            continue
        binding = _primary_binding(bindings, acc.id)
        if binding is None:
            errors.append(f"{acc.name}: no strategy binding")
            continue
        try:
            portable = portable_terminal_path(acc.id)
            # Prefer C++ portable copy; fall back to install path if copy not ready
            term = portable if portable else (install_terminal_path or acc.terminal_path or "")
            start = build_api_trade_start(
                acc,
                binding,
                presets_dir=presets_dir,
                terminal_path=term,
                journal_dir=journal_dir,
            )
            handle = ensure_api_handle(handles, acc)
            jobs.append((acc, binding, start, handle, term))
        except Exception as exc:
            errors.append(f"{acc.name}: {exc}")

    # Connect + start_slot on every worker — each process is isolated (parallel calc)
    for acc, binding, start, handle, term in jobs:
        try:
            _log(
                f"[API] {acc.name}: strategy {binding.preset_file} @ {binding.timeframe} "
                f"→ portable {term or '(install path)'}"
            )
            handle.send(
                "connect",
                terminal_path=start.credentials.terminal_path,
                login=start.credentials.login,
                password=start.credentials.password,
                server=start.credentials.server,
            )
            # Give connect a moment; events polled by UI
            time.sleep(0.15)
            handle.send("start_slot", **start_slot_kwargs(start))
            started.append(acc.id)
            _log(
                f"[API] {acc.name}: trading started "
                f"({'DRY-RUN' if start.live_config.dry_run else 'LIVE ORDERS'})"
            )
        except Exception as exc:
            errors.append(f"{acc.name}: start failed — {exc}")

    ok = bool(started) and not errors
    if started and errors:
        ok = True  # partial success
    msg = (
        f"Started {len(started)}/{len(plan.accounts)} account(s) in parallel "
        f"(C++ terminals + strategy workers)."
    )
    if errors:
        msg += " Issues: " + "; ".join(errors[:4])
    return AlgoStartResult(
        ok=ok or bool(started),
        message=msg,
        started=started,
        errors=errors,
        fleet_status=fleet_st,
    )
