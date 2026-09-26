"""Paths for C++ portable terminal isolation folders."""

from __future__ import annotations

import glob
import os
import shutil
from typing import List, Optional, Tuple

TERMINAL_EXE_NAMES = ("terminal64.exe", "terminal.exe")


def api_terminals_root() -> str:
    """Same root as C++ DefaultDataRoot(): %LOCALAPPDATA%\\Hammer\\API\\terminals."""
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        return os.path.join(local, "Hammer", "API", "terminals")
    # Non-Windows / fallback (engine still Windows-only for CreateProcess)
    return os.path.join(os.path.expanduser("~"), "Hammer", "API", "terminals")


def is_isolated_terminal_path(path: str, root: Optional[str] = None) -> bool:
    """True when path is a per-account copy under the API terminals folder."""
    if not path:
        return False
    base = _norm_dir(root or api_terminals_root())
    return _norm_dir(os.path.dirname(path)).startswith(base + os.sep)


def portable_terminal_candidates(account_id: str) -> List[str]:
    aid = (account_id or "").strip()
    if not aid:
        return []
    root = os.path.join(api_terminals_root(), aid)
    return [
        os.path.join(root, "terminal64.exe"),
        os.path.join(root, "terminal.exe"),
    ]


def portable_terminal_path(account_id: str) -> str:
    """Path C++ prepares for this account; strategy worker attaches here."""
    for cand in portable_terminal_candidates(account_id):
        if os.path.isfile(cand):
            return cand
    # Prefer the canonical name even if not copied yet (engine creates on connect)
    cands = portable_terminal_candidates(account_id)
    return cands[0] if cands else ""


def _read_origin(path: str) -> str:
    """MT5 data folders hold origin.txt = install folder (usually UTF-16 with BOM)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return ""
    for enc in (("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc).strip().strip("\x00").strip()
        except UnicodeDecodeError:
            continue
    return ""


def _norm_dir(p: str) -> str:
    return os.path.normcase(os.path.normpath((p or "").strip().rstrip("\\/")))


def servers_dat_sources(install_exe: str, appdata: Optional[str] = None) -> List[str]:
    """servers.dat files that belong to this MT5 install (install Config + its data folder)."""
    install_dir = os.path.dirname(install_exe)
    out = []
    own = os.path.join(install_dir, "Config", "servers.dat")
    if os.path.isfile(own):
        out.append(own)
    appdata = appdata if appdata is not None else (os.environ.get("APPDATA") or "")
    if appdata:
        want = _norm_dir(install_dir)
        for origin in glob.glob(os.path.join(appdata, "MetaQuotes", "Terminal", "*", "origin.txt")):
            if _norm_dir(_read_origin(origin)) == want:
                cand = os.path.join(os.path.dirname(origin), "Config", "servers.dat")
                if os.path.isfile(cand):
                    out.append(cand)
    return out


def _copy_if_changed(src: str, dest: str) -> bool:
    try:
        if os.path.isfile(dest) and int(os.stat(src).st_mtime) <= int(os.stat(dest).st_mtime):
            return False
        shutil.copy2(src, dest)
        return True
    except OSError:
        # Terminal already running from this folder (file locked) — keep the current copy.
        return False


def prepare_portable_terminal(
    account_id: str,
    install_exe: str,
    *,
    root: Optional[str] = None,
    appdata: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Make %LOCALAPPDATA%\\Hammer\\API\\terminals\\<id> a runnable /portable MT5 copy.

    Same files the C++ engine copies (terminal exe + DLLs) plus Config\\servers.dat,
    so the isolated terminal knows the broker's server names. Existing files are
    only replaced when the install has a newer copy. Returns (portable_exe, message).
    """
    aid = (account_id or "").strip()
    if not aid:
        return "", "account id missing"
    if not install_exe or not os.path.isfile(install_exe):
        return "", f"MT5 terminal not found at '{install_exe}'"
    install_dir = os.path.dirname(install_exe)
    dest_dir = os.path.join(root or api_terminals_root(), aid)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        return "", f"cannot create {dest_dir}: {exc}"
    copied = 0
    for name in os.listdir(install_dir):
        src = os.path.join(install_dir, name)
        if not os.path.isfile(src):
            continue
        if name.lower() in TERMINAL_EXE_NAMES or name.lower().endswith(".dll"):
            copied += _copy_if_changed(src, os.path.join(dest_dir, name))
    dest_exe = os.path.join(dest_dir, os.path.basename(install_exe))
    if not os.path.isfile(dest_exe):
        return "", f"could not copy {os.path.basename(install_exe)} into {dest_dir}"
    servers = servers_dat_sources(install_exe, appdata)
    servers_note = "no servers.dat found (terminal will look the server up)"
    if servers:
        newest = max(servers, key=lambda p: os.path.getmtime(p))
        cfg_dir = os.path.join(dest_dir, "Config")
        os.makedirs(cfg_dir, exist_ok=True)
        dest_servers = os.path.join(cfg_dir, "servers.dat")
        if not os.path.isfile(dest_servers) or os.path.getmtime(newest) > os.path.getmtime(dest_servers):
            try:
                shutil.copy2(newest, dest_servers)
            except OSError:
                pass
        servers_note = "servers.dat ready"
    return dest_exe, f"portable copy ready ({copied} file(s) updated, {servers_note})"
