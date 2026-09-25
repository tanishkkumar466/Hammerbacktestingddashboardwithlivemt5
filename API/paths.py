"""Paths for C++ portable terminal isolation folders."""

from __future__ import annotations

import os
from typing import List


def api_terminals_root() -> str:
    """Same root as C++ DefaultDataRoot(): %LOCALAPPDATA%\\Hammer\\API\\terminals."""
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        return os.path.join(local, "Hammer", "API", "terminals")
    # Non-Windows / fallback (engine still Windows-only for CreateProcess)
    return os.path.join(os.path.expanduser("~"), "Hammer", "API", "terminals")


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
