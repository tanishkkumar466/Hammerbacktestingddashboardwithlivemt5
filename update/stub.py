"""
Tiny Windows stub launcher for Hammer.

Double-click HammerCandleBacktestDashboard.exe (this stub). It:
  1) Applies app/HammerRuntime.exe.pending if present
  2) Launches app/HammerRuntime.exe with HAMMER_INSTALL_ROOT set
  3) Exits (the runtime is the real UI)

Keep this module free of Qt / polars / heavy imports so the stub stays small.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

from update.paths import (
    INSTALL_ROOT_ENV,
    MIN_RUNTIME_BYTES,
    app_dir,
    install_root,
    pending_runtime_path,
    runtime_exe_path,
)


def _log(root: str, msg: str) -> None:
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs, exist_ok=True)
        path = os.path.join(logs, "stub.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass
    try:
        # Also next to stub for early diagnostics
        with open(os.path.join(root, "hammer_boot.log"), "a", encoding="utf-8") as f:
            f.write(f"stub: {msg}\n")
    except OSError:
        pass


def _unblock(path: str) -> None:
    if os.name != "nt":
        return
    zone = path + ":Zone.Identifier"
    try:
        if os.path.exists(zone):
            os.remove(zone)
    except OSError:
        pass
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f'Unblock-File -LiteralPath "{path}"',
            ],
            check=False,
            capture_output=True,
            creationflags=flags,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _apply_pending(root: str) -> None:
    pending = pending_runtime_path(root)
    current = runtime_exe_path(root)
    if not os.path.isfile(pending):
        return
    try:
        size = os.path.getsize(pending)
    except OSError:
        return
    if size < MIN_RUNTIME_BYTES:
        _log(root, f"pending too small ({size}); leaving in place")
        return

    os.makedirs(app_dir(root), exist_ok=True)
    old = current + ".old"
    try:
        if os.path.isfile(old):
            try:
                os.remove(old)
            except OSError:
                old = current + f".old.{os.getpid()}"
        if os.path.isfile(current):
            os.replace(current, old)
        os.replace(pending, current)
        _unblock(current)
        _log(root, "applied pending runtime")
        try:
            os.remove(old)
        except OSError:
            pass
    except OSError as exc:
        _log(root, f"pending apply failed: {exc}")
        # Try to restore
        try:
            if os.path.isfile(old) and not os.path.isfile(current):
                os.replace(old, current)
        except OSError:
            pass


def _message_box(title: str, text: str) -> None:
    if os.name != "nt":
        print(f"{title}: {text}")
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


def main() -> int:
    # When frozen, this exe sits at install root
    if getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = install_root()

    os.environ[INSTALL_ROOT_ENV] = root
    _log(root, "stub start")
    _apply_pending(root)

    runtime = runtime_exe_path(root)
    if not os.path.isfile(runtime):
        # Dev convenience: running stub.py from source without a package
        if not getattr(sys, "frozen", False):
            main_py = os.path.join(root, "main.py")
            if os.path.isfile(main_py):
                _log(root, "dev: launching main.py")
                return subprocess.call([sys.executable, main_py, *sys.argv[1:]], cwd=root)
        msg = (
            f"Hammer runtime not found:\n{runtime}\n\n"
            "Re-download Hammer-windows.zip from GitHub Releases, or use "
            "Help → Check for Updates from a working install."
        )
        _log(root, msg)
        _message_box("Hammer", msg)
        return 1

    try:
        size = os.path.getsize(runtime)
    except OSError as exc:
        _message_box("Hammer", f"Cannot read runtime:\n{exc}")
        return 1
    if size < MIN_RUNTIME_BYTES:
        msg = (
            f"Runtime looks incomplete ({size / (1024 * 1024):.1f} MB).\n"
            f"Expected ~420–450 MB at:\n{runtime}"
        )
        _log(root, msg)
        _message_box("Hammer", msg)
        return 1

    _unblock(runtime)
    env = os.environ.copy()
    env[INSTALL_ROOT_ENV] = root
    # Fresh PyInstaller child — do not inherit stub/bootloader private vars
    for key in list(env):
        if str(key).startswith("_PYI_"):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

    args = [runtime, *sys.argv[1:]]
    _log(root, f"launching {runtime}")
    try:
        if os.name == "nt":
            # Detach so closing the stub console (if any) does not kill the UI
            creation = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                args,
                cwd=root,
                env=env,
                close_fds=True,
                creationflags=creation,
            )
        else:
            subprocess.Popen(args, cwd=root, env=env, close_fds=True)
    except OSError as exc:
        _log(root, f"launch failed: {exc}")
        _message_box("Hammer", f"Could not start Hammer runtime:\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        root = (
            os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))
        )
        _log(root, "stub CRASHED:\n" + traceback.format_exc())
        _message_box("Hammer", "Hammer launcher crashed. See logs\\stub.log")
        raise SystemExit(1)
