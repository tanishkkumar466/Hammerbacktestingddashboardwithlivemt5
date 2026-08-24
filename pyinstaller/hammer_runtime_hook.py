"""
PyInstaller runtime hook — run before main.py in the frozen exe.

- Puts _MEIPASS on PATH and registers every folder that contains .dll/.pyd
- Points Qt at bundled platform plugins (qwindows.dll)
"""
import os
import sys


def _register_dll_dir(path: str, seen: set[str]) -> None:
    if not path or path in seen or not os.path.isdir(path):
        return
    seen.add(path)
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(path)
        except OSError:
            pass


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS
    if meipass not in sys.path:
        sys.path.insert(0, meipass)

    os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")

    _seen_dirs: set[str] = set()
    _register_dll_dir(meipass, _seen_dirs)

    # Register every folder under _MEIPASS that contains native libraries
    try:
        for dirpath, dirnames, filenames in os.walk(meipass):
            if any(
                name.lower().endswith(ext)
                for name in filenames
                for ext in (".dll", ".pyd", ".so")
            ):
                _register_dll_dir(dirpath, _seen_dirs)
    except OSError:
        pass

    # PySide6 platform plugin (qwindows.dll) — required for QApplication
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
            break
