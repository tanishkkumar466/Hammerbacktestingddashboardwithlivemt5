"""
Preset metadata helpers for the API algo desk.

Live / backtest loaders ignore unknown keys. We always stamp saved_at and
expose enabled timeframes so each account binding can pick a TF safely.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def enabled_timeframes_from_preset(data: Dict[str, Any]) -> List[str]:
    """Return TF labels marked enabled in a preset snapshot (order preserved)."""
    out: List[str] = []
    # Prefer explicit list if present (written on save)
    explicit = data.get("enabled_timeframes")
    if isinstance(explicit, list) and explicit:
        for item in explicit:
            s = str(item or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    tfs = data.get("timeframes") or {}
    if isinstance(tfs, dict):
        for name, cfg in tfs.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enabled", True):
                label = str(name).strip()
                if label and label not in out:
                    out.append(label)

    customs = data.get("custom_timeframes") or {}
    if isinstance(customs, dict):
        for name, cfg in customs.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enabled", True):
                label = str(name).strip()
                if label and label not in out:
                    out.append(label)
    return out


def stamp_preset_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure saved_at + enabled_timeframes on a preset dict (mutates and returns).
    Safe to call from dashboard save and from API tooling.
    """
    payload = dict(payload or {})
    payload["saved_at"] = utc_now_iso()
    enabled = enabled_timeframes_from_preset(payload)
    payload["enabled_timeframes"] = enabled
    return payload


def read_preset_file(path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (data, error_message). data is None on failure."""
    if not path or not os.path.isfile(path):
        return None, f"preset not found: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "preset root must be an object"
    return data, ""


def preset_saved_at(data: Dict[str, Any]) -> str:
    return str((data or {}).get("saved_at") or "").strip()
