"""
One-hop migration: legacy one-file EXE → stub launcher + app/HammerRuntime.exe.

Old clients (e.g. 1.0.14–1.0.26) download the large bridge EXE via Check for Updates.
That build is the full runtime with an embedded stub. On first launch we:

  1) Copy this running EXE → app/HammerRuntime.exe
  2) Replace the top-level EXE with the embedded stub launcher
  3) Relaunch the stub (which starts the runtime)

If anything fails, we keep running as one-file so the client is not bricked.
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
from typing import Optional

from update.paths import (
    MIN_RUNTIME_BYTES,
    STUB_EXE_NAME,
    app_dir,
    install_root,
    is_running_as_legacy_onefile,
    is_running_as_runtime,
    runtime_exe_path,
    stub_exe_path,
)

_EMBEDDED_STUB_DIR = "_hammer_embedded_stub"
_MAX_STUB_BYTES = 80_000_000


def _log(msg: str) -> None:
    try:
        from hammer_boot import boot_log

        boot_log(f"migrate: {msg}")
    except Exception:
        try:
            root = install_root()
            path = os.path.join(root, "hammer_boot.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"migrate: {msg}\n")
        except OSError:
            pass


def find_embedded_stub() -> Optional[str]:
    """Path to stub EXE baked into this one-file build (PyInstaller datas)."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    direct = os.path.join(meipass, _EMBEDDED_STUB_DIR, STUB_EXE_NAME)
    if os.path.isfile(direct):
        try:
            if os.path.getsize(direct) < _MAX_STUB_BYTES:
                return direct
        except OSError:
            return None
    # Any exe under the embed folder
    folder = os.path.join(meipass, _EMBEDDED_STUB_DIR)
    if not os.path.isdir(folder):
        return None
    for name in os.listdir(folder):
        if not name.lower().endswith(".exe"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) < _MAX_STUB_BYTES:
                return path
        except OSError:
            continue
    return None


def needs_legacy_stub_migration() -> bool:
    if os.environ.get("HAMMER_SKIP_STUB_MIGRATE", "").strip() in ("1", "true", "yes"):
        return False
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    if is_running_as_runtime():
        return False
    if not is_running_as_legacy_onefile():
        return False
    # Already fully migrated: small stub at root + runtime in app/
    root = install_root()
    runtime = runtime_exe_path(root)
    stub = stub_exe_path(root)
    try:
        if (
            os.path.isfile(runtime)
            and os.path.getsize(runtime) >= MIN_RUNTIME_BYTES
            and os.path.isfile(stub)
            and os.path.getsize(stub) < _MAX_STUB_BYTES
            and os.path.abspath(sys.executable) == os.path.abspath(stub)
        ):
            return False
    except OSError:
        pass
    return find_embedded_stub() is not None


def migrate_legacy_onefile_to_stub_if_needed() -> bool:
    """
    Perform one-hop stub migration when appropriate.

    Returns True if this process should exit immediately (relaunch scheduled).
    """
    if not needs_legacy_stub_migration():
        return False

    embedded = find_embedded_stub()
    if not embedded:
        return False

    root = install_root()
    current = os.path.abspath(sys.executable)
    _log(f"starting one-hop migration from {current}")

    try:
        os.makedirs(app_dir(root), exist_ok=True)
        dest_runtime = runtime_exe_path(root)

        # Copy running one-file → app/HammerRuntime.exe (allowed while running on Windows)
        if (
            not os.path.isfile(dest_runtime)
            or os.path.getsize(dest_runtime) < MIN_RUNTIME_BYTES
            or os.path.abspath(dest_runtime) != current
        ):
            _log(f"copying runtime → {dest_runtime}")
            tmp_rt = dest_runtime + ".migrating"
            shutil.copy2(current, tmp_rt)
            if os.path.getsize(tmp_rt) < MIN_RUNTIME_BYTES:
                raise OSError("copied runtime too small")
            os.replace(tmp_rt, dest_runtime)

        from update.updater import (
            _spawn_frozen_relaunch,
            _swap_exe_while_running,
            _unblock_windows_download,
            _write_windows_update_bat,
            _windows_relaunch_lines,
            _windows_replace_exe_lines,
            _bat_echo_log,
            update_log_path,
            _WINDOWS_UPDATE_BAT,
        )

        _unblock_windows_download(dest_runtime)

        staged_stub = os.path.join(root, STUB_EXE_NAME + ".migrate-new")
        shutil.copy2(embedded, staged_stub)
        _unblock_windows_download(staged_stub)
        if os.path.getsize(staged_stub) >= _MAX_STUB_BYTES:
            raise OSError("embedded stub too large")

        # If we are already named as the stub path, rename-swap in place.
        # Otherwise install stub next to us under the canonical name and relaunch it.
        target_stub = stub_exe_path(root)
        log_path = update_log_path()

        if os.path.abspath(current) == os.path.abspath(target_stub):
            swapped = _swap_exe_while_running(
                staged_stub, current, min_bytes=50_000
            )
            if swapped:
                _log("stub swapped into place")
                _spawn_frozen_relaunch(current, root)
                return True
            _log("swap failed — scheduling bat fallback")
            bat = _write_windows_update_bat(
                pid=os.getpid(),
                exe_path=current,
                lines=[
                    _bat_echo_log(log_path, "One-hop stub migration bat fallback"),
                    *_windows_replace_exe_lines(
                        staged=staged_stub, current_exe=current, log_path=log_path
                    ),
                    *_windows_relaunch_lines(root, current, log_path),
                ],
            )
            _WINDOWS_UPDATE_BAT[0] = bat
            import subprocess
            import time

            subprocess.Popen(["cmd", "/c", bat], close_fds=True)
            time.sleep(1.0)
            return True

        # Running under a non-canonical name: write stub to canonical path, relaunch stub
        shutil.copy2(staged_stub, target_stub)
        _unblock_windows_download(target_stub)
        try:
            os.remove(staged_stub)
        except OSError:
            pass
        _log(f"wrote stub at {target_stub}; relaunching")
        _spawn_frozen_relaunch(target_stub, root)
        return True

    except Exception as exc:
        _log(f"migration FAILED (continuing as one-file): {exc}")
        _log(traceback.format_exc())
        # Best-effort: if runtime is already in app/, still usable next to legacy exe
        return False
