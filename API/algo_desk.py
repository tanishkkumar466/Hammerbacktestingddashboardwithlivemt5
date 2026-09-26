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
from live_history import entry_offset_ignored_warning

from .accounts import ApiAccount, ApiAccountStore
from .bindings import BindingStore, StrategyBinding
from .fleet import DEFAULT_STAGGER_MS, FleetClient, FleetStartPlan, build_fleet_plan_from_stores
from .paths import portable_terminal_path, prepare_portable_terminal
from .runtime import (
    api_slot_id,
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
    already_running: List[str] = field(default_factory=list)
    fleet_status: Optional[EngineStatus] = None
    warnings: List[str] = field(default_factory=list)


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
    shared_live_settings: Optional[Dict[str, Any]] = None,
    ensure_engine: Optional[Callable[[], EngineStatus]] = None,
) -> AlgoStartResult:
    """
    1) Validate every account + build its Live payload (nothing launched yet)
    2) Prepare isolated portable MT5 copies, start the C++ engine if needed
    3) C++ staggered fleet launch for the valid accounts only
    4) One worker process per account: connect, then start the strategy slot
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

    errors: List[str] = []
    warnings: List[str] = []
    already_running: List[str] = []
    by_id = {a.id: a for a in accounts.accounts}

    jobs = []
    for spec in plan.accounts:
        acc = by_id.get(spec.account_id)
        if acc is None:
            continue
        binding = _primary_binding(bindings, acc.id)
        if binding is None:
            errors.append(f"{acc.name}: no strategy binding")
            continue
        existing = handles.get(api_worker_key(acc.id))
        if (
            existing is not None
            and existing.is_alive()
            and api_slot_id(binding.id) in existing.running_slots
        ):
            already_running.append(acc.id)
            _log(f"[API] {acc.name}: strategy already running — skipped.")
            continue
        try:
            term = portable_terminal_path(acc.id)
            source = (spec.terminal_path or "").strip()
            if source:
                prepared, note = prepare_portable_terminal(acc.id, source)
                _log(f"[API] {acc.name}: {note}")
                if prepared:
                    term = prepared
            start = build_api_trade_start(
                acc,
                binding,
                presets_dir=presets_dir,
                terminal_path=term,
                journal_dir=journal_dir,
                shared_live_settings=shared_live_settings,
            )
            offset_warning = entry_offset_ignored_warning(
                start.strategy_config,
                start.pattern_type,
                start.live_config.order_mode,
                bool(getattr(start.live_config, "limit_offset_from_market", True)),
            )
            if offset_warning:
                _log(f"[API] {acc.name}: {offset_warning}")
                warnings.append(f"{acc.name}: {offset_warning}")
            jobs.append((acc, binding, start, spec))
        except Exception as exc:
            errors.append(f"{acc.name}: {exc}")

    def _result(ok: bool, message: str, started: List[str], fleet_st=None) -> AlgoStartResult:
        if errors:
            message += " Issues: " + "; ".join(errors[:4])
        return AlgoStartResult(
            ok=ok,
            message=message,
            started=started,
            errors=errors,
            already_running=already_running,
            fleet_status=fleet_st,
            warnings=warnings,
        )

    if not jobs:
        if already_running:
            return _result(True, f"{len(already_running)} account(s) already running.", [])
        return _result(False, "Nothing started.", [])

    if ensure_engine is not None:
        eng_st = ensure_engine()
        if not (eng_st.reachable and eng_st.ok):
            _log(f"[API] Engine: {eng_st.message}")
            return _result(False, f"MT5 API engine not available — {eng_st.message}", [], eng_st)

    client = FleetClient(engine or HeadlessEngineClient())
    job_plan = FleetStartPlan(accounts=[spec for *_rest, spec in jobs], stagger_ms=plan.stagger_ms)
    fleet_st = client.connect_fleet(job_plan)
    _log(f"[API] C++ fleet: {fleet_st.message}")
    if not fleet_st.reachable:
        return _result(
            False,
            "C++ API service offline — the MT5 API engine is not running.",
            [],
            fleet_st,
        )
    if not fleet_st.ok:
        rows = fleet_st.results()
        for (acc, *_rest), row in zip(jobs, rows):
            if not row.get("ok", True):
                _log(
                    f"[API] {acc.name}: engine could not open the terminal "
                    f"({row.get('message') or 'unknown'}) — the worker will open it directly."
                )

    # Brief settle so portable terminals finish boot before MT5 initialize
    if settle_sec > 0:
        time.sleep(min(settle_sec, 5.0))

    started: List[str] = []
    for acc, binding, start, _spec in jobs:
        try:
            handle = ensure_api_handle(handles, acc)
            _log(
                f"[API] {acc.name}: strategy {binding.preset_file} @ {binding.timeframe} "
                f"→ {start.credentials.terminal_path or '(install path)'}"
            )
            handle.send(
                "connect",
                terminal_path=start.credentials.terminal_path,
                login=start.credentials.login,
                password=start.credentials.password,
                server=start.credentials.server,
                portable=start.credentials.portable,
            )
            # Worker handles commands in order: start_slot runs after connect finishes.
            handle.send("start_slot", **start_slot_kwargs(start))
            started.append(acc.id)
            _log(
                f"[API] {acc.name}: start sent "
                f"({'DRY-RUN' if start.live_config.dry_run else 'LIVE ORDERS'}, "
                f"order={start.live_config.order_mode}) — waiting for MT5 login."
            )
        except Exception as exc:
            errors.append(f"{acc.name}: start failed — {exc}")

    msg = f"Start sent to {len(started)}/{len(plan.accounts)} account(s)."
    if already_running:
        msg += f" {len(already_running)} already running."
    if started:
        msg += (
            " Watch the Live log: '[API] Connected …' then 'Strategy running …' "
            "confirms each account; a login problem shows as an error there."
        )
    return _result(bool(started) or bool(already_running), msg, started, fleet_st)
