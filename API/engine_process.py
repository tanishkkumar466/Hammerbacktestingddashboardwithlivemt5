"""
Find, install and auto-start the bundled C++ hammer_mt5_engine.exe (Windows).

The release build bundles the engine inside the app. It is copied to
%LOCALAPPDATA%\\Hammer\\API\\engine (so a running engine never locks the
PyInstaller temp folder) and started hidden on demand — only when the API
Accounts desk needs it. An engine the user started by hand is reused and
never stopped by the app.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional

from .paths import api_terminals_root
from .service import EngineStatus, HeadlessEngineClient

ENGINE_EXE = "hammer_mt5_engine.exe"
ENGINE_MARKER = "hammer_mt5_engine"

LogFn = Callable[[str], None]


def _repo_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def engine_candidates(app_dir: str = "") -> List[str]:
    """Where a bundled / dev-built / hand-copied engine may live (first hit wins)."""
    out: List[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(os.path.join(meipass, "API", "engine", ENGINE_EXE))
    for base in filter(None, [app_dir, _repo_dir()]):
        out += [
            os.path.join(base, "API", "engine", ENGINE_EXE),
            os.path.join(base, "API", "engine", "build", "Release", ENGINE_EXE),
            os.path.join(base, "API", "engine", "build", ENGINE_EXE),
            os.path.join(base, ENGINE_EXE),
        ]
    seen, uniq = set(), []
    for p in out:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_engine_exe(app_dir: str = "") -> str:
    for cand in engine_candidates(app_dir):
        if os.path.isfile(cand):
            return cand
    return ""


def installed_engine_dir() -> str:
    return os.path.join(os.path.dirname(api_terminals_root()), "engine")


def _same_file_bytes(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def install_engine(src: str, dest_dir: Optional[str] = None) -> str:
    """Copy the engine to a stable folder; returns the path to run (src if copy impossible)."""
    dest_dir = dest_dir or installed_engine_dir()
    dest = os.path.join(dest_dir, ENGINE_EXE)
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
        return dest
    if os.path.isfile(dest) and _same_file_bytes(src, dest):
        return dest
    try:
        os.makedirs(dest_dir, exist_ok=True)
        tmp = dest + ".new"
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(1 << 20)
                if not chunk:
                    break
                fo.write(chunk)
        shutil.copymode(src, tmp)
        os.replace(tmp, dest)
        return dest
    except OSError:
        # Old copy still running (locked) or folder not writable — run the bundled file.
        return src


def is_hammer_engine(status: EngineStatus) -> bool:
    return bool(status.reachable and status.ok and ENGINE_MARKER in (status.detail or ""))


def _tail(path: str, n: int = 600) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


class EngineProcess:
    """Owns the engine process the app started (if any)."""

    def __init__(
        self,
        client: Optional[HeadlessEngineClient] = None,
        *,
        app_dir: str = "",
        log: Optional[LogFn] = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        platform: str = sys.platform,
        install_dir: Optional[str] = None,
    ):
        self.client = client or HeadlessEngineClient()
        self.app_dir = app_dir
        self._log = log
        self._popen = popen
        self._platform = platform
        self._install_dir = install_dir
        self.proc: Optional[subprocess.Popen] = None
        self.exe_path = ""
        self.log_path = ""

    def log(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    @property
    def started_by_us(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _health(self) -> EngineStatus:
        return self.client.health_http()

    def ensure_running(self, timeout_sec: float = 10.0, poll_sec: float = 0.25) -> EngineStatus:
        st = self._health()
        if is_hammer_engine(st):
            return st
        if st.reachable:
            return EngineStatus(
                False,
                f"Port {self.client.port} is used by another program — close it, then try again.",
                st.detail,
                ok=False,
            )
        if not self._platform.startswith("win"):
            return EngineStatus(False, "The MT5 API engine runs on Windows only.", ok=False)
        src = find_engine_exe(self.app_dir)
        if not src:
            return EngineStatus(
                False,
                f"{ENGINE_EXE} is missing from this install — update or reinstall Hammer.",
                "searched: " + "; ".join(engine_candidates(self.app_dir)),
                ok=False,
            )
        exe = install_engine(src, self._install_dir)
        self.exe_path = exe
        self.log_path = os.path.join(os.path.dirname(exe), "engine.log")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            log_f = open(self.log_path, "ab")
        except OSError:
            log_f = subprocess.DEVNULL
        try:
            self.proc = self._popen(
                [exe, "--port", str(self.client.port)],
                cwd=os.path.dirname(exe),
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            self.proc = None
            return EngineStatus(False, f"Could not start {ENGINE_EXE}: {exc}", exe, ok=False)
        finally:
            if log_f is not subprocess.DEVNULL:
                log_f.close()
        self.log(f"[API] Starting MT5 API engine ({exe})…")
        deadline = time.monotonic() + max(0.5, timeout_sec)
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                tail = _tail(self.log_path)
                self.proc = None
                return EngineStatus(
                    False,
                    f"{ENGINE_EXE} stopped right away (exit {code}). {tail[-240:]}".strip(),
                    tail,
                    ok=False,
                )
            st = self._health()
            if is_hammer_engine(st):
                st.message = f"API service started · {self.client.host}:{self.client.port}"
                self.log(f"[API] {st.message}")
                return st
            time.sleep(poll_sec)
        self.stop(close_terminals=False)
        return EngineStatus(
            False,
            f"{ENGINE_EXE} did not answer within {timeout_sec:g}s.",
            _tail(self.log_path),
            ok=False,
        )

    def stop(self, *, close_terminals: bool = True, wait_sec: float = 3.0) -> None:
        """Stop the engine only if this app started it."""
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            if close_terminals:
                try:
                    self.client.request_stop_all()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=wait_sec)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.proc = None
