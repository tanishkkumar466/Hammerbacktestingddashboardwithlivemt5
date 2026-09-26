# Packaging HammerCandleBacktestDashboard as a Windows .exe

## Current release: v1.0.35

Bump `version.py` → commit → tag `v1.0.35` → push tag. GitHub Actions
(`release-windows-exe.yml`) builds the Windows package on that tag.

### v1.0.35 — what changed (client QA)

**Must verify on Windows:**
- **API Accounts:** the C++ MT5 engine is bundled and starts by itself — no manual
  `hammer_mt5_engine.exe`. Start Algo on a demo account; per-account Order type
  (default Market) is respected.
- **Entry Offset warning:** a preset with Entry Offset ≠ 0 and Order type *Market*
  shows a warning on Live Start and API Start (Market ignores the offset).
- **Telegram alerts:** new layout — strategy first (HWC / HWC 35%), preset name,
  bold Entry / SL / TP, details below, no emojis.
- **Session restore:** run a backtest, close and reopen the app — parameters, open tabs
  and the last run's results come back.
- **HWC / HWC 35% min-wick filter:** off by default; when on, small wicks are rejected
  (backtest and Live agree).
- **Live history:** history dialog explains each signal with the settings row that built it.

Tests: `pytest tests` (458) + `python3.11 scripts/mutation_check.py --jobs 6`
(74/74 injected bugs caught).

### v1.0.34 — what changed (client QA)

**Must verify on Live (all patterns: Hammer, Doji, HWC, HWC 35%):**
- SL stays on candle **low** (BUY) / **high** (SELL) ± buffer
- Buffer **widens** the stop (BUY: low − flat; SELL: high + flat)
- Entry offset may move entry; it must **not** slide/shrink SL on fill
- Live log: `SL locked at … (candle low/high ± buffer) — not slid with fill`

Also good to smoke:
- Fetch while Live is running (must not kill Live)
- HWC vs HWC 35% are separate (pullback only on 35%)
- Live audit CSV (executed / skipped / errors + reason)

**Do not QA SL on old 1.0.33 builds** — that release still slid SL with fill.

---

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

1. Keep the app sources in the repo. Pattern engines live under
   `strategy/` (`logic.py`, `doji_logic.py`, `hammer_context_core.py`,
   `hammer_with_candles_logic.py`, `hammer_with_candles_35_logic.py`).
   Root-level `logic.py` / `doji_logic.py` / `hammer_context_logic.py`
   (and HWC shims) are thin import compatibility shims — still required
   for the frozen exe. Also include `dashboard.py`, `backtest.py`,
   `plotting.py`, `live.py`, `broker.py`, `live_journal.py`,
   `indicators/`, `notification/`, `update/`, and `main.py`.
2. Copy the `.github/workflows/build-windows-exe.yml` file and
   `HammerCandleBacktestDashboard.spec` (included alongside this guide)
   into that same folder, keeping the `.github/workflows/` path structure.
   Put your Windows icon at **`logo.ico`** in the repo root (or keep
   `app_icon.ico` as fallback). Both are bundled into the exe.
   For client releases, prefer tagging `v*` so
   `.github/workflows/release-windows-exe.yml` attaches the artifact to
   the GitHub Release (updater picks that up).
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
   Or for a release: push tag `v1.0.35` (must match `version.py`) and wait
   for **Release Windows EXE**.
5. Wait a few minutes. Open the finished run, scroll down to
   **Artifacts**, download **HammerCandleBacktestDashboard-windows**.
   Unzip it -- that's your `.exe`. For tagged releases, also check the
   GitHub **Releases** page for the stub package zip.

From then on, every time you push a change, a fresh `.exe` builds
automatically -- you don't have to repeat these steps.

---

## Option B: Building directly on a Windows machine

On the Windows machine, with Python 3.11+ installed:

```
pip install -r requirements-build.txt

pyinstaller --noconfirm --clean HammerCandleBacktestDashboard.spec
```

Prefer the `.spec` (bundles `strategy/`, Ray, MT5, stub migration). Ad-hoc
one-liners can miss hidden imports.

The finished runtime will be under `dist\` (see the release workflow for
the stub + `app/HammerRuntime.exe` layout handed to clients).

### Optional: a custom icon
Add `--icon=your_icon.ico` to the command (needs to be a `.ico` file,
not `.png` -- there are free online PNG-to-ICO converters if you only
have a logo image). The release `.spec` already picks `logo.ico` /
`app_icon.ico` when present.

---

## What to actually hand the client

Create a folder like this and zip it:
```
HammerCandleBacktestDashboard/
    HammerCandleBacktestDashboard.exe
    data/              <- their price CSVs go here
```
Prefer the release artifact layout (stub + `app/HammerRuntime.exe`) when
shipping updates via the in-app updater.

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
- **Live:** one paper/tiny-lot trade per pattern; confirm SL = candle
  extreme ± buffer and does not shrink when entry offset / market fill
  differ from strategy entry

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
