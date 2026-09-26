"""
C++ MT5 API engine: discovery, install, auto-start/stop, portable terminal prep,
plus integration tests against a real compiled hammer_mt5_engine.

Integration tests use HAMMER_ENGINE_EXE (set by the Windows release workflow) or
compile the engine with a local C++ compiler; they skip when neither is available.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from API import algo_desk
from API.accounts import ApiAccount, ApiAccountStore
from API.bindings import BindingStore, StrategyBinding
from API.engine_process import (
    ENGINE_EXE,
    EngineProcess,
    engine_candidates,
    find_engine_exe,
    install_engine,
    is_hammer_engine,
)
from API.paths import (
    is_isolated_terminal_path,
    prepare_portable_terminal,
    servers_dat_sources,
)
from API.runtime import credentials_for_api_account
from API.service import EngineStatus, HeadlessEngineClient

ROOT = Path(__file__).resolve().parents[1]
ENGINE_SRC = ROOT / "API" / "engine"
HEALTH_BODY = '{"ok":true,"engine":"hammer_mt5_engine","mt5":""}'


# ---------------------------------------------------------------- discovery / install

def test_find_engine_prefers_bundled_then_app_dir(tmp_path, monkeypatch):
    app = tmp_path / "app"
    built = app / "API" / "engine" / "build" / "Release" / ENGINE_EXE
    built.parent.mkdir(parents=True)
    built.write_bytes(b"built")
    monkeypatch.setattr("API.engine_process._repo_dir", lambda: str(tmp_path / "norepo"))
    assert find_engine_exe(str(app)) == str(built)
    bundle = tmp_path / "meipass"
    (bundle / "API" / "engine").mkdir(parents=True)
    (bundle / "API" / "engine" / ENGINE_EXE).write_bytes(b"bundled")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert find_engine_exe(str(app)) == str(bundle / "API" / "engine" / ENGINE_EXE)
    assert len(engine_candidates(str(app))) == len(set(engine_candidates(str(app))))


def test_find_engine_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("API.engine_process._repo_dir", lambda: str(tmp_path / "norepo"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert find_engine_exe(str(tmp_path)) == ""


def test_install_engine_copies_once_and_updates_on_change(tmp_path):
    src = tmp_path / "src" / ENGINE_EXE
    src.parent.mkdir()
    src.write_bytes(b"v1")
    dest_dir = tmp_path / "installed"
    out = install_engine(str(src), str(dest_dir))
    assert out == str(dest_dir / ENGINE_EXE)
    assert Path(out).read_bytes() == b"v1"
    mtime = os.path.getmtime(out)
    assert install_engine(str(src), str(dest_dir)) == out
    assert os.path.getmtime(out) == mtime
    src.write_bytes(b"v2-new")
    install_engine(str(src), str(dest_dir))
    assert Path(out).read_bytes() == b"v2-new"


def test_install_engine_falls_back_to_source_when_dest_unwritable(tmp_path):
    src = tmp_path / ENGINE_EXE
    src.write_bytes(b"x")
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("")
    assert install_engine(str(src), str(blocker / "sub")) == str(src)


def test_is_hammer_engine_marker():
    assert is_hammer_engine(EngineStatus(True, "", HEALTH_BODY))
    assert not is_hammer_engine(EngineStatus(True, "", "<html>other app</html>"))
    assert not is_hammer_engine(EngineStatus(True, "", HEALTH_BODY, ok=False))
    assert not is_hammer_engine(EngineStatus(False, "offline"))


# ---------------------------------------------------------------- ensure_running with fakes

class _FakeClient:
    def __init__(self, statuses, port=17101):
        self.statuses = list(statuses)
        self.port = port
        self.host = "127.0.0.1"
        self.stop_all_calls = 0

    def health_http(self):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def request_stop_all(self):
        self.stop_all_calls += 1
        return EngineStatus(True, "all stopped")


class _FakeProc:
    def __init__(self, exit_code=None):
        self._code = exit_code
        self.terminated = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = 0

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self._code = -9


OFFLINE = EngineStatus(False, "offline", ok=False)
ONLINE = EngineStatus(True, "", HEALTH_BODY)


def _engine_app(tmp_path):
    exe = tmp_path / "app" / "API" / "engine" / ENGINE_EXE
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"engine")
    return str(tmp_path / "app")


def test_ensure_running_reuses_existing_engine_without_launch(tmp_path):
    launched = []
    ep = EngineProcess(_FakeClient([ONLINE]), popen=lambda *a, **k: launched.append(a), platform="win32")
    st = ep.ensure_running()
    assert st.reachable and st.ok
    assert launched == []
    ep.stop()  # not ours → nothing to stop


def test_ensure_running_reports_foreign_program_on_port():
    foreign = EngineStatus(True, "HTTP 404", "<html/>", ok=False)
    ep = EngineProcess(_FakeClient([foreign]), popen=lambda *a, **k: pytest.fail("launched"), platform="win32")
    st = ep.ensure_running()
    assert not st.ok and "another program" in st.message


def test_ensure_running_windows_only():
    ep = EngineProcess(_FakeClient([OFFLINE]), popen=lambda *a, **k: pytest.fail("launched"), platform="darwin")
    st = ep.ensure_running()
    assert not st.ok and "Windows" in st.message


def test_ensure_running_missing_exe(tmp_path, monkeypatch):
    monkeypatch.setattr("API.engine_process._repo_dir", lambda: str(tmp_path / "norepo"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    ep = EngineProcess(_FakeClient([OFFLINE]), app_dir=str(tmp_path), platform="win32",
                       popen=lambda *a, **k: pytest.fail("launched"))
    st = ep.ensure_running()
    assert not st.ok and "missing" in st.message


def test_ensure_running_launches_hidden_and_waits_for_health(tmp_path):
    calls = []
    proc = _FakeProc()

    def popen(args, **kw):
        calls.append((args, kw))
        return proc

    client = _FakeClient([OFFLINE, OFFLINE, OFFLINE, ONLINE], port=17222)
    logs = []
    ep = EngineProcess(client, app_dir=_engine_app(tmp_path), popen=popen, platform="win32",
                       install_dir=str(tmp_path / "installed"), log=logs.append)
    st = ep.ensure_running(timeout_sec=5, poll_sec=0.01)
    assert st.reachable and st.ok and "started" in st.message
    args, kw = calls[0]
    assert args[0] == str(tmp_path / "installed" / ENGINE_EXE)
    assert args[1:] == ["--port", "17222"]
    assert kw["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert kw["stdin"] == subprocess.DEVNULL
    assert ep.started_by_us
    ep.stop()
    assert client.stop_all_calls == 1 and proc.terminated
    assert not ep.started_by_us


def test_ensure_running_reports_early_exit_with_log_tail(tmp_path):
    install = tmp_path / "installed"
    install.mkdir()
    (install / "engine.log").write_bytes(b"[hammer_mt5_engine] FATAL bind() failed - is port 17101 in use?\n")
    ep = EngineProcess(_FakeClient([OFFLINE]), app_dir=_engine_app(tmp_path), platform="win32",
                       popen=lambda *a, **k: _FakeProc(exit_code=1), install_dir=str(install))
    st = ep.ensure_running(timeout_sec=2, poll_sec=0.01)
    assert not st.ok
    assert "exit 1" in st.message and "in use" in st.message
    assert not ep.started_by_us


def test_ensure_running_times_out_and_cleans_up(tmp_path):
    proc = _FakeProc()
    client = _FakeClient([OFFLINE])
    ep = EngineProcess(client, app_dir=_engine_app(tmp_path), platform="win32",
                       popen=lambda *a, **k: proc, install_dir=str(tmp_path / "i"))
    st = ep.ensure_running(timeout_sec=0.5, poll_sec=0.05)
    assert not st.ok and "did not answer" in st.message
    assert proc.terminated
    assert client.stop_all_calls == 0  # never close terminals on a failed start


def test_ensure_running_popen_error(tmp_path):
    def boom(*a, **k):
        raise OSError("Access is denied")

    ep = EngineProcess(_FakeClient([OFFLINE]), app_dir=_engine_app(tmp_path), platform="win32",
                       popen=boom, install_dir=str(tmp_path / "i"))
    st = ep.ensure_running()
    assert not st.ok and "Access is denied" in st.message


# ---------------------------------------------------------------- portable terminal prep

def _fake_install(tmp_path):
    inst = tmp_path / "Program Files" / "Broker MetaTrader 5"
    inst.mkdir(parents=True)
    (inst / "terminal64.exe").write_bytes(b"MZ-terminal")
    (inst / "helper.dll").write_bytes(b"dll")
    (inst / "metaeditor64.exe").write_bytes(b"editor")
    (inst / "readme.txt").write_text("x")
    return inst


def test_prepare_portable_copies_same_files_as_engine_plus_servers(tmp_path):
    inst = _fake_install(tmp_path)
    appdata = tmp_path / "Roaming"
    data = appdata / "MetaQuotes" / "Terminal" / "ABC123"
    (data / "Config").mkdir(parents=True)
    (data / "origin.txt").write_bytes(str(inst).encode("utf-16"))
    (data / "Config" / "servers.dat").write_bytes(b"broker-servers")
    other = appdata / "MetaQuotes" / "Terminal" / "OTHER"
    (other / "Config").mkdir(parents=True)
    (other / "origin.txt").write_bytes(str(tmp_path / "elsewhere").encode("utf-16"))
    (other / "Config" / "servers.dat").write_bytes(b"wrong")

    root = tmp_path / "terminals"
    exe, note = prepare_portable_terminal("acc1", str(inst / "terminal64.exe"), root=str(root), appdata=str(appdata))
    dest = root / "acc1"
    assert exe == str(dest / "terminal64.exe")
    assert sorted(p.name for p in dest.iterdir() if p.is_file()) == ["helper.dll", "terminal64.exe"]
    assert (dest / "Config" / "servers.dat").read_bytes() == b"broker-servers"
    assert "servers.dat ready" in note

    # Second run: nothing to copy
    _exe, note2 = prepare_portable_terminal("acc1", str(inst / "terminal64.exe"), root=str(root), appdata=str(appdata))
    assert "0 file(s) updated" in note2


def test_prepare_portable_never_downgrades_self_updated_terminal(tmp_path):
    inst = _fake_install(tmp_path)
    root = tmp_path / "terminals"
    exe, _ = prepare_portable_terminal("a", str(inst / "terminal64.exe"), root=str(root), appdata="")
    Path(exe).write_bytes(b"MZ-terminal-newer-build")
    future = time.time() + 100
    os.utime(exe, (future, future))
    prepare_portable_terminal("a", str(inst / "terminal64.exe"), root=str(root), appdata="")
    assert Path(exe).read_bytes() == b"MZ-terminal-newer-build"
    # A newer install build does replace the copy
    (inst / "terminal64.exe").write_bytes(b"MZ-install-update")
    later = future + 100
    os.utime(inst / "terminal64.exe", (later, later))
    prepare_portable_terminal("a", str(inst / "terminal64.exe"), root=str(root), appdata="")
    assert Path(exe).read_bytes() == b"MZ-install-update"


def test_prepare_portable_missing_install(tmp_path):
    exe, note = prepare_portable_terminal("a", str(tmp_path / "nope" / "terminal64.exe"), root=str(tmp_path))
    assert exe == "" and "not found" in note


def test_servers_dat_origin_matching_ignores_case_and_trailing_slash(tmp_path):
    inst = _fake_install(tmp_path)
    data = tmp_path / "Roaming" / "MetaQuotes" / "Terminal" / "H"
    (data / "Config").mkdir(parents=True)
    (data / "origin.txt").write_text(str(inst) + os.sep, encoding="utf-8")
    (data / "Config" / "servers.dat").write_bytes(b"s")
    found = servers_dat_sources(str(inst / "terminal64.exe"), str(tmp_path / "Roaming"))
    assert found == [str(data / "Config" / "servers.dat")]


def test_portable_flag_only_for_isolated_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    iso = str(tmp_path / "Local" / "Hammer" / "API" / "terminals" / "abc" / "terminal64.exe")
    assert is_isolated_terminal_path(iso)
    assert not is_isolated_terminal_path(str(tmp_path / "Program Files" / "MT5" / "terminal64.exe"))
    assert not is_isolated_terminal_path("")
    acc = ApiAccount.new("x")
    acc.login, acc.server, acc.password = "1", "S", "p"
    assert credentials_for_api_account(acc, terminal_path=iso).portable is True
    install = str(tmp_path / "Program Files" / "MT5" / "terminal64.exe")
    assert credentials_for_api_account(acc, terminal_path=install).portable is False


def test_live_desk_credentials_stay_non_portable():
    from broker import BrokerCredentials

    assert BrokerCredentials(terminal_path="C:/MT5/terminal64.exe").portable is False


# ---------------------------------------------------------------- real compiled engine

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def engine_exe(tmp_path_factory):
    env_exe = os.environ.get("HAMMER_ENGINE_EXE", "")
    if env_exe:
        assert os.path.isfile(env_exe), f"HAMMER_ENGINE_EXE not found: {env_exe}"
        return env_exe
    if os.name == "nt":
        pytest.skip("set HAMMER_ENGINE_EXE to test the Windows engine build")
    cxx = shutil.which("clang++") or shutil.which("g++")
    if not cxx:
        pytest.skip("no C++ compiler to build the engine")
    out = tmp_path_factory.mktemp("engine") / ENGINE_EXE
    srcs = [str(ENGINE_SRC / n) for n in ("main.cpp", "mt5_path.cpp", "launcher.cpp", "http_mini.cpp", "orders.cpp")]
    res = subprocess.run(
        [cxx, "-std=c++17", "-O1", f"-I{ENGINE_SRC}", *srcs, "-o", str(out), "-pthread"],
        capture_output=True, text=True, timeout=240,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    return str(out)


def _popen_any_os(args, **kw):
    if os.name != "nt":
        kw.pop("creationflags", None)
    return subprocess.Popen(args, **kw)


@pytest.fixture
def running_engine(engine_exe, tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "API" / "engine").mkdir(parents=True)
    shutil.copy2(engine_exe, app / "API" / "engine" / ENGINE_EXE)
    monkeypatch.setattr("API.engine_process._repo_dir", lambda: str(tmp_path / "norepo"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    client = HeadlessEngineClient(port=_free_port(), timeout_sec=3.0)
    ep = EngineProcess(client, app_dir=str(app), popen=_popen_any_os, platform="win32",
                       install_dir=str(tmp_path / "installed"))
    st = ep.ensure_running(timeout_sec=15)
    assert st.reachable and st.ok, f"{st.message} {st.detail}"
    yield ep, client
    ep.stop()


def test_real_engine_health_and_routes(running_engine):
    ep, client = running_engine
    assert ep.started_by_us
    health = client.health_http()
    assert is_hammer_engine(health)
    empty = client.request_fleet_connect({"stagger_ms": 0, "accounts": []})
    assert empty.reachable and empty.ok
    missing = client._http("GET", "/nope")
    assert missing.reachable and not missing.ok
    status = client.status()
    assert status.reachable and '"accounts"' in status.detail


def test_real_engine_failed_launch_is_reachable_not_offline(running_engine, tmp_path):
    _ep, client = running_engine
    fake = _fake_install(tmp_path)
    st = client.request_connect_account({
        "account_id": "probe1", "login": "1", "password": "p", "server": "S",
        "terminal_path": str(fake / "terminal64.exe"),
    })
    assert st.reachable, st.message
    assert not st.ok
    assert st.message.startswith("Engine error:")
    fleet = client.request_fleet_connect({"stagger_ms": 0, "accounts": [
        {"account_id": "probe2", "terminal_path": str(fake / "terminal64.exe")},
    ]})
    assert fleet.reachable and not fleet.ok
    rows = fleet.results()
    assert len(rows) == 1 and rows[0]["ok"] is False and rows[0]["message"]
    client.request_stop_all()


def test_real_engine_second_instance_on_same_port_refused(running_engine):
    ep, client = running_engine
    second = subprocess.Popen(
        [ep.exe_path, "--port", str(client.port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        code = second.wait(timeout=10)
    except subprocess.TimeoutExpired:
        second.kill()
        pytest.fail("second engine bound the same port")
    assert code != 0
    assert "bind" in second.stdout.read().decode("utf-8", "replace")
    assert is_hammer_engine(client.health_http())


def test_real_engine_stop_frees_port(engine_exe, tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "API" / "engine").mkdir(parents=True)
    shutil.copy2(engine_exe, app / "API" / "engine" / ENGINE_EXE)
    monkeypatch.setattr("API.engine_process._repo_dir", lambda: str(tmp_path / "norepo"))
    client = HeadlessEngineClient(port=_free_port(), timeout_sec=2.0)
    ep = EngineProcess(client, app_dir=str(app), popen=_popen_any_os, platform="win32",
                       install_dir=str(tmp_path / "installed"))
    assert ep.ensure_running(timeout_sec=15).ok
    ep.stop()
    time.sleep(0.3)
    assert not client.health_http().reachable


def test_full_start_flow_against_real_engine(running_engine, tmp_path, monkeypatch):
    """Validation → portable prep → real engine fleet call → workers get portable connect."""
    ep, client = running_engine
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    fake = _fake_install(tmp_path)

    sent = {}

    class Handle:
        def __init__(self, account_id, account_name=""):
            self.account_id, self.account_name = account_id, account_name
            self.running_slots, self._alive = set(), False
            sent[account_id] = []

        def is_alive(self):
            return self._alive

        def start(self):
            self._alive = True

        def send(self, cmd, **kw):
            sent[self.account_id].append((cmd, kw))

    monkeypatch.setattr(algo_desk, "AccountWorkerHandle", Handle)
    accounts, bindings = ApiAccountStore(), BindingStore()
    acc = ApiAccount.new("Real Engine")
    acc.login, acc.server, acc.password = "4242", "Broker-Demo", "pw"
    accounts.accounts.append(acc)
    b = StrategyBinding.new(acc.id, "preset_example_hammer_with_candle_35.json", "1h")
    bindings.upsert(b)
    logs = []
    res = algo_desk.start_api_trading_parallel(
        accounts=accounts, bindings=bindings, handles={}, presets_dir=str(ROOT / "template"),
        install_terminal_path=str(fake / "terminal64.exe"), engine=client, settle_sec=0,
        log=logs.append, ensure_engine=ep.ensure_running,
    )
    assert res.ok, res.message
    assert res.started == [acc.id]
    joined = "\n".join(logs)
    assert "portable copy ready" in joined
    assert "engine could not open the terminal" in joined  # fake exe cannot launch
    cmds = sent[f"api:{acc.id}"]
    assert [c for c, _ in cmds] == ["connect", "start_slot"]
    connect = cmds[0][1]
    assert connect["portable"] is True
    assert connect["terminal_path"] == str(tmp_path / "Local" / "Hammer" / "API" / "terminals" / acc.id / "terminal64.exe")
    assert os.path.isfile(connect["terminal_path"])
    assert cmds[1][1]["live_config"].order_mode == "market"
