# PyInstaller spec — Windows one-file HammerCandleBacktestDashboard.exe
# Local:  pyinstaller --noconfirm --clean HammerCandleBacktestDashboard.spec
#
# Bundles every runtime library from requirements.txt + requirements-build.txt.
# Target ~420–450 MB one-file exe on Windows 10/11.

import os
import sys
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH)
entry_script = str(ROOT / "main.py")

datas = []
binaries = []

# Every pip package — keep in sync with requirements-build.txt
# NOTE: do NOT collect_all("google") — pulls conflicting native DLLs and breaks startup.
_ALL_PACKAGES = (
    # requirements.txt
    "PySide6", "shiboken6", "matplotlib", "polars", "pyarrow", "numpy", "PIL",
    "openpyxl", "et_xmlfile", "tzdata", "psutil", "MetaTrader5", "ray",
    # HTTPS / networking
    "certifi", "charset_normalizer", "idna", "urllib3", "requests",
    # matplotlib stack
    "kiwisolver", "fonttools", "contourpy", "cycler", "pyparsing",
    "packaging", "dateutil", "six",
    # excel
    "defusedxml",
    # data IO
    "fsspec",
    # ray core
    "cloudpickle", "filelock", "jsonschema", "jsonschema_specifications",
    "msgpack", "protobuf", "grpcio", "yaml", "click",
    # ray [default]
    "aiohttp", "aiohttp_cors", "aiohappyeyeballs", "aiorwlock", "aiosignal",
    "attrs", "colorful", "distlib", "frozenlist", "multidict",
    "opencensus", "opencensus_context", "platformdirs", "prometheus_client",
    "pydantic", "pydantic_core", "annotated_types", "typing_extensions",
    "referencing", "rpds", "rich", "smart_open", "virtualenv", "watchfiles", "yarl",
    # transitive (specific modules only — not the whole "google" tree)
    "markdown_it", "mdurl", "pygments",
    "google.protobuf", "google.api_core", "google.auth", "googleapis_common_protos",
    "proto", "cachetools", "pyasn1", "pyasn1_modules", "rsa",
    "lz4", "ormsgpack",
)

