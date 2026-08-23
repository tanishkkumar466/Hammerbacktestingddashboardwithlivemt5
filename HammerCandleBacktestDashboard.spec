# PyInstaller spec — Windows one-file HammerCandleBacktestDashboard.exe
# Local:  pyinstaller --noconfirm --clean HammerCandleBacktestDashboard.spec
# CI:     .github/workflows/release-windows-exe.yml

import os
import sys
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
entry_script = str(ROOT / "main.py")

datas = []
binaries = []
hiddenimports = [
    # --- app modules ---
    "dashboard",
    "backtest",
    "plotting",
    "logic",
    "doji_logic",
    "broker",
    "live",
    "live_journal",
    "fetch",
    "sessions",
    "hammer_context_logic",
    "telegram_notify",
    "telegram_workers",
    "version",
    "updater",
    "update_workers",
    "update_window",
    "indicators",
    "indicators.config",
    "indicators.filter",
    "indicators.registry",
    "indicators.supertrend",
    "indicators.vwap",
    # --- stdlib / runtime (sqlite3 often missing if not explicit) ---
    "sqlite3",
    "_sqlite3",
    "encodings",
    "encodings.utf_8",
    "encodings.cp1252",
    # --- third party ---
    "MetaTrader5",
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.cell",
    "openpyxl.workbook",
    "openpyxl.utils",
    "matplotlib",
    "matplotlib.backends",
    "matplotlib.backends.backend_agg",
    "numpy",
    "numpy.core",
    "numpy.core._multiarray_umath",
    "PIL",
    "PIL.Image",
    "polars",
    "psutil",
    "shiboken6",
    "shiboken6.Shiboken",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.support",
]

# Bundle icons for get_asset_path()
for asset_name in ("logo.ico", "app_icon.ico", "app_icon.png", "logo.png"):
    asset_path = ROOT / asset_name
    if asset_path.is_file():
        datas.append((str(asset_path), "."))


def _collect_package(name: str) -> None:
    """Merge collect_all() output; skip optional packages cleanly."""
    try:
        d, b, h = collect_all(name)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    except Exception as exc:
        print(f"[spec] collect_all({name}) skipped: {exc}")


# Heavy deps — must be fully bundled (missing = tiny broken exe / ModuleNotFoundError)
for _pkg in (
    "PySide6",
    "shiboken6",
    "polars",
    "numpy",
    "matplotlib",
    "PIL",
    "openpyxl",
    "psutil",
):
    _collect_package(_pkg)

try:
    hiddenimports += collect_submodules("indicators")
except Exception:
    pass

datas += collect_data_files("matplotlib")

# Windows: explicitly bundle sqlite3 native extension (fixes "No module named _sqlite3")
if sys.platform == "win32":
    _search_roots = [
        sysconfig.get_path("stdlib"),
        os.path.join(sys.base_prefix, "DLLs"),
        sys.base_prefix,
    ]
    for base in _search_roots:
        if not base or not os.path.isdir(base):
            continue
        for fname in ("_sqlite3.pyd", "sqlite3.dll"):
            fpath = os.path.join(base, fname)
            if os.path.isfile(fpath):
                binaries.append((fpath, "."))
                print(f"[spec] bundled {fname} from {base}")

_exe_icon = None
for icon_name in ("logo.ico", "app_icon.ico"):
    p = ROOT / icon_name
    if p.is_file():
        _exe_icon = str(p)
        break

block_cipher = None
_runtime_hooks = [str(ROOT / "pyinstaller" / "hammer_runtime_hook.py")]

a = Analysis(
    [entry_script],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=_runtime_hooks,
    excludes=["ray", "tkinter", "pytest", "IPython"],  # optional / unused
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-file exe — upx off, extract cache beside exe (avoids temp-dir / interpreter errors)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HammerCandleBacktestDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir="_hammer_pyi",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)
