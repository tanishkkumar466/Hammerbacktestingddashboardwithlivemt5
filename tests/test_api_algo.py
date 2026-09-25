"""API algo desk — bindings, preset timestamps/TFs, fleet stagger, order batch."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from API.accounts import ApiAccount, ApiAccountStore
from API.bindings import (
    BindingStore,
    StrategyBinding,
    load_bindings,
    save_bindings,
)
from API.fleet import (
    DEFAULT_STAGGER_MS,
    FleetClient,
    OrderIntent,
    build_fleet_plan_from_stores,
    build_order_batch_payload,
)
from API.preset_meta import (
    enabled_timeframes_from_preset,
    stamp_preset_meta,
)
from API.service import HeadlessEngineClient, build_connect_payload


ROOT = Path(__file__).resolve().parents[1]


def test_stamp_preset_meta_writes_saved_at_and_enabled_tfs():
    payload = {
        "version": 2,
        "pattern": "Hammer",
        "timeframes": {
            "3m": {"enabled": True, "rr": "2"},
            "5m": {"enabled": False, "rr": "2"},
            "15m": {"enabled": True, "rr": "2"},
        },
    }
    stamped = stamp_preset_meta(payload)
    assert stamped["saved_at"]
    assert "T" in stamped["saved_at"]
    assert stamped["enabled_timeframes"] == ["3m", "15m"]


def test_enabled_timeframes_prefers_explicit_list():
    data = {
        "enabled_timeframes": ["1h"],
        "timeframes": {"3m": {"enabled": True}},
    }
    assert enabled_timeframes_from_preset(data) == ["1h"]


def test_binding_roundtrip(tmp_path):
    store = BindingStore()
    b = StrategyBinding.new("acc1", "hammer.json", "3m", symbol="XAUUSD", magic=1101)
    b.preset_saved_at = "2026-09-24T06:00:00+00:00"
    store.upsert(b)
    path = str(tmp_path / "bindings.json")
    save_bindings(path, store)
    loaded = load_bindings(path)
    assert len(loaded.bindings) == 1
    assert loaded.bindings[0].account_id == "acc1"
    assert loaded.bindings[0].preset_file == "hammer.json"
    assert loaded.bindings[0].timeframe == "3m"
    assert loaded.bindings[0].preset_saved_at.startswith("2026")


def test_different_accounts_different_strategies():
    store = BindingStore()
    store.upsert(StrategyBinding.new("a1", "preset_a.json", "3m"))
    store.upsert(StrategyBinding.new("a2", "preset_b.json", "15m"))
    by_a1 = store.by_account("a1")
    by_a2 = store.by_account("a2")
    assert by_a1[0].preset_file == "preset_a.json"
    assert by_a2[0].preset_file == "preset_b.json"
    assert by_a1[0].timeframe != by_a2[0].timeframe


def test_fleet_plan_stagger_and_dedupe_accounts():
    accounts = ApiAccountStore()
    a1 = ApiAccount.new("One")
    a1.id = "a1"
    a1.login = "1"
    a1.server = "S"
    a1.password = "p"
    a2 = ApiAccount.new("Two")
    a2.id = "a2"
    a2.login = "2"
    a2.server = "S"
    a2.password = "p"
    accounts.upsert(a1)
    accounts.upsert(a2)

    bindings = BindingStore()
    bindings.upsert(StrategyBinding.new("a1", "p1.json", "3m"))
    bindings.upsert(StrategyBinding.new("a1", "p1b.json", "5m"))  # second binding same account
    bindings.upsert(StrategyBinding.new("a2", "p2.json", "15m"))

    plan = build_fleet_plan_from_stores(accounts, bindings, stagger_ms=100)
    assert plan.stagger_ms == 100
    assert len(plan.accounts) == 2  # deduped a1
    payload = plan.to_payload()
    assert payload["stagger_ms"] == DEFAULT_STAGGER_MS or payload["stagger_ms"] == 100
    assert all("password" in row for row in payload["accounts"])


def test_order_batch_payload_parallel_flag():
    orders = [
        OrderIntent("a1", "XAUUSD", "buy", "0.01", magic=1100),
        OrderIntent("a2", "XAUUSD", "sell", "0.02", magic=1200),
    ]
    payload = build_order_batch_payload(orders, parallel=True)
    assert payload["parallel"] is True
    assert len(payload["orders"]) == 2
    assert payload["orders"][0]["side"] == "buy"


def test_fleet_client_posts_staggered_connect():
    client = FleetClient(HeadlessEngineClient(timeout_sec=0.5))
    accounts = ApiAccountStore()
    acc = ApiAccount.new("A")
    acc.id = "x1"
    acc.login = "9"
    acc.server = "Demo"
    acc.password = "pw"
    accounts.upsert(acc)
    bindings = BindingStore()
    bindings.upsert(StrategyBinding.new("x1", "p.json", "3m"))
    plan = build_fleet_plan_from_stores(accounts, bindings, stagger_ms=100)

    fake = MagicMock()
    fake.read.return_value = b'{"ok":true,"message":"fleet started 1/1"}'
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=fake) as urlopen:
        st = client.connect_fleet(plan)
    assert st.reachable is True
    req = urlopen.call_args[0][0]
    assert req.full_url.endswith("/fleet/connect")
    body = json.loads(req.data.decode("utf-8"))
    assert body["stagger_ms"] == 100
    assert body["accounts"][0]["account_id"] == "x1"


def test_orders_batch_client_posts_parallel():
    engine = HeadlessEngineClient(timeout_sec=0.5)
    fake = MagicMock()
    fake.read.return_value = b'{"ok":true,"message":"dispatched 2"}'
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    payload = build_order_batch_payload(
        [OrderIntent("a", "XAUUSD", "buy", "0.01"), OrderIntent("b", "XAUUSD", "sell", "0.01")],
        parallel=True,
    )
    with patch("urllib.request.urlopen", return_value=fake) as urlopen:
        st = engine.request_orders_batch(payload)
    assert st.reachable is True
    assert urlopen.call_args[0][0].full_url.endswith("/orders/batch")


def test_example_presets_have_saved_at():
    for name in (
        "preset_example_hammer_green_buy_red_sell.json",
        "preset_example_hammer_with_candles.json",
        "preset_example_hammer_with_candle_35.json",
    ):
        path = ROOT / "template" / name
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("saved_at"), name
        # After stamp, enabled_timeframes is derived
        stamped = stamp_preset_meta(data)
        assert stamped["enabled_timeframes"]


def test_connect_payload_still_has_no_strategy_fields():
    """Single-account connect remains terminal-only; strategy lives in bindings/workers."""
    payload = build_connect_payload(
        account_id="id",
        login="1",
        password="p",
        server="S",
        terminal_path="/x/terminal64.exe",
    )
    assert "preset_file" not in payload


def test_build_api_trade_start_from_preset(tmp_path):
    from API.accounts import ApiAccount
    from API.bindings import StrategyBinding
    from API.runtime import build_api_trade_start, api_worker_key

    preset = {
        "version": 2,
        "saved_at": "2026-09-24T00:00:00+00:00",
        "pattern": "Hammer",
        "fields": {"classic_green": "BUY", "classic_red": "SELL"},
        "timeframes": {"3m": {"enabled": True, "rr": "2", "sl": "20"}},
        "enabled_timeframes": ["3m"],
    }
    path = tmp_path / "hammer.json"
    path.write_text(json.dumps(preset), encoding="utf-8")

    acc = ApiAccount.new("Demo")
    acc.login = "501"
    acc.server = "Demo"
    acc.password = "pw"
    binding = StrategyBinding.new(acc.id, "hammer.json", "3m", magic=1105)
    binding.dry_run = True

    start = build_api_trade_start(
        acc, binding, presets_dir=str(tmp_path), terminal_path=r"C:\t\terminal64.exe"
    )
    assert start.worker_key == api_worker_key(acc.id)
    assert start.live_config.timeframe_label == "3m"
    assert start.live_config.magic == 1105
    assert start.live_config.dry_run is True
    assert start.pattern_type == "hammer"
    assert start.credentials.login == 501


def test_portable_path_uses_account_id():
    from API.paths import portable_terminal_candidates

    cands = portable_terminal_candidates("abc123")
    assert cands
    assert "abc123" in cands[0]
    assert cands[0].endswith("terminal64.exe")
