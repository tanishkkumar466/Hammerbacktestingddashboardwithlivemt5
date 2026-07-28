# PyInstaller spec — used by .github/workflows/build-windows-exe.yml
# Run locally on Windows: pyinstaller HammerCandleBacktestDashboard.spec

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH)

# Entry: main.py sets cwd to exe folder then launches dashboard (data/output paths)
entry_script = str(ROOT / "main.py")

datas = []
binaries = []
hiddenimports = [
    "backtest",
    "plotting",
    "logic",
    "doji_logic",
    "broker",
    "live",
    "live_journal",
    "fetch",
    "indicators",
    "indicators.config",
    "indicators.filter",
    "indicators.registry",
    "indicators.supertrend",
    "indicators.vwap",
    "MetaTrader5",
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.cell",
    "openpyxl.workbook",
    "matplotlib.backends.backend_agg",
    "sqlite3",
    "psutil",
    "numpy",
    "PIL",
    "PIL.Image",
]

# Bundle window / taskbar icons into the onefile extract (_MEIPASS) for get_asset_path()
for asset_name in ("logo.ico", "app_icon.ico", "app_icon.png", "logo.png"):
    asset_path = ROOT / asset_name
    if asset_path.is_file():
        datas.append((str(asset_path), "."))

# PySide6 (Qt platform plugins — required or exe fails to start on Windows)
tmp = collect_all("PySide6")
datas += tmp[0]
binaries += tmp[1]
hiddenimports += tmp[2]

# Matplotlib fonts / data (charts tab)
datas += collect_data_files("matplotlib")

# Polars runtime pieces
tmp = collect_all("polars")
datas += tmp[0]
binaries += tmp[1]
hiddenimports += tmp[2]

try:
    tmp = collect_all("psutil")
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]
except Exception:
    pass

# EXE icon: prefer logo.ico (client branding), then app_icon.ico
_exe_icon = None
for icon_name in ("logo.ico", "app_icon.ico"):
    p = ROOT / icon_name
    if p.is_file():
        _exe_icon = str(p)
        break

block_cipher = None

a = Analysis(
    [entry_script],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["ray"],  # optional live feature; large / not needed in frozen build
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)
