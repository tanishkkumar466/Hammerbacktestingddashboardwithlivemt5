# PyInstaller spec — Windows one-file HammerCandleBacktestDashboard.exe
# Local:  pyinstaller --noconfirm --clean HammerCandleBacktestDashboard.spec
#
# Bundles every runtime library Hammer needs on Windows 10/11 (~400–450 MB one-file).
# Do NOT exclude ray — live optional compute requires the full ray tree.

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
    "dashboard", "backtest", "plotting", "logic", "doji_logic", "broker", "live",
    "live_journal", "fetch", "sessions", "hammer_context_logic",
    "telegram_notify", "telegram_workers", "version", "updater",
    "update_workers", "update_window",
    "indicators", "indicators.config", "indicators.filter", "indicators.registry",
    "indicators.supertrend", "indicators.vwap",
    # --- stdlib (sqlite, ssl/https, multiprocessing for ray) ---
    "sqlite3", "_sqlite3",
    "ssl", "_ssl", "hashlib", "_hashlib",
    "encodings", "encodings.utf_8", "encodings.cp1252",
    "multiprocessing", "multiprocessing.spawn", "multiprocessing.popen_spawn_win32",
    "zoneinfo",
    # --- UI: PySide6 / Qt ---
    "shiboken6", "shiboken6.Shiboken",
    "PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtNetwork", "PySide6.support", "PySide6.support.deprecated",
    # --- charts: matplotlib (Agg PNG export + Qt inspect dialog) ---
    "matplotlib", "matplotlib.backends", "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_qtagg", "matplotlib.backends.backend_qt",
    "matplotlib.backends.backend_qt5agg", "matplotlib.figure",
    "matplotlib.pyplot", "matplotlib.dates", "matplotlib.patches",
    # --- data / backtest ---
    "numpy", "numpy.core", "numpy.core._multiarray_umath",
    "polars", "pyarrow",
    # --- images ---
    "PIL", "PIL.Image", "PIL._imaging",
    # --- excel export chain ---
    "openpyxl", "openpyxl.styles", "openpyxl.cell", "openpyxl.workbook", "openpyxl.utils",
    "et_xmlfile", "defusedxml", "defusedxml.ElementTree",
    # --- live / system ---
    "psutil", "_psutil_windows",
    # --- HTTPS (updater + telegram via urllib/ssl) ---
    "certifi", "charset_normalizer", "idna", "urllib3",
    # --- matplotlib deps ---
    "kiwisolver", "fonttools", "contourpy", "cycler", "pyparsing",
    "packaging", "dateutil", "six",
    # --- timezones (Windows) ---
    "tzdata",
    # --- Windows live: MT5 + ray ---
    "MetaTrader5",
    "ray", "ray._private", "ray._private.object_ref_generator",
    "ray._private.worker", "ray._private.services", "ray._private.runtime_env",
    # --- ray transitive (ray[default] — must match local ~430 MB build) ---
    "cloudpickle", "filelock", "jsonschema", "jsonschema_specifications", "msgpack", "yaml",
    "google.protobuf", "grpc", "grpcio",
    "aiohttp", "aiohttp_cors", "aiohappyeyeballs", "aiosignal", "attrs",
    "frozenlist", "multidict", "yarl", "referencing", "rpds",
    "pydantic", "pydantic_core", "annotated_types", "typing_extensions",
    "click", "colorful", "virtualenv", "watchfiles", "prometheus_client",
    "requests", "rich", "smart_open", "opencensus", "fsspec",
]

for asset_name in ("logo.ico", "app_icon.ico", "app_icon.png", "logo.png"):
    asset_path = ROOT / asset_name
    if asset_path.is_file():
        datas.append((str(asset_path), "."))


def _collect_package(name: str) -> None:
    try:
        d, b, h = collect_all(name)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
        print(f"[spec] collect_all({name}): {len(d)} datas, {len(b)} binaries, {len(h)} hidden")
    except Exception as exc:
        print(f"[spec] collect_all({name}) skipped: {exc}")


# requirements.txt + requirements-build.txt (target ~430 MB one-file exe on Windows)
_RUNTIME_PACKAGES = (
    # UI
    "PySide6",
    "shiboken6",
    # data / charts
    "polars",
    "pyarrow",
    "numpy",
    "matplotlib",
    "PIL",
    # excel
    "openpyxl",
    "et_xmlfile",
    "defusedxml",
    # live helpers
    "psutil",
    # HTTPS / CA bundle
    "certifi",
    "charset_normalizer",
    "requests",
    # matplotlib stack
    "kiwisolver",
    "fonttools",
    "contourpy",
    "cycler",
    "pyparsing",
    "packaging",
    "dateutil",
    "six",
    # timezones / IO
    "tzdata",
    "fsspec",
    # ray core deps
    "cloudpickle",
    "filelock",
    "jsonschema",
    "jsonschema_specifications",
    "msgpack",
    "protobuf",
    "grpcio",
    "PyYAML",
    "click",
    # ray[default] extras (biggest CI vs local size gap)
    "aiohttp",
    "aiohttp_cors",
    "aiohappyeyeballs",
    "aiosignal",
    "attrs",
    "frozenlist",
    "multidict",
    "yarl",
    "referencing",
    "rpds",
    "pydantic",
    "pydantic_core",
    "annotated_types",
    "typing_extensions",
    "colorful",
    "virtualenv",
    "watchfiles",
    "prometheus_client",
    "rich",
    "smart_open",
    "opencensus",
)

for _pkg in _RUNTIME_PACKAGES:
    _collect_package(_pkg)

if sys.platform == "win32":
    for _pkg in ("MetaTrader5", "ray"):
        _collect_package(_pkg)

# Full submodule trees — ray._private.* fixes "No module named ray._private.object_ref_generator"
for _mod in ("indicators", "ray", "ray._private", "matplotlib.backends"):
    try:
        hiddenimports += collect_submodules(_mod)
    except Exception as exc:
        print(f"[spec] collect_submodules({_mod}) skipped: {exc}")

# Data files not always picked up by collect_all alone
for _data_pkg in ("matplotlib", "certifi", "tzdata", "pyarrow", "PySide6"):
    try:
        datas += collect_data_files(_data_pkg)
    except Exception as exc:
        print(f"[spec] collect_data_files({_data_pkg}) skipped: {exc}")

# Windows native extensions (sqlite3, OpenSSL for HTTPS)
if sys.platform == "win32":
    _win_native = (
        "_sqlite3.pyd",
        "sqlite3.dll",
        "_ssl.pyd",
        "_hashlib.pyd",
        "libcrypto-3.dll",
        "libssl-3.dll",
        "pyexpat.pyd",
        "_elementtree.pyd",
    )
    for base in (
        sysconfig.get_path("stdlib"),
        os.path.join(sys.base_prefix, "DLLs"),
        sys.base_prefix,
    ):
        if not base or not os.path.isdir(base):
            continue
        for fname in _win_native:
            fpath = os.path.join(base, fname)
            if os.path.isfile(fpath):
                binaries.append((fpath, "."))
                print(f"[spec] bundled {fname}")

_exe_icon = None
for icon_name in ("logo.ico", "app_icon.ico"):
    p = ROOT / icon_name
    if p.is_file():
        _exe_icon = str(p)
        break

a = Analysis(
    [entry_script],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "pyinstaller" / "hammer_runtime_hook.py")],
    excludes=["tkinter", "pytest", "IPython", "jupyter", "notebook"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# One-file exe — default _MEI temp (required for PyInstaller 6.22+ security validation).
# Do NOT set runtime_tmpdir to a custom name — breaks "parent process" security checks.
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
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)
