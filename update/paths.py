"""
Install-root layout for frozen Hammer (stub launcher + app runtime).

  <install_root>/
    HammerCandleBacktestDashboard.exe   # stub (user double-clicks this)
    app/
      HammerRuntime.exe                 # real PyInstaller dashboard
      HammerRuntime.exe.pending         # downloaded update waiting to apply
    logs/
    data/
    ...

Legacy single-file installs (exe at install root, no app/) still work until
the next Check-for-Updates migrates them into this layout.
"""
from __future__ import annotations

import os
import sys

INSTALL_ROOT_ENV = "HAMMER_INSTALL_ROOT"
STUB_EXE_NAME = "HammerCandleBacktestDashboard.exe"
RUNTIME_EXE_NAME = "HammerRuntime.exe"
APP_SUBDIR = "app"
PENDING_SUFFIX = ".pending"
MIN_RUNTIME_BYTES = 350_000_000


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_root() -> str:
    """Folder that holds the stub, data/, logs/ — never the inner app/ folder."""
    env = (os.environ.get(INSTALL_ROOT_ENV) or "").strip()
    if env and os.path.isdir(env):
        return os.path.abspath(env)

    if _frozen():
        exe = os.path.abspath(sys.executable)
        parent = os.path.dirname(exe)
        # Runtime lives in <root>/app/HammerRuntime.exe
        if os.path.basename(parent).lower() == APP_SUBDIR:
            return os.path.dirname(parent)
        return parent

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir(root: str | None = None) -> str:
    return os.path.join(root or install_root(), APP_SUBDIR)


def stub_exe_path(root: str | None = None) -> str:
    return os.path.join(root or install_root(), STUB_EXE_NAME)


def runtime_exe_path(root: str | None = None) -> str:
    return os.path.join(app_dir(root), RUNTIME_EXE_NAME)


def pending_runtime_path(root: str | None = None) -> str:
    return runtime_exe_path(root) + PENDING_SUFFIX


def is_stub_layout(root: str | None = None) -> bool:
    root = root or install_root()
    try:
        return (
            os.path.isfile(stub_exe_path(root))
            and os.path.isfile(runtime_exe_path(root))
            and os.path.getsize(runtime_exe_path(root)) >= MIN_RUNTIME_BYTES
            and os.path.getsize(stub_exe_path(root)) < 80_000_000
        )
    except OSError:
        return False


def is_running_as_runtime() -> bool:
    if not _frozen():
        return False
    exe = os.path.abspath(sys.executable)
    parent = os.path.dirname(exe)
    return (
        os.path.basename(parent).lower() == APP_SUBDIR
        and os.path.basename(exe).lower() == RUNTIME_EXE_NAME.lower()
    )


def is_running_as_legacy_onefile() -> bool:
    """Frozen exe at install root (pre-stub), not app/HammerRuntime.exe."""
    if not _frozen():
        return False
    if is_running_as_runtime():
        return False
    # Stub itself is small and named STUB_EXE_NAME at root — treat as not legacy app
    name = os.path.basename(sys.executable).lower()
    if name == STUB_EXE_NAME.lower():
        # Could be stub or legacy one-file with same name; size distinguishes
        try:
            return os.path.getsize(sys.executable) >= MIN_RUNTIME_BYTES
        except OSError:
            return True
    return True
