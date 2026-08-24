"""
PyInstaller runtime hook — run before main.py in the frozen exe.

- Writes hammer_boot.log next to the exe (proves Python started)
- Puts _MEIPASS on PATH and registers DLL folders
- Points Qt at bundled platform plugins (qwindows.dll)
"""
import os
import sys
import traceback


def _exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _boot_log(msg: str) -> None:
    try:
        path = os.path.join(_exe_dir(), "hammer_boot.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _register_dll_dir(path: str, seen: set) -> None:
    if not path or path in seen or not os.path.isdir(path):
        return
    seen.add(path)
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(path)
        except OSError:
            pass


try:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # Fresh boot marker (overwrite) — if this file never appears, bootloader
        # died before Python (bad DLL / OpenSSL), not the updater.
        try:
            with open(os.path.join(_exe_dir(), "hammer_boot.log"), "w", encoding="utf-8") as f:
                f.write("boot: python started\n")
                f.write(f"meipass: {sys._MEIPASS}\n")
                f.write(f"executable: {sys.executable}\n")
        except OSError:
            pass

        meipass = sys._MEIPASS
        if meipass not in sys.path:
            sys.path.insert(0, meipass)

        # Prefer _MEIPASS first so bundled OpenSSL/Qt win over PATH copies
        os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")

        _seen_dirs: set = set()
        _register_dll_dir(meipass, _seen_dirs)

        # Register native-lib folders (numpy.libs, pyarrow, PySide6, …)
        # Do not walk the entire tree blindly for add_dll_directory order —
        # register .libs and known package dirs first.
        prefer = []
        try:
            for entry in os.listdir(meipass):
                full = os.path.join(meipass, entry)
                if not os.path.isdir(full):
                    continue
                if entry.endswith(".libs") or entry in (
                    "PySide6", "shiboken6", "numpy", "pyarrow", "polars", "PIL",
                ):
                    prefer.append(full)
            for full in prefer:
                _register_dll_dir(full, _seen_dirs)
                for dirpath, _dirnames, filenames in os.walk(full):
                    if any(n.lower().endswith((".dll", ".pyd")) for n in filenames):
                        _register_dll_dir(dirpath, _seen_dirs)
        except OSError as exc:
            _boot_log(f"dll scan error: {exc}")

        # PySide6 platform plugin (qwindows.dll) — required for QApplication
        qt_ok = False
        for plugins in (
            os.path.join(meipass, "PySide6", "plugins"),
            os.path.join(meipass, "PySide6", "Qt6", "plugins"),
            os.path.join(meipass, "PySide6", "Qt", "plugins"),
        ):
            if os.path.isdir(plugins):
                os.environ["QT_PLUGIN_PATH"] = plugins
                platforms = os.path.join(plugins, "platforms")
                os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platforms
                _register_dll_dir(plugins, _seen_dirs)
                _register_dll_dir(platforms, _seen_dirs)
                qwindows = os.path.join(platforms, "qwindows.dll")
                _boot_log(f"qt plugins: {plugins}")
                _boot_log(f"qwindows exists: {os.path.isfile(qwindows)}")
                qt_ok = True
                break
        if not qt_ok:
            _boot_log("WARNING: no PySide6 plugins folder found")

        _boot_log("boot: runtime hook finished")
except Exception:
    _boot_log("boot: runtime hook CRASHED:\n" + traceback.format_exc())
    raise
