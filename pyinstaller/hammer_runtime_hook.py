"""
PyInstaller runtime hook — run before main.py in the frozen exe.

- Puts _MEIPASS on PATH / DLL search path (sqlite3, polars, pyarrow, etc.)
- Points Qt at bundled platform plugins (qwindows.dll) — without this the
  windowed exe often exits silently on Windows.
"""
import os
import sys

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS
    if meipass not in sys.path:
        sys.path.insert(0, meipass)

    os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")

    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(meipass)
        except OSError:
            pass
        for sub in (
            os.path.join(meipass, "PySide6"),
            os.path.join(meipass, "PySide6", "plugins"),
            os.path.join(meipass, "PySide6", "plugins", "platforms"),
            os.path.join(meipass, "shiboken6"),
        ):
            if os.path.isdir(sub):
                try:
                    os.add_dll_directory(sub)
                except OSError:
                    pass

    # PySide6 platform plugin (qwindows.dll) — required for QApplication to start
    for plugins in (
        os.path.join(meipass, "PySide6", "plugins"),
        os.path.join(meipass, "PySide6", "Qt6", "plugins"),
        os.path.join(meipass, "PySide6", "Qt", "plugins"),
    ):
        if os.path.isdir(plugins):
            os.environ["QT_PLUGIN_PATH"] = plugins
            platforms = os.path.join(plugins, "platforms")
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = platforms
            if (
                os.path.isdir(platforms)
                and sys.platform == "win32"
                and hasattr(os, "add_dll_directory")
            ):
                try:
                    os.add_dll_directory(platforms)
                except OSError:
                    pass
            break
