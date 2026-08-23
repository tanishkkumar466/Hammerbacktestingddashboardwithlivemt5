"""
PyInstaller runtime hook — run before main.py in the frozen exe.
Ensures stdlib extension modules (sqlite3) resolve on Windows one-file builds.
"""
import os
import sys

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    meipass = sys._MEIPASS
    if meipass not in sys.path:
        sys.path.insert(0, meipass)
    # Help Windows find bundled .pyd / .dll next to extracted bundle
    os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")
