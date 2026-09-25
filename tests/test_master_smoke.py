"""
Cross-cutting smoke checks — strategy, presets, Live vs API isolation, package imports.

Run via:  python scripts/run_all_tests.py --smoke
Or:       pytest tests/test_master_smoke.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import live_accounts as la
from API.accounts import ApiAccount, ApiAccountStore
from API.algo_desk import ensure_api_handle, stop_api_trading
from API.bindings import BindingStore, StrategyBinding
from API.runtime import api_slot_id, api_worker_key, build_api_trade_start, strategy_from_preset
from API.paths import portable_terminal_candidates
from API.preset_meta import stamp_preset_meta
from live_account_ipc import AccountWorkerHandle

ROOT = Path(__file__).resolve().parents[1]


def test_packages_import():
    import backtest  # noqa: F401
    import broker  # noqa: F401
    import doji_logic  # noqa: F401
    import hammer_context_logic  # noqa: F401
    import live  # noqa: F401
    import logic  # noqa: F401
    import notification  # noqa: F401
    from notification import TelegramNotifier, NotificationManager  # noqa: F401
    from indicators import rsi, rolling_vwap  # noqa: F401
    import API  # noqa: F401


def test_hammer_strategy_self_test_passes():
    import logic

    cfg = logic.StrategyConfig()
    lines = logic.run_hammer_direction_self_test(cfg)
    assert lines
    assert not any("FAIL" in ln for ln in lines)


def test_example_presets_build_strategy_configs():
    for name in (
        "preset_example_hammer_green_buy_red_sell.json",
        "preset_example_hammer_with_candles.json",
        "preset_example_hammer_with_candle_35.json",
    ):
        path = ROOT / "template" / name
        data = json.loads(path.read_text(encoding="utf-8"))
        stamped = stamp_preset_meta(data)
        assert stamped.get("saved_at")
        assert stamped.get("enabled_timeframes")
        label, ptype, _cfg, _stack = strategy_from_preset(stamped)
        assert label
        assert ptype in ("hammer", "hammer_context", "hammer_context_35") or "hammer" in ptype


def test_live_and_api_stores_are_separate():
    desk = la.LiveDesk()
    desk.ensure_defaults()
    live_ids = {a.id for a in desk.accounts}

    api_store = ApiAccountStore()
    api_acc = ApiAccount.new("API Demo")
    api_store.upsert(api_acc)

    assert api_worker_key(api_acc.id).startswith("api:")
    assert api_worker_key(api_acc.id) not in live_ids
    assert api_slot_id("xyz") == "api-xyz"
    assert api_acc.id not in live_ids or api_acc.id != desk.accounts[0].id


def test_api_worker_keys_never_collide_with_live_handles():
    live_ids = {"a1", "b2", "c3"}
    for lid in live_ids:
        assert api_worker_key(lid) not in live_ids
        assert api_worker_key(lid).startswith("api:")


def test_api_trade_start_uses_isolated_slot_and_worker_keys(tmp_path):
    preset = {
        "version": 2,
        "pattern": "Hammer",
        "fields": {"classic_green": "BUY", "classic_red": "SELL"},
        "timeframes": {"3m": {"enabled": True, "rr": "2", "sl": "15"}},
        "enabled_timeframes": ["3m"],
    }
    (tmp_path / "p.json").write_text(json.dumps(preset), encoding="utf-8")
    acc = ApiAccount.new("Iso")
    acc.login = "1001"
    acc.server = "Demo"
    acc.password = "x"
    binding = StrategyBinding.new(acc.id, "p.json", "3m", magic=1201)
    start = build_api_trade_start(acc, binding, presets_dir=str(tmp_path))
    assert start.worker_key.startswith("api:")
    assert start.slot_id.startswith("api-")
    assert start.live_config.magic == 1201


def test_portable_terminal_path_namespaced_by_account():
    a = portable_terminal_candidates("accA")[0]
    b = portable_terminal_candidates("accB")[0]
    assert "accA" in a and "accB" in b
    assert a != b


def test_ensure_api_handle_uses_api_prefix_not_live_dict():
    handles: dict = {}
    acc = ApiAccount.new("H")
    h = ensure_api_handle(handles, acc)
    assert isinstance(h, AccountWorkerHandle)
    key = api_worker_key(acc.id)
    assert key in handles
    assert key.startswith("api:")
    # Do not leave a live process running in unit tests
    stop_api_trading(handles)
