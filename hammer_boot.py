"""Frozen-exe / app boot diagnostics — all under logs/ next to the .exe."""
from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from datetime import datetime


def exe_dir() -> str:
    """Install root (stub folder), not app/ when running as HammerRuntime."""
    try:
        from update.paths import install_root

        return install_root()
    except Exception:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))


def logs_dir() -> str:
    path = os.path.join(exe_dir(), "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def boot_log_path() -> str:
    return os.path.join(logs_dir(), "boot.log")


def legacy_boot_log_path() -> str:
    """Next to the exe — CI smoke tests and older diagnostics still look here."""
    return os.path.join(exe_dir(), "hammer_boot.log")


def crash_log_path() -> str:
    return os.path.join(logs_dir(), "crash.log")


def legacy_crash_log_path() -> str:
    return os.path.join(exe_dir(), "hammer_crash.log")


def boot_log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"{ts} {msg}\n"
    for path in (boot_log_path(), legacy_boot_log_path()):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except OSError:
            pass


def enable_faulthandler() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        path = os.path.join(logs_dir(), "fault.log")
        fh = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
    except OSError:
        pass


def write_crash(exc: BaseException) -> None:
    body = "Hammer failed to start\n\n" + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    for path in (crash_log_path(), legacy_crash_log_path()):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
        except OSError:
            pass
