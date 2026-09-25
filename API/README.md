# API / headless accounts

Python UI (`API Accounts` dock) manages the table.  
**C++ engine** (`API/engine`) launches hidden, isolated MT5 processes — not Python.

## Why C++ (not Python) for the engine

Spawning / tracking many `terminal64.exe` instances, registry lookup, and
`CREATE_NO_WINDOW` belong in a small native service. Python stays on the UI
thread and only HTTP-calls `127.0.0.1:17101`.

## Build (Windows + Visual Studio)

```bat
cd API\engine
build.bat
```

Or:

```bat
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
build\Release\hammer_mt5_engine.exe
```

Health check: http://127.0.0.1:17101/health

## Engine behaviour

1. **Path discovery** — registry + common broker folders; UI can **Locate…** / **Auto-find**
2. **Per-account portable copy** under `%LOCALAPPDATA%\Hammer\API\terminals\<id>\`
3. **Launch** with `/portable` + `CREATE_NO_WINDOW` (no visible MT5 UI)
4. **Disconnect** terminates that PID only (other accounts stay up)

## Any broker

Rows are normal broker credentials (login + server). There is **no official
MetaQuotes retail cloud API** — Cloud/vendor options were removed from the UI.
The local API service attaches accounts without opening broker charts.

## Algo desk (C++ terminals + parallel strategy workers)

- Save a preset from Parameters → JSON gets `saved_at` + `enabled_timeframes`
- **Assign strategy** on an API account → `API/bindings.json` (account → preset + TF)
- **Start trading** →
  1. C++ `POST /fleet/connect` — staggered `/portable` terminals (isolated folders)
  2. One strategy **process per account** (parallel) loads that preset and trades via MetaTrader5 Python against the portable path
- **Stop trading** stops strategy workers; Disconnect kills C++ terminal PIDs
- Live dock is unchanged

Dry-run is on by default per binding (uncheck for real orders).

## Honest limits

- MetaQuotes still requires a terminal binary; we hide it, we do not remove it.
- CLI `/login` `/password` `/server` are best-effort; full trading attach may still use MetaTrader5 Python or MtApi/NJ4X against the portable path later.
- Engine is **Windows-only**. macOS/Linux UI can edit the table; Connect needs a Windows host running `hammer_mt5_engine.exe`.

## Files

| Path | Role |
|------|------|
| `accounts.py` | Table rows → `API/accounts.json` |
| `service.py` | Python HTTP client to C++ engine |
| `engine/` | C++ `hammer_mt5_engine` source |
