# -*- mode: python ; coding: utf-8 -*-
# Tiny stub launcher — ships as HammerCandleBacktestDashboard.exe
# Build: pyinstaller --noconfirm --clean HammerStub.spec
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

_icon = None
for icon_name in ("logo.ico", "app_icon.ico"):
    p = ROOT / icon_name
    if p.is_file():
        _icon = str(p)
        break

a = Analysis(
    ["update/stub.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["update", "update.paths", "update.stub"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "shiboken6",
        "polars",
        "pyarrow",
        "numpy",
        "pandas",
        "ray",
        "MetaTrader5",
        "matplotlib",
        "PIL",
        "tkinter",
        "pytest",
    ],
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
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)
