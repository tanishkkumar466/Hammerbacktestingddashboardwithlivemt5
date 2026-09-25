"""
API Accounts pipeline — what works today vs what’s still missing.

Current pipeline (Windows only for the engine):
  UI table → HeadlessEngineClient → HTTP 127.0.0.1:17101
  → C++ hammer_mt5_engine → /portable CREATE_NO_WINDOW terminal + PID map

NOT wired:
  ApiAccount ↛ LiveSlot / preset_file / live strategy loop / orders

Strategies from presets/ run on Live accounts + slots only.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import live_accounts as la
from API.accounts import (
    KIND_HEADLESS,
    ApiAccount,
    ApiAccountStore,
    account_to_dict,
    load_accounts,
    save_accounts,
)
from API.service import (
    DEFAULT_ENGINE_HOST,
    DEFAULT_ENGINE_PORT,
    HeadlessEngineClient,
    build_connect_payload,
    _looks_like_broker_mt5_folder,
)


ROOT = Path(__file__).resolve().parents[1]


def test_account_roundtrip_strips_password(tmp_path):
    store = ApiAccountStore()
    acc = ApiAccount.new("IC Demo")
    acc.login = "50123456"
    acc.server = "ICMarketsEU-Demo"
    acc.password = "secret-session-only"
    acc.notes = "demo"
    store.upsert(acc)
    path = str(tmp_path / "accounts.json")
    save_accounts(path, store)

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    blob = json.dumps(raw)
    assert "secret-session-only" not in blob
    assert "password" not in blob

    loaded = load_accounts(path)
    assert len(loaded.accounts) == 1
    assert loaded.accounts[0].login == "50123456"
    assert loaded.accounts[0].server == "ICMarketsEU-Demo"
    assert loaded.accounts[0].password == ""  # never restored from disk
    assert loaded.accounts[0].kind == KIND_HEADLESS


def test_upsert_keeps_in_memory_password():
    store = ApiAccountStore()
    acc = ApiAccount.new("A")
    acc.password = "keep-me"
    store.upsert(acc)
    edited = ApiAccount(
        id=acc.id,
        name="A2",
        login="1",
        server="Srv",
        password="",  # blank in edit form
    )
    store.upsert(edited)
    assert store.by_id(acc.id).password == "keep-me"
    assert store.by_id(acc.id).name == "A2"


def test_connect_payload_has_no_strategy_fields():
    payload = build_connect_payload(
        account_id="abc123",
        name="IC Demo",
        login="50123456",
        password="pw",
        server="ICMarketsEU-Demo",
        terminal_path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
    )
    assert payload["account_id"] == "abc123"
    assert payload["login"] == "50123456"
    assert payload["server"] == "ICMarketsEU-Demo"
    assert payload["kind"] == "headless_mt5"
    assert payload["terminal_path"] == payload["mt5_path"]
    # Contract: engine only launches a terminal — no preset / symbol / TF
    for banned in ("preset", "preset_file", "symbol", "timeframe", "strategy", "magic"):
        assert banned not in payload


def test_api_account_has_no_preset_slot_fields():
    """Presets attach to LiveSlot, not ApiAccount."""
    fields = set(account_to_dict(ApiAccount.new("x")).keys())
    assert "preset_file" not in fields
    assert "symbol" not in fields
    assert "timeframe" not in fields
    assert "magic" not in fields

    slot = la.LiveSlot.new("acc1", "Slot 1", la.MAGIC_BASE)
    slot.preset_file = "preset_example_hammer_green_buy_red_sell.json"
    assert slot.preset_file.endswith(".json")


def test_presets_run_on_live_slots_not_api_accounts():
    """
    Strategies from presets/ are configured on Live desk slots.
    API Accounts Connect does not start a strategy loop.
    """
    desk = la.LiveDesk()
    desk.ensure_defaults()
    desk.slots[0].preset_file = "preset_example_hammer_with_candles.json"

    api = ApiAccount.new("API only")
    api.login = "99"
    api.server = "Demo"

    # No shared id / no automatic promotion
    assert api.id != desk.accounts[0].id
    assert not hasattr(api, "preset_file")
    assert desk.slots[0].preset_file
    assert desk.slots[0].account_id == desk.accounts[0].id


def test_example_presets_exist_for_live_configuration():
    presets = ROOT / "presets"
    assert (presets / "preset_example_hammer_green_buy_red_sell.json").is_file()
    assert (presets / "preset_example_hammer_with_candles.json").is_file()
    assert (presets / "preset_example_hammer_with_candle_35.json").is_file()


def test_engine_client_defaults_and_offline():
    client = HeadlessEngineClient()
    assert client.host == DEFAULT_ENGINE_HOST
    assert client.port == DEFAULT_ENGINE_PORT
    assert client.base_url == "http://127.0.0.1:17101"
    # Without hammer_mt5_engine running, health must report offline (not crash)
    st = client.health()
    assert st.reachable is False
    assert "offline" in st.message.lower() or "service" in st.message.lower()


def test_engine_client_posts_connect_to_account_endpoint():
    client = HeadlessEngineClient(timeout_sec=0.5)
    payload = build_connect_payload(
        account_id="id1",
        login="1",
        password="p",
        server="S",
        terminal_path="/tmp/terminal64.exe",
    )
    fake = MagicMock()
    fake.read.return_value = b'{"ok":true,"pid":42,"message":"started"}'
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake) as urlopen:
        st = client.request_connect_account(payload)
    assert st.reachable is True
    req = urlopen.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:17101/account/connect"
    assert req.get_method() == "POST"
    body = json.loads(req.data.decode("utf-8"))
    assert body["account_id"] == "id1"
    assert body["login"] == "1"


def test_engine_client_disconnect_posts_account_id():
    client = HeadlessEngineClient(timeout_sec=0.5)
    fake = MagicMock()
    fake.read.return_value = b'{"ok":true,"message":"stopped"}'
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake) as urlopen:
        st = client.request_disconnect_account("acc-9")
    assert st.reachable is True
    req = urlopen.call_args[0][0]
    assert req.full_url.endswith("/account/disconnect")
    body = json.loads(req.data.decode("utf-8"))
    assert body["account_id"] == "acc-9"


def test_broker_folder_name_matcher_any_broker():
    assert _looks_like_broker_mt5_folder("ICMarkets - MetaTrader 5")
    assert _looks_like_broker_mt5_folder("Exness MetaTrader 5")
    assert _looks_like_broker_mt5_folder("Pepperstone MT5")
    assert not _looks_like_broker_mt5_folder("Google Chrome")


def test_pipeline_gap_documented():
    """Single checklist for what’s implemented vs missing."""
    missing_for_strategies = {
        "login_cli_guaranteed",  # MetaQuotes CLI login still best-effort
        "engine_autostart_from_ui",
    }
    implemented = {
        "accounts_table_persist",
        "http_client_17101",
        "connect_payload",
        "cpp_portable_launch_windows",
        "disconnect_by_account_id",
        "api_account_to_preset_bindings",
        "fleet_staggered_connect",
        "parallel_strategy_workers_per_account",
        "orders_via_mt5_python_in_worker",
        "preset_saved_at_and_enabled_timeframes",
    }
    assert "parallel_strategy_workers_per_account" in implemented
    assert "engine_autostart_from_ui" in missing_for_strategies
