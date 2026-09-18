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
    "dashboard", "backtest", "plotting", "logic", "doji_logic", "broker",     "live",
    "live_accounts", "live_account_ipc", "live_account_worker",
    "live_journal", "fetch", "sessions", "hammer_context_logic",
    "telegram_notify", "telegram_workers", "version", "updater",
    "update_workers", "update_window", "hammer_boot",
    "indicators", "indicators.config", "indicators.filter", "indicators.registry",
    "indicators.supertrend", "indicators.vwap", "indicators.rolling_vwap", "indicators.rsi",
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
    "polars", "polars.polars", "pyarrow", "fsspec",
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

# Extra native libs for packages PyInstaller often under-collects.
# Keep collect_dynamic_libs — but NEVER force-copy libcrypto/libssl into the
# bundle root (that caused OpenSSL mismatches: CI exe won't open, local can).
for _dyn in (
    "PySide6", "shiboken6", "polars", "pyarrow", "numpy",
    "pydantic_core", "grpc", "rpds", "matplotlib", "PIL", "psutil",
):
    _collect_dynamic(_dyn)


def _collect_package_natives(pkg: str) -> None:
    """Walk site-packages/<pkg> for .pyd/.dll — collect_dynamic_libs(polars) often returns 0."""
    try:
        import importlib.util

        spec = importlib.util.find_spec(pkg)
        if spec is None:
            print(f"[spec] native walk {pkg}: not installed")
            return
        roots = []
        if spec.origin and os.path.isfile(spec.origin):
            roots.append(os.path.dirname(spec.origin))
        if spec.submodule_search_locations:
            roots.extend(list(spec.submodule_search_locations))
        seen = set()
        count = 0
        for pkg_dir in roots:
            pkg_dir = os.path.abspath(pkg_dir)
            if not os.path.isdir(pkg_dir) or pkg_dir in seen:
                continue
            seen.add(pkg_dir)
            parent = os.path.dirname(pkg_dir)
            for dirpath, _dirnames, filenames in os.walk(pkg_dir):
                for fn in filenames:
                    low = fn.lower()
                    if not low.endswith((".pyd", ".dll", ".so")):
                        continue
                    src = os.path.join(dirpath, fn)
                    rel = os.path.relpath(dirpath, parent)
                    dest = "." if rel == "." else rel
                    binaries.append((src, dest))
                    count += 1
                    print(f"[spec] native {pkg}: {fn} -> {dest}")
            libs = pkg_dir + ".libs"
            if os.path.isdir(libs):
                dest_libs = os.path.basename(libs)
                for fn in os.listdir(libs):
                    if fn.lower().endswith(".dll"):
                        binaries.append((os.path.join(libs, fn), dest_libs))
                        count += 1
                        print(f"[spec] native {pkg}: {dest_libs}/{fn}")
        print(f"[spec] native walk {pkg}: {count} files")
    except Exception as exc:
        print(f"[spec] native walk {pkg} skipped: {exc}")


for _pkg in ("polars", "pyarrow", "numpy"):
    _collect_package_natives(_pkg)

# Only force-bundle sqlite3 — do NOT copy libcrypto/libssl/_ssl manually.
# PyInstaller + Python already ship matching OpenSSL for _ssl.pyd.
# Manually adding libcrypto-3.dll / libssl-3.dll next to Qt's libcrypto-3-x64.dll
# makes the CI one-file exe crash on start ("module could not be found" / silent exit).
if sys.platform == "win32":
    for base in (
        sysconfig.get_path("stdlib"),
        os.path.join(sys.base_prefix, "DLLs"),
        sys.base_prefix,
    ):
        if not base or not os.path.isdir(base):
            continue
        for fname in ("_sqlite3.pyd", "sqlite3.dll"):
            fpath = os.path.join(base, fname)
            if os.path.isfile(fpath):
                binaries.append((fpath, "."))
                print(f"[spec] bundled {fname}")


def _binary_src(item) -> str:
    if isinstance(item, (tuple, list)) and item:
        return str(item[0])
    return str(item)


def _is_openssl_dll(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name.startswith("libssl") or name.startswith("libcrypto")


def _openssl_priority(path: str) -> int:
    """Higher = keep. Prefer Python's DLLs and PySide6 over random PATH copies."""
    low = path.replace("\\", "/").lower()
    score = 0
    if "pyside6" in low or "shiboken6" in low:
        score += 100
    if "/dlls/" in low or low.endswith("/dlls") or "\\dlls\\" in path.lower():
        score += 80
    if "python" in low:
        score += 40
    if "program files" in low and "python" not in low:
        score -= 200  # MySQL / FireDaemon / HP OpenSSL on CI runners
    if "openssl" in low and "python" not in low and "pyside" not in low:
        score -= 100
    return score


def _sanitize_binaries(items: list) -> list:
    """
    Drop conflicting OpenSSL DLLs collected from PATH (common on GitHub Actions).
    Keep at most one libssl* / libcrypto* pair from the best source.
    """
    openssl: dict[str, tuple[int, object]] = {}
    out: list = []
    for item in items:
        src = _binary_src(item)
        if not _is_openssl_dll(src):
            out.append(item)
            continue
        # Normalize key: libssl-3.dll and libssl-3-x64.dll are different Qt vs CPython names
        key = os.path.basename(src).lower()
        pri = _openssl_priority(src)
        prev = openssl.get(key)
        if prev is None or pri > prev[0]:
            if prev is not None:
                print(f"[spec] drop weaker OpenSSL: {_binary_src(prev[1])} (pri={prev[0]})")
            openssl[key] = (pri, item)
            print(f"[spec] keep OpenSSL: {src} (pri={pri})")
        else:
            print(f"[spec] drop OpenSSL: {src} (pri={pri} < {prev[0]})")
    out.extend(item for _, item in openssl.values())
    return out


binaries = _sanitize_binaries(binaries)

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

# Analysis discovers more DLLs from PATH — sanitize again (CI runners often have
# MySQL/FireDaemon OpenSSL that break the frozen exe on a clean user PC).
a.binaries = _sanitize_binaries(list(a.binaries))

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# One-file exe — default _MEI temp directory (do not set a custom runtime_tmpdir).
# Built with PyInstaller 6.19.0 — MUST match the local Windows build that opens.
# See requirements-build.txt (do not bump without rebuilding + testing on a clean PC).
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
