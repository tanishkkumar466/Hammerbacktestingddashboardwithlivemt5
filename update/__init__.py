"""
Hammer self-update package (Check for Updates, stub launcher, one-hop migration).

Keep this module lightweight — the Windows stub imports update.paths and must
NOT pull PySide6 / updater UI through package __init__.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "CURRENT_VERSION",
    "check_for_update",
    "download_and_install",
    "install_root",
    "migrate_legacy_onefile_to_stub_if_needed",
    "open_update_window",
    "save_github_token",
    "token_lookup_hint",
]


def __getattr__(name: str) -> Any:
    if name == "install_root":
        from update.paths import install_root

        return install_root
    if name == "migrate_legacy_onefile_to_stub_if_needed":
        from update.migrate import migrate_legacy_onefile_to_stub_if_needed

        return migrate_legacy_onefile_to_stub_if_needed
    if name in {
        "CURRENT_VERSION",
        "check_for_update",
        "download_and_install",
        "save_github_token",
        "token_lookup_hint",
    }:
        import update.updater as updater

        return getattr(updater, name)
    if name == "open_update_window":
        from update.window import open_update_window

        return open_update_window
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
