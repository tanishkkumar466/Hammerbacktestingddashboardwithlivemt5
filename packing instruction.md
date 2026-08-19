# Packaging HammerCandleBacktestDashboard as a Windows .exe

## The one thing that matters most
PyInstaller does **not** cross-compile. Building on macOS produces a Mac
app; building on Windows produces a `.exe`. Since your client needs
Windows and you're developing on a Mac, you have two options:

- **Option A (recommended): GitHub Actions** -- free, no Windows machine
  needed, builds on a real Windows VM in the cloud automatically.
- **Option B: a real or virtual Windows machine** -- if you have access
  to one (a PC, a Windows VM via Parallels/VMware, or a rented cloud
  Windows instance), you can run PyInstaller directly.

Both produce the exact same kind of `.exe`. Pick whichever is easier
for you to set up.

---

## Option A: GitHub Actions (no Windows machine needed)

1. Put `dashboard.py` (Qt UI entry point), `logic.py`, `doji_logic.py`,
   `hammer_context_logic.py`, `backtest.py`, `plotting.py`, `live.py`,
   `broker.py`, `live_journal.py`, `indicators/`, and `main.py` in a folder
   together (leave `fetch.py` out if you want — live uses `broker.py`).
2. Copy the `.github/workflows/build-windows-exe.yml` file and
   `HammerCandleBacktestDashboard.spec` (included alongside this guide)
   into that same folder, keeping the `.github/workflows/` path structure.
   Put your Windows icon at **`logo.ico`** in the repo root (or keep
   `app_icon.ico` as fallback). Both are bundled into the exe.
3. Create a new GitHub repository (private is fine -- Actions works
   the same either way) and push this folder to it:
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin <your-repo-url>
   git push -u origin main
   ```
4. On GitHub, open the repo -> **Actions** tab -> **Build Windows EXE**
   -> **Run workflow**.
5. Wait a few minutes. Open the finished run, scroll down to
   **Artifacts**, download **HammerCandleBacktestDashboard-windows**.
   Unzip it -- that's your `.exe`.

From then on, every time you push a change, a fresh `.exe` builds
automatically -- you don't have to repeat these steps.

---

## Option B: Building directly on a Windows machine

On the Windows machine, with Python 3.11+ installed:

```
pip install PySide6 matplotlib polars numpy pillow openpyxl pyinstaller

pyinstaller --onefile --windowed ^
  --name "HammerCandleBacktestDashboard" ^
  --collect-all PySide6 ^
  --collect-data matplotlib ^
  --hidden-import openpyxl ^
  --hidden-import openpyxl.styles ^
  dashboard.py
```

(That `^` is a line-continuation for Windows `cmd`. In PowerShell use
a backtick `` ` `` instead, or just put it all on one line.)

The finished `.exe` will be in `dist\HammerCandleBacktestDashboard.exe`.

### What each flag does
- `--onefile` -- bundles everything into a single `.exe`.
- `--windowed` -- hides the console/terminal window, so it looks like a
  normal desktop app instead of a script.
- `--collect-all PySide6` -- makes sure Qt's platform plugins are
  included. Without this, the built exe can fail to open at all with
  an error like *"could not find or load the Qt platform plugin
  windows"*.
- `--collect-data matplotlib` -- bundles matplotlib's font/data files,
  needed for the chart images to render correctly once frozen.
- `--hidden-import openpyxl` / `openpyxl.styles` -- the Excel export
  feature does `import openpyxl` inside a function rather than at the
  top of the file. PyInstaller's scanner usually catches this on its
  own, but this makes sure it isn't missed.

### Optional: a custom icon
Add `--icon=your_icon.ico` to the command (needs to be a `.ico` file,
not `.png` -- there are free online PNG-to-ICO converters if you only
have a logo image).

---

## What to actually hand the client

Create a folder like this and zip it:
```
HammerCandleBacktestDashboard/
    HammerCandleBacktestDashboard.exe
    data/              <- their price CSVs go here
```
`output/` and `plots/` folders get created automatically next to the
exe the first time it runs -- no need to include them.

## Before you send it: test on a genuinely clean machine
Run the built `.exe` on a Windows machine (or fresh VM) that has
**never had Python installed** -- this is the only way to be sure
nothing is secretly depending on your dev machine's Python. Check that:
- The window opens without a console flashing up behind it
- Run Backtest completes end-to-end with real data
- Charts render in the Charts tab
- A run saves to the History tab, and running the same config twice
  correctly skips as a duplicate
- Export to Excel produces a working `.xlsx`

## One thing to warn the client about in advance
Since this `.exe` isn't code-signed, Windows will very likely show a
**"Windows protected your PC" SmartScreen warning** the first time it's
opened -- this is completely normal for any unsigned app, not a sign
of a problem. The client clicks **"More info" -> "Run anyway"** once,
and it won't ask again on that machine. Worth mentioning to them ahead
of time so it doesn't look alarming. (A paid code-signing certificate
removes this warning entirely, but that's a separate, optional step
for later -- not needed to hand off a working build now.)

## One performance note, also worth setting expectations on
`--onefile` mode unpacks the whole bundle to a temp folder every time
the exe launches, and this dependency stack (PySide6 + matplotlib +
polars + numpy) is heavy -- expect the `.exe` itself to be roughly
150-300MB, and a few seconds of blank pause before the window appears
each time it's opened. That's normal for a bundle this size, not a bug.