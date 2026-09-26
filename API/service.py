"""
Python client for the C++ headless MT5 engine (127.0.0.1:17101).

UI stays in Python. Launching / isolating terminals is C++ only —
do not launch MT5 from Python for the API Accounts path.

Any broker: login + server + that broker's terminal64.exe (Locate / Auto-find).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 17101


@dataclass
class EngineStatus:
    reachable: bool
    message: str
    detail: str = ""
    path: str = ""
    # False when the engine answered but the request failed (HTTP 4xx/5xx or "ok": false)
    ok: bool = True

    def results(self) -> List[Dict[str, Any]]:
        """Per-account rows from /fleet/connect (same order as the request)."""
        try:
            rows = json.loads(self.detail or "{}").get("results")
        except (json.JSONDecodeError, AttributeError):
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def build_connect_payload(
    *,
    account_id: str,
    name: str = "",
    login: str = "",
    password: str = "",
    server: str = "",
    terminal_path: str = "",
) -> Dict[str, Any]:
    """JSON body for POST /account/connect (C++ engine). No strategy / preset fields."""
    path = (terminal_path or "").strip()
    return {
        "account_id": (account_id or "").strip(),
        "name": (name or "").strip(),
        "kind": "headless_mt5",
        "login": (login or "").strip(),
        "password": password or "",
        "server": (server or "").strip(),
        "terminal_path": path,
        "mt5_path": path,
    }


def _join_mt5_exe(folder: str) -> str:
    folder = (folder or "").strip().rstrip("\\/")
    if not folder:
        return ""
    for name in ("terminal64.exe", "terminal.exe"):
        cand = os.path.join(folder, name)
        if os.path.isfile(cand):
            return cand
    return ""


def _looks_like_broker_mt5_folder(name: str) -> bool:
    n = (name or "").lower()
    if "metatrader" in n and "5" in n:
        return True
    if "mt5" in n:
        return True
    return False


def _scan_program_files_any_broker() -> List[str]:
    """Find terminal64.exe under any broker-branded MetaTrader 5 folder."""
    found: List[str] = []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                if not _looks_like_broker_mt5_folder(entry):
                    continue
                exe = _join_mt5_exe(os.path.join(root, entry))
                if exe and exe not in found:
                    found.append(exe)
        except OSError:
            continue
    return found


def discover_mt5_path_windows() -> str:
    """
    Registry + scan Program Files for *any* broker MT5 install
    (IC Markets, Exness, Pepperstone, FTMO, generic MetaTrader 5, …).
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\MetaQuotes\Terminal"),
        (winreg.HKEY_CURRENT_USER, r"Software\MetaQuotes\MetaTrader 5"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\MetaQuotes\Terminal"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\MetaQuotes\MetaTrader 5"),
    ]
    value_names = ("Path", "InstallPath", "EXE", "Folder", "DataPath")
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as h:
                for vn in value_names:
                    try:
                        val, _ = winreg.QueryValueEx(h, vn)
                    except OSError:
                        continue
                    path = str(val or "").strip()
                    if not path:
                        continue
                    if path.lower().endswith("terminal64.exe") or path.lower().endswith("terminal.exe"):
                        if os.path.isfile(path):
                            return path
                    joined = _join_mt5_exe(path)
                    if joined:
                        return joined
                # Nested hashes under Terminal\
                try:
                    i = 0
                    while i < 64:
                        try:
                            child_name = winreg.EnumKey(h, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(h, child_name) as ch:
                                for vn in value_names:
                                    try:
                                        val, _ = winreg.QueryValueEx(ch, vn)
                                    except OSError:
                                        continue
                                    path = str(val or "").strip()
                                    if path.lower().endswith(".exe") and os.path.isfile(path):
                                        return path
                                    joined = _join_mt5_exe(path)
                                    if joined:
                                        return joined
                        except OSError:
                            continue
                except OSError:
                    pass
        except OSError:
            continue

    scanned = _scan_program_files_any_broker()
    if scanned:
        return scanned[0]
    return ""


def list_mt5_installs_windows() -> List[str]:
    """All discovered broker MT5 terminals (for UI / debugging)."""
    paths: List[str] = []
    primary = discover_mt5_path_windows()
    if primary:
        paths.append(primary)
    for p in _scan_program_files_any_broker():
        if p not in paths:
            paths.append(p)
    return paths


class HeadlessEngineClient:
    """HTTP client for hammer_mt5_engine.exe (C++)."""

    def __init__(
        self,
        host: str = DEFAULT_ENGINE_HOST,
        port: int = DEFAULT_ENGINE_PORT,
        timeout_sec: float = 1.5,
    ):
        self.host = (host or DEFAULT_ENGINE_HOST).strip() or DEFAULT_ENGINE_HOST
        self.port = int(port or DEFAULT_ENGINE_PORT)
        self.timeout_sec = float(timeout_sec)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _http(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
              timeout: Optional[float] = None) -> EngineStatus:
        url = f"{self.base_url}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                path_found = ""
                try:
                    parsed = json.loads(body)
                    path_found = str(parsed.get("path") or parsed.get("mt5") or "")
                    msg = str(parsed.get("message") or body[:200])
                    ok = bool(parsed.get("ok", True))
                except json.JSONDecodeError:
                    msg = body[:200]
                    ok = True
                return EngineStatus(
                    reachable=True,
                    message=msg if ok else f"Engine error: {msg}",
                    detail=body[:4000],
                    path=path_found,
                    ok=ok,
                )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            msg = f"HTTP {exc.code}"
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict) and parsed.get("message"):
                    msg = f"Engine error: {parsed['message']}"
            except json.JSONDecodeError:
                pass
            return EngineStatus(True, msg, detail[:4000], ok=False)
        except Exception as exc:
            return EngineStatus(
                False,
                "API service offline — start the local API service",
                str(exc),
                ok=False,
            )

    def ping(self) -> EngineStatus:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_sec):
                return EngineStatus(True, f"Engine listening on {self.host}:{self.port}")
        except OSError as exc:
            return EngineStatus(
                False,
                "API service offline — start the local API service",
                str(exc),
                ok=False,
            )

    def health_http(self) -> EngineStatus:
        """GET /health only — no TCP-ping fallback (tells our engine from other programs)."""
        return self._http("GET", "/health")

    def health(self) -> EngineStatus:
        st = self._http("GET", "/health")
        if st.reachable:
            st.message = f"API service online · {self.host}:{self.port}"
            return st
        return self.ping()

    def discover_path(self) -> EngineStatus:
        return self._http("GET", "/discover")

    def set_mt5_path(self, path: str) -> EngineStatus:
        return self._http("POST", "/engine/set_path", {"path": path})

    def request_connect_account(self, payload: Dict[str, Any]) -> EngineStatus:
        return self._http("POST", "/account/connect", payload, timeout=max(8.0, self.timeout_sec))

    def request_disconnect_account(self, account_id: str) -> EngineStatus:
        return self._http("POST", "/account/disconnect", {"account_id": account_id})

    def request_fleet_connect(self, payload: Dict[str, Any]) -> EngineStatus:
        """Staggered multi-account connect (POST /fleet/connect)."""
        return self._http(
            "POST",
            "/fleet/connect",
            payload,
            timeout=max(60.0, self.timeout_sec),
        )

    def request_orders_batch(self, payload: Dict[str, Any]) -> EngineStatus:
        """Parallel order fan-out (POST /orders/batch)."""
        return self._http(
            "POST",
            "/orders/batch",
            payload,
            timeout=max(30.0, self.timeout_sec),
        )

    def status(self) -> EngineStatus:
        return self._http("GET", "/status")

    def request_stop_all(self) -> EngineStatus:
        """Close every terminal this engine launched (POST /engine/stop_all)."""
        return self._http("POST", "/engine/stop_all", {}, timeout=max(5.0, self.timeout_sec))
