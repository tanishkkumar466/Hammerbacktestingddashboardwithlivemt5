"""Frozen-exe / app boot diagnostics — all under logs/ next to the .exe."""
from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from datetime import datetime


def exe_dir() -> str:
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


def crash_log_path() -> str:
    return os.path.join(logs_dir(), "crash.log")


def boot_log(msg: str) -> None:
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        with open(boot_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
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
    try:
        with open(crash_log_path(), "w", encoding="utf-8") as f:
            f.write("Hammer failed to start\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
            f.flush()
    except OSError:
        pass