hiddenimports = [
    # --- app modules ---
    "dashboard", "backtest", "plotting", "logic", "doji_logic", "broker", "live",
    "live_journal", "fetch", "sessions", "hammer_context_logic",
    "telegram_notify", "telegram_workers", "version", "updater",
    "update_workers", "update_window",
    "indicators", "indicators.config", "indicators.filter", "indicators.registry",
    "indicators.supertrend", "indicators.vwap",
    # --- stdlib ---
    "sqlite3", "_sqlite3",
    "ssl", "_ssl", "hashlib", "_hashlib",
    "encodings", "encodings.utf_8", "encodings.cp1252",
    "multiprocessing", "multiprocessing.spawn", "multiprocessing.popen_spawn_win32",
    "zoneinfo",
    # --- PySide6 / Qt ---
    "shiboken6", "shiboken6.Shiboken",
    "PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtNetwork", "PySide6.support", "PySide6.support.deprecated",
    # --- matplotlib (Agg + Qt backends) ---
    "matplotlib", "matplotlib.backends", "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_qtagg", "matplotlib.backends.backend_qt",
    "matplotlib.backends.backend_qt5agg", "matplotlib.figure",
    "matplotlib.pyplot", "matplotlib.dates", "matplotlib.patches",
    # --- data ---
    "numpy", "numpy.core", "numpy.core._multiarray_umath",
    "polars", "pyarrow", "fsspec",
    # --- images (no PIL._imagingtk — requires tkinter which we exclude) ---
    "PIL", "PIL.Image", "PIL._imaging",
    # --- excel ---
    "openpyxl", "openpyxl.styles", "openpyxl.cell", "openpyxl.workbook", "openpyxl.utils",
    "et_xmlfile", "defusedxml", "defusedxml.ElementTree",
    # --- live ---
    "psutil", "_psutil_windows", "MetaTrader5",
    # --- ray ---
    "ray", "ray._private", "ray._private.object_ref_generator",
    "ray._private.worker", "ray._private.services", "ray._private.runtime_env",
    # --- HTTPS ---
    "certifi", "charset_normalizer", "idna", "urllib3", "requests",
    # --- matplotlib / ray deps ---
    "kiwisolver", "fonttools", "contourpy", "cycler", "pyparsing",
    "packaging", "dateutil", "six", "tzdata",
    "cloudpickle", "filelock", "jsonschema", "jsonschema_specifications",
    "msgpack", "google.protobuf", "grpc", "grpcio", "yaml",
    "aiohttp", "aiohttp_cors", "aiohappyeyeballs", "aiorwlock", "aiosignal",
    "attrs", "colorful", "distlib", "frozenlist", "multidict", "yarl",
    "opencensus", "opencensus_context", "platformdirs", "prometheus_client",
    "pydantic", "pydantic_core", "annotated_types", "typing_extensions",
    "referencing", "rpds", "click", "virtualenv", "watchfiles",
    "rich", "smart_open", "markdown_it", "mdurl", "pygments",
    "google.api_core", "google.auth", "googleapis_common_protos",
    "proto", "cachetools", "pyasn1", "pyasn1_modules", "rsa",
    "lz4", "ormsgpack", "lz4.frame",
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


def _collect_dynamic(name: str) -> None:
    """Native .dll/.pyd for packages like polars, pyarrow, PySide6."""
    try:
        libs = collect_dynamic_libs(name)
        binaries.extend(libs)
        print(f"[spec] collect_dynamic_libs({name}): {len(libs)} binaries")
    except Exception as exc:
        print(f"[spec] collect_dynamic_libs({name}) skipped: {exc}")


for _pkg in _ALL_PACKAGES:
    if _pkg == "MetaTrader5" and sys.platform != "win32":
        continue
    _collect_package(_pkg)

# Submodule trees PyInstaller often misses (do NOT collect_submodules PySide6 — breaks startup)
for _mod in (
    "indicators",
    "ray", "ray._private",
    "matplotlib.backends",
    "aiohttp", "pydantic", "grpc", "google.protobuf", "opencensus",
):
    try:
        hiddenimports += collect_submodules(_mod)
        print(f"[spec] collect_submodules({_mod}): OK")
    except Exception as exc:
        print(f"[spec] collect_submodules({_mod}) skipped: {exc}")

for _data_pkg in ("matplotlib", "certifi", "tzdata", "pyarrow", "PySide6", "shiboken6", "polars", "ray"):
    try:
        datas += collect_data_files(_data_pkg)
        print(f"[spec] collect_data_files({_data_pkg}): OK")
    except Exception as exc:
        print(f"[spec] collect_data_files({_data_pkg}) skipped: {exc}")

# Extra native libs — do NOT dedupe by basename; different folders need their own copies.
for _dyn in (
    "PySide6", "shiboken6", "polars", "pyarrow", "numpy",
    "pydantic_core", "grpc", "rpds", "matplotlib", "PIL", "psutil",
):
    _collect_dynamic(_dyn)

# Windows Python stdlib .pyd + OpenSSL (fixes "LoadLibrary / specified module could not be found")
if sys.platform == "win32":
    _win_native = (
        "_sqlite3.pyd", "sqlite3.dll",
        "_ssl.pyd", "_hashlib.pyd",
        "libcrypto-3.dll", "libssl-3.dll",
        "_bz2.pyd", "_lzma.pyd", "_ctypes.pyd",
        "_socket.pyd", "select.pyd",
        "pyexpat.pyd", "_elementtree.pyd",
        "unicodedata.pyd", "_decimal.pyd",
        "_multiprocessing.pyd",
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
        for fname in os.listdir(base):
            if fname.lower().startswith("libffi") and fname.lower().endswith(".dll"):
                binaries.append((os.path.join(base, fname), "."))
                print(f"[spec] bundled {fname}")

# Do NOT dedupe binaries — keeping "first" copy dropped Qt/OpenSSL DLLs and caused
# "The specified module could not be found" on Windows.

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

# One-file exe — default _MEI temp directory (do not set a custom runtime_tmpdir).
# Built with PyInstaller 6.21.0 (see requirements-build.txt) to avoid 6.22.1+
# "Security validation failure: Failed to obtain executable path for parent process".
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
