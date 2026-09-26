"""API Accounts desk — preset → Live payload, start flow, worker protocol, persistence."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

import doji_logic
import live as live_trading
from API import algo_desk
from API.accounts import ApiAccount, ApiAccountStore, load_accounts, save_accounts
from API.bindings import BindingStore, StrategyBinding, load_bindings, save_bindings
from API.runtime import (
    SHARED_LIVE_SETTING_KEYS,
    api_slot_id,
    api_worker_key,
    build_api_trade_start,
    shared_live_settings_from,
    strategy_from_preset,
)
from API.service import EngineStatus

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "template"
HAMMER = "preset_example_hammer_green_buy_red_sell.json"
HWC = "preset_example_hammer_with_candles.json"
HWC35 = "preset_example_hammer_with_candle_35.json"


def _account(name="IC Demo", login="50123456", password="secret"):
    acc = ApiAccount.new(name)
    acc.login = login
    acc.server = "ICMarketsSC-Demo"
    acc.password = password
    return acc


def _binding(acc, preset=HAMMER, tf="5m", volume="0.01", dry_run=True):
    b = StrategyBinding.new(acc.id, preset, tf, symbol="XAUUSD", magic=1155, volume=volume)
    b.dry_run = dry_run
    return b


def _start(acc, binding, presets_dir=TEMPLATES, **kw):
    return build_api_trade_start(
        acc, binding, presets_dir=str(presets_dir), terminal_path=r"C:\MT5\terminal64.exe", **kw
    )


def _preset(name):
    return json.loads((TEMPLATES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- order type (account setting)

@pytest.mark.parametrize("preset", [HAMMER, HWC, HWC35])
def test_order_type_defaults_to_market_for_every_pattern(preset):
    acc = _account()
    start = _start(acc, _binding(acc, preset))
    assert start.live_config.order_mode == "market"


def test_hwc35_keeps_its_35_pullback_signal_with_market_order():
    acc = _account()
    start = _start(acc, _binding(acc, HWC35))
    assert start.pattern_type == "hammer_with_candles_35"
    assert start.strategy_config.entry_pullback_pct == pytest.approx(35.0)
    assert start.live_config.order_mode == "market"


@pytest.mark.parametrize("mode", ["market", "limit_entry", "limit_offset"])
def test_order_type_comes_from_account_binding(mode):
    acc = _account()
    b = _binding(acc, HWC35)
    b.order_mode = mode
    b.limit_offset_points = 25.0
    cfg = _start(acc, b).live_config
    assert cfg.order_mode == mode
    assert cfg.limit_offset_points == 25.0


def test_unknown_order_type_falls_back_to_market():
    acc = _account()
    b = _binding(acc)
    b.order_mode = "stop_hunt"
    assert _start(acc, b).live_config.order_mode == "market"


def _resolve_with(start, sig_kwargs):
    import logic
    from datetime import datetime

    class _Broker:
        def get_tick_prices(self, sym):
            return (2001.0, 2001.2)

        def symbol_point(self, sym):
            return 0.01

    eng = live_trading.LiveTradingEngine.__new__(live_trading.LiveTradingEngine)
    eng.live_config = start.live_config
    eng.broker = _Broker()
    eng._broker_lock = threading.Lock()
    eng.strategy_config = start.strategy_config
    eng.pattern_type = start.pattern_type
    eng.log = lambda *_a, **_k: None
    bar = logic.Candle(datetime(2026, 1, 5, 10), 1998.0, 2000.0, 1990.5, 1997.7)
    sig = logic.TradeSignal(
        direction=logic.TradeDirection.BUY, hammer_candle=bar, entry_candle=bar,
        entry_price=1995.0, stop_loss=1990.0, risk=5.0, rr_multiple=2.0, target=2005.0,
        timeframe="1h", **sig_kwargs,
    )
    return eng._resolve_live_order(sig, "XAUUSD")


PULLBACK_SIG = dict(await_limit_fill=True, signal_entry_price=1997.7, entry_pullback_pct=35.0)


def test_hwc35_market_order_enters_at_ask_with_candle_sl_kept():
    acc = _account()
    start = _start(acc, _binding(acc, HWC35))
    mode, limit_price, sl, tp, exec_entry = _resolve_with(start, PULLBACK_SIG)
    assert mode == "market" and limit_price is None
    assert exec_entry == pytest.approx(2001.2)  # ask
    assert sl == pytest.approx(1990.0)  # candle extreme ± buffer, not slid


def test_hwc35_limit_order_when_account_chooses_limit():
    acc = _account()
    b = _binding(acc, HWC35)
    b.order_mode = "limit_entry"
    mode, limit_price, sl, tp, exec_entry = _resolve_with(_start(acc, b), PULLBACK_SIG)
    assert mode == "limit_entry"
    assert limit_price == pytest.approx(1995.0)
    assert (sl, tp) == (pytest.approx(1990.0), pytest.approx(2005.0))


# ---------------------------------------------------------------- Doji + indicators

def test_doji_preset_supported_on_api_desk(tmp_path):
    data = _preset(HAMMER)
    data["pattern"] = "Doji"
    data["fields"].update({
        "doji_max_body_pct": "7.5",
        "doji_style": "DRAGONFLY",
        "doji_direction_mode": doji_logic.DojiDirectionMode.WICK_BIAS.value,
        "doji_fallback_direction": "SELL",
        "doji_allow_red_trades": False,
        "sl_buffer_flat": "1.25",
    })
    (tmp_path / "doji.json").write_text(json.dumps(data), encoding="utf-8")
    label, ptype, cfg, _stack = strategy_from_preset(data)
    assert (label, ptype) == ("Doji", "doji")
    assert isinstance(cfg, doji_logic.DojiStrategyConfig)
    assert cfg.doji_ratios.max_body_pct == pytest.approx(7.5)
    assert cfg.doji_style == doji_logic.DojiStyle.DRAGONFLY
    assert cfg.fallback_direction.value == "SELL"
    assert cfg.allow_red_trades is False
    assert cfg.sl_buffer_flat == pytest.approx(1.25)

    acc = _account()
    start = _start(acc, _binding(acc, "doji.json"), presets_dir=tmp_path)
    assert start.pattern_type == "doji"
    assert start.live_config.order_mode == "market"


def test_doji_bad_enum_falls_back_to_default():
    data = {"pattern": "Doji", "fields": {"doji_style": "NOT_A_STYLE"}}
    _l, _t, cfg, _s = strategy_from_preset(data)
    assert cfg.doji_style == doji_logic.DojiStrategyConfig().doji_style


def test_indicator_values_clamped_like_dashboard():
    data = _preset(HAMMER)
    data["fields"].update({
        "indicators_rsi_period": "0",
        "indicators_rsi_buy_above": "150",
        "indicators_rsi_sell_below": "-5",
        "indicators_rolling_vwap_period": "0",
    })
    _l, _t, _c, stack = strategy_from_preset(data)
    assert stack.rsi.period >= 2
    assert 0 <= stack.rsi.buy_above <= 100
    assert 0 <= stack.rsi.sell_below <= 100
    assert stack.rolling_vwap.period >= 1


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("login", ["5012 3456", "abc", "50123456x"])
def test_non_numeric_login_rejected(login):
    acc = _account(login=login)
    with pytest.raises(ValueError, match="numeric"):
        _start(acc, _binding(acc))


def test_missing_password_rejected():
    acc = _account(password="")
    with pytest.raises(ValueError, match="password"):
        _start(acc, _binding(acc))


@pytest.mark.parametrize("vol,expected", [("0.05", 0.05), ("0,05", 0.05), (" 0.10 ", 0.10)])
def test_volume_parsing(vol, expected):
    acc = _account()
    assert _start(acc, _binding(acc, volume=vol)).live_config.volume == pytest.approx(expected)


@pytest.mark.parametrize("vol", ["abc", "0", "-0.01"])
def test_bad_volume_rejected_not_silently_replaced(vol):
    acc = _account()
    with pytest.raises(ValueError, match="[Vv]olume"):
        _start(acc, _binding(acc, volume=vol))


def test_missing_preset_file_reported():
    acc = _account()
    with pytest.raises(FileNotFoundError):
        _start(acc, _binding(acc, "does_not_exist.json"))


# ---------------------------------------------------------------- Live-tab safety settings

def test_shared_live_settings_applied_and_unknown_keys_ignored():
    acc = _account()
    shared = {
        "max_daily_loss_usd": 250.0,
        "max_daily_trades": 7,
        "max_lot_size": 0.5,
        "max_spread_points": 30.0,
        "notification_bots": [{"name": "desk"}],
        "symbol": "EURUSD",  # not shared — binding owns the symbol
        "dry_run": False,  # not shared — binding owns dry-run
    }
    cfg = _start(acc, _binding(acc), shared_live_settings=shared).live_config
    assert cfg.max_daily_loss_usd == 250.0
    assert cfg.max_daily_trades == 7
    assert cfg.max_lot_size == 0.5
    assert cfg.max_spread_points == 30.0
    assert cfg.notification_bots == [{"name": "desk"}]
    assert cfg.symbol == "XAUUSD"
    assert cfg.dry_run is True


def test_shared_live_settings_from_live_config_roundtrip():
    live_cfg = live_trading.LiveRunConfig(
        symbol="XAUUSD", timeframe_label="5m", volume=0.01, magic=1, max_open_positions=1,
        poll_interval_sec=2.0, max_daily_loss_usd=42.0,
    )
    shared = shared_live_settings_from(live_cfg)
    assert set(shared) == set(SHARED_LIVE_SETTING_KEYS)
    assert shared["max_daily_loss_usd"] == 42.0


def test_real_orders_blocked_when_volume_above_lot_cap():
    acc = _account()
    b = _binding(acc, volume="0.50", dry_run=False)
    with pytest.raises(ValueError, match="Max lot cap"):
        _start(acc, b, shared_live_settings={"max_lot_size": 0.10})
    # Dry-run is never blocked by risk limits
    b.dry_run = True
    assert _start(acc, b, shared_live_settings={"max_lot_size": 0.10}).live_config.dry_run


def test_default_api_config_passes_live_risk_validation_for_real_orders():
    acc = _account()
    cfg = _start(acc, _binding(acc, dry_run=False)).live_config
    assert live_trading.validate_risk_config(cfg, real_money=True) is None


# ---------------------------------------------------------------- start flow (fake engine + workers)

class _FakeEngine:
    def __init__(self, reachable=True):
        self.reachable = reachable
        self.fleet_payloads = []

    def request_fleet_connect(self, payload):
        self.fleet_payloads.append(payload)
        if not self.reachable:
            return EngineStatus(False, "offline")
        return EngineStatus(True, f"fleet started {len(payload['accounts'])}/{len(payload['accounts'])}")


class _FakeHandle:
    def __init__(self, account_id, account_name=""):
        self.account_id = account_id
        self.account_name = account_name
        self.sent = []
        self.running_slots = set()
        self._alive = False

    def is_alive(self):
        return self._alive

    def start(self):
        self._alive = True

    def send(self, cmd, **kw):
        self.sent.append((cmd, kw))

    def shutdown(self, timeout=0):
        self._alive = False


@pytest.fixture
def desk(monkeypatch, tmp_path):
    monkeypatch.setattr(algo_desk, "AccountWorkerHandle", _FakeHandle)
    monkeypatch.setattr(algo_desk, "portable_terminal_path", lambda aid: rf"C:\Hammer\API\terminals\{aid}\terminal64.exe")
    accounts, bindings = ApiAccountStore(), BindingStore()
    a1, a2 = _account("Acc One", "111"), _account("Acc Two", "222")
    accounts.accounts += [a1, a2]
    bindings.upsert(_binding(a1, HAMMER, "5m"))
    b2 = _binding(a2, HWC35, "1h")
    b2.order_mode = "limit_entry"
    bindings.upsert(b2)
    return accounts, bindings, a1, a2


def _run(accounts, bindings, handles, engine, **kw):
    return algo_desk.start_api_trading_parallel(
        accounts=accounts, bindings=bindings, handles=handles, presets_dir=str(TEMPLATES),
        engine=engine, settle_sec=0, **kw,
    )


def test_start_reports_engine_offline(desk):
    accounts, bindings, *_ = desk
    handles = {}
    res = _run(accounts, bindings, handles, _FakeEngine(reachable=False))
    assert not res.ok
    assert "offline" in res.message
    assert handles == {}


def test_start_sends_connect_then_start_slot_per_account(desk):
    accounts, bindings, a1, a2 = desk
    handles = {}
    eng = _FakeEngine()
    logs = []
    res = _run(accounts, bindings, handles, eng, log=logs.append,
               shared_live_settings={"max_daily_loss_usd": 300.0})
    assert res.ok and not res.errors
    assert sorted(res.started) == sorted([a1.id, a2.id])
    assert "Start sent to 2/2" in res.message
    assert "trading started" not in " ".join(logs)
    # Fleet payload carries the session password for the engine launch
    assert {a["login"] for a in eng.fleet_payloads[0]["accounts"]} == {"111", "222"}
    for acc, mode in ((a1, "market"), (a2, "limit_entry")):
        h = handles[api_worker_key(acc.id)]
        assert [c for c, _ in h.sent] == ["connect", "start_slot"]
        connect = h.sent[0][1]
        assert connect["login"] == int(acc.login) and connect["password"] == "secret"
        assert connect["terminal_path"].endswith(rf"{acc.id}\terminal64.exe")
        slot = h.sent[1][1]
        assert slot["slot_id"].startswith("api-")
        assert slot["live_config"].order_mode == mode
        assert slot["live_config"].max_daily_loss_usd == 300.0


def test_start_has_no_offset_warning_when_presets_have_no_offset(desk):
    accounts, bindings, *_ = desk
    res = _run(accounts, bindings, {}, _FakeEngine())
    assert res.ok and res.warnings == []


def test_start_warns_when_market_order_would_ignore_entry_offset(desk, tmp_path):
    accounts, bindings, a1, a2 = desk
    for name in (HAMMER, HWC35):
        data = _preset(name)
        data["fields"]["entry_offset"] = "1.5"
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    logs = []
    res = algo_desk.start_api_trading_parallel(
        accounts=accounts, bindings=bindings, handles={}, presets_dir=str(tmp_path),
        engine=_FakeEngine(), settle_sec=0, log=logs.append,
    )
    assert res.ok and sorted(res.started) == sorted([a1.id, a2.id])
    # Acc One: Market → offset ignored. Acc Two: Limit at strategy entry → offset used.
    assert len(res.warnings) == 1 and res.warnings[0].startswith("Acc One:")
    assert "Entry Offset" in res.warnings[0]
    assert any("Acc One" in m and "Entry Offset" in m for m in logs)


def test_start_partial_failure_keeps_good_accounts(desk):
    accounts, bindings, a1, a2 = desk
    a2.login = "not-a-number"
    handles = {}
    res = _run(accounts, bindings, handles, _FakeEngine())
    assert res.ok
    assert res.started == [a1.id]
    assert any("Acc Two" in e and "numeric" in e for e in res.errors)
    assert api_worker_key(a2.id) not in handles


def test_start_twice_skips_running_accounts(desk):
    accounts, bindings, a1, a2 = desk
    handles = {}
    _run(accounts, bindings, handles, _FakeEngine())
    for acc in (a1, a2):
        b = bindings.by_account(acc.id)[0]
        handles[api_worker_key(acc.id)].running_slots.add(api_slot_id(b.id))
        handles[api_worker_key(acc.id)].sent.clear()
    res = _run(accounts, bindings, handles, _FakeEngine())
    assert res.ok
    assert res.started == []
    assert sorted(res.already_running) == sorted([a1.id, a2.id])
    assert all(not h.sent for h in handles.values())


def test_disabled_account_or_binding_not_started(desk):
    accounts, bindings, a1, a2 = desk
    a2.enabled = False
    bindings.by_account(a1.id)[0].enabled = False
    res = _run(accounts, bindings, {}, _FakeEngine())
    assert not res.ok
    assert "No enabled accounts" in res.message


def test_stop_api_trading_only_touches_api_handles(desk):
    accounts, bindings, a1, _a2 = desk
    handles = {}
    _run(accounts, bindings, handles, _FakeEngine())
    live_handle = _FakeHandle("live-acc")
    live_handle.start()
    handles["live-acc"] = live_handle
    algo_desk.stop_api_trading(handles)
    assert list(handles) == ["live-acc"]
    assert live_handle.is_alive()


# ---------------------------------------------------------------- real worker loop with a fake MT5

class _FakeMT5Broker:
    connect_ok = True

    def __init__(self):
        self.is_connected = False
        self.raw_mt5 = None
        self.creds = None

    def connect(self, creds):
        self.creds = creds
        self.is_connected = self.connect_ok
        return (self.connect_ok, "Connected" if self.connect_ok else "Authorization failed")

    def disconnect(self):
        self.is_connected = False

    def account_info_dict(self):
        return {"login": self.creds.login if self.creds else 0, "balance": 1000.0} if self.is_connected else {}


class _FakeEngineLoop:
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        self._stop = threading.Event()
        _FakeEngineLoop.instances.append(self)

    def run(self):
        self._stop.wait(5)

    def request_stop(self):
        self._stop.set()


def _drive_worker(monkeypatch, connect_ok, commands):
    import broker
    import live_account_worker

    _FakeMT5Broker.connect_ok = connect_ok
    _FakeEngineLoop.instances = []
    monkeypatch.setattr(broker, "MT5Broker", _FakeMT5Broker)
    monkeypatch.setattr(live_trading, "LiveTradingEngine", _FakeEngineLoop)
    cmd_q, evt_q = queue.Queue(), queue.Queue()
    th = threading.Thread(
        target=live_account_worker.account_worker_main,
        args=("api:x", "API:test", cmd_q, evt_q),
        daemon=True,
    )
    th.start()
    for cmd, kw in commands:
        cmd_q.put({"cmd": cmd, **kw})
    cmd_q.put({"cmd": "shutdown"})
    th.join(10)
    assert not th.is_alive()
    events = []
    while not evt_q.empty():
        events.append(evt_q.get())
    return [e for e in events if e["kind"] not in ("heartbeat", "log")]


def test_worker_connects_with_credentials_then_runs_slot(monkeypatch):
    acc = _account(login="777")
    start = _start(acc, _binding(acc, HWC35))
    events = _drive_worker(monkeypatch, True, [
        ("connect", dict(terminal_path=start.credentials.terminal_path, login=start.credentials.login,
                         password=start.credentials.password, server=start.credentials.server)),
        ("start_slot", algo_desk.start_slot_kwargs(start)),
    ])
    kinds = [e["kind"] for e in events]
    assert kinds[:2] == ["connected", "slot_started"]
    assert "slot_stopped" in kinds
    eng = _FakeEngineLoop.instances[0]
    assert eng.kw["pattern_type"] == "hammer_with_candles_35"
    assert eng.kw["live_config"].order_mode == "market"
    assert eng.kw["broker"].creds.login == 777


def test_worker_login_failure_blocks_strategy(monkeypatch):
    acc = _account()
    start = _start(acc, _binding(acc))
    events = _drive_worker(monkeypatch, False, [
        ("connect", dict(terminal_path="", login=acc.login, password="bad", server=acc.server)),
        ("start_slot", algo_desk.start_slot_kwargs(start)),
    ])
    msgs = [(e["kind"], e.get("message", "")) for e in events]
    assert ("error", "Authorization failed") in msgs
    assert any(k == "error" and "Not connected" in m for k, m in msgs)
    assert "slot_started" not in [k for k, _ in msgs]
    assert _FakeEngineLoop.instances == []


def test_start_payload_is_picklable_for_spawned_worker():
    import pickle

    acc = _account()
    for preset in (HAMMER, HWC, HWC35):
        start = _start(acc, _binding(acc, preset))
        kwargs = algo_desk.start_slot_kwargs(start)
        again = pickle.loads(pickle.dumps(kwargs))
        assert again["live_config"].order_mode == start.live_config.order_mode


# ---------------------------------------------------------------- persistence

def test_account_store_never_writes_password(tmp_path):
    store = ApiAccountStore()
    acc = _account(password="TopSecret!")
    acc.terminal_path = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    store.accounts.append(acc)
    path = tmp_path / "API" / "accounts.json"
    save_accounts(str(path), store)
    raw = path.read_text(encoding="utf-8")
    assert "TopSecret" not in raw
    loaded = load_accounts(str(path)).accounts[0]
    assert loaded.password == ""
    assert loaded.terminal_path == acc.terminal_path
    assert (loaded.login, loaded.server) == (acc.login, acc.server)


def test_account_upsert_keeps_session_password_when_edit_leaves_it_blank():
    store = ApiAccountStore()
    acc = _account(password="pw1")
    store.upsert(acc)
    edited = ApiAccount(id=acc.id, name="Renamed", login=acc.login, server=acc.server)
    store.upsert(edited)
    assert store.by_id(acc.id).password == "pw1"
    assert store.by_id(acc.id).name == "Renamed"


def test_bindings_roundtrip_and_dry_run_default(tmp_path):
    acc = _account()
    store = BindingStore()
    b = _binding(acc, HWC35, "1h", volume="0.02", dry_run=False)
    store.upsert(b)
    path = tmp_path / "bindings.json"
    save_bindings(str(path), store)
    loaded = load_bindings(str(path)).bindings[0]
    assert (loaded.preset_file, loaded.timeframe, loaded.volume, loaded.dry_run) == (HWC35, "1h", "0.02", False)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["bindings"][0]["dry_run"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_bindings(str(path)).bindings[0].dry_run is True


def test_binding_order_type_saved_and_old_files_load_as_market(tmp_path):
    acc = _account()
    store = BindingStore()
    b = _binding(acc, HWC35, "1h")
    b.order_mode, b.limit_offset_points = "limit_offset", 15.0
    store.upsert(b)
    path = tmp_path / "bindings.json"
    save_bindings(str(path), store)
    loaded = load_bindings(str(path)).bindings[0]
    assert (loaded.order_mode, loaded.limit_offset_points) == ("limit_offset", 15.0)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["bindings"][0]["order_mode"]
    del raw["bindings"][0]["limit_offset_points"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    old = load_bindings(str(path)).bindings[0]
    assert (old.order_mode, old.limit_offset_points) == ("market", 0.0)
    raw["bindings"][0]["order_mode"] = "garbage"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_bindings(str(path)).bindings[0].order_mode == "market"


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2, 3]", '{"accounts": 5}'])
def test_corrupt_store_files_load_empty(tmp_path, content):
    p = tmp_path / "x.json"
    p.write_text(content, encoding="utf-8")
    assert load_accounts(str(p)).accounts == []
    assert load_bindings(str(p)).bindings == []
