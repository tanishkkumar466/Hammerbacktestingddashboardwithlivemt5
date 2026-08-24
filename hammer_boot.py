"""Frozen-exe boot diagnostics — append to hammer_boot.log next to the .exe."""
from __future__ import annotations

import faulthandler
import os
import sys
import traceback


def exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def boot_log(msg: str) -> None:
    try:
        with open(os.path.join(exe_dir(), "hammer_boot.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def enable_faulthandler() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        path = os.path.join(exe_dir(), "hammer_crash.log")
        fh = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
    except OSError:
        pass


def write_crash(exc: BaseException) -> None:
    try:
        with open(os.path.join(exe_dir(), "hammer_crash.log"), "w", encoding="utf-8") as f:
            f.write("Hammer failed to start\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except OSError:
        pass
