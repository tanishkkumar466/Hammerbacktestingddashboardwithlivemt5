# Hammer Candle Backtest Dashboard — Delivery Summary

> **Share with client:** Edit and send **`Client_Delivery_Summary.txt`** (plain text — opens in Word, Google Docs, email). This `.md` file is the same content in Markdown for repo viewing.

**Project:** Hammer / Doji Candle Backtest Dashboard (XAUUSD) + Live Trading (MT5)  
**Version:** 1.2 (session analytics, fixed SL, expanded run history)  
**Document date:** 20 August 2026  

**Prepared for:** _______________________________  

**Prepared by:** _______________________________  

---------------------------------------

## 1. Executive summary

This document summarizes the **Hammer Candle Backtest Dashboard** delivered for candlestick strategy research on historical XAUUSD data, plus an integrated **Live Trading** module for MetaTrader 5 (MT5).

The application provides a modern desktop interface (PySide6/Qt), live visual previews of pattern rules, configurable backtests, metrics and charts, run history aligned with your Strategy Configurator workbook schema, optional packaging as a Windows executable, and **live signal polling / optional order execution** using the **same strategy and indicator settings** as backtest.

Work completed across phases includes **full Doji support**, **hammer type and direction controls**, **SuperTrend and VWAP indicators** (extensible), **results UX** (colors, trades, compare, export), **optional JSON presets**, and **live trading** with **client-facing risk limits**, **dry-run default**, and **stability-focused threading** on the live loop.

Saving a “preset” is **optional** (Run Settings). Live orders use **Safety limits on the Live Trading panel only**, not backtest commission/slippage fields.

**Platform note:** Backtest and UI run on **macOS and Windows**. **MT5 live connection and orders require Windows** with the MT5 terminal and `MetaTrader5` Python package installed.

---------------------------------------

## 2. Scope delivered

### 2.1 Core application (foundation)

| Area | Delivered |
|------|-----------|
| Desktop dashboard | `dashboard.py` — parameter tabs, dockable panels, menus (View, Live, Run, Help) |
| Hammer engine | `logic.py` — shape rules, entry/SL/target, direction, classic vs inverted hammer |
| Doji engine | `doji_logic.py` — styles, direction modes |
| Backtest engine | `backtest.py` — multi-timeframe runs, three exit models, CSV exports, indicator filters |
| Charts | `plotting.py` — equity and summary plots |
| Live trading | `live.py` — poll loop, safety gates, optional orders; `broker.py` — MT5 wrapper |
| CLI entry | `main.py` — gui / backtest / plots modes |
| Dependencies | `requirements.txt` (MetaTrader5 / Ray noted as optional for live) |
| Windows packaging | `.github/workflows/build-windows-exe.yml` + packaging notes; live modules included in build |
| Data layout | Historical CSVs under `data/<symbol>/<timeframe>/` |
| Client template | `template/Client_Delivery_Summary.md` (this document) |

### 2.2 Pattern & preview (UI)

- **Candle pattern selector:** Hammer and Doji with pattern-specific fields shown/hidden automatically.
- **Pattern preview — Signal Shape:** Live candle drawing from body/wick tolerance settings; loosest/tightest valid shapes.
- **Pattern preview — Pattern in Context:** Illustrative chart per timeframe with entry, SL, TP, support/resistance; follows Doji style when Doji is selected.
- **Hammer — Direction tab:** Green/red candle direction; classic vs inverted hammer; allow/block BUY/SELL per type.
- **Doji — Doji Direction tab:** Doji style and direction mode (including **CANDLE_COLOR**); per-side enable flags.
- **Body / Wicks tabs:** Separate hammer and doji ratio fields with tooltips.

### 2.3 Timeframes & run configuration

- **Combined Timeframes tab:** Per-timeframe **Include in run**, **RR multiple**, **Max SL ($)**, **signal risk filters**, and **trading session checkboxes** (Asian / London / US).
- **Run Settings tab:** Symbol, data folder, date range, position sizing, overlap, commission, slippage, etc.
- **Optional saved parameter sets:** Save/load/delete JSON in `presets/` — **not required** to run a backtest or to start live (but recommended to document a approved setup).

### 2.4 Indicators (SuperTrend & VWAP)

- **Extensible package:** `indicators/` — config, SuperTrend, VWAP, registry, filter pipeline.
- **Indicators tab:** Dropdown add; per-indicator settings; **Combine filters (ALL / ANY)** when 2+ indicators active.
- **Documented filter rules:** View rules / open `indicators/filter.py`.
- **Backtest & live:** Filters apply to **Hammer and Doji** in backtest and in the **live signal loop** when indicators are enabled.

### 2.5 Results & reporting

- **Dock:** **Results** panel (F8) — separate from Live Trading.
- **Metrics:** Overall, By Timeframe, By Direction, **By Session**, **By Year**, **By Month** — **color key:** green = BUY, red = SELL, blue = candle-bias exit model.
- **Session analytics:** Trades grouped into **Asian / London / US** using **IC Markets MT5 server time** (GMT+2 winter / GMT+3 US daylight saving — same as CSV data). Filter which sessions to include on the **Timeframes** tab.
- **Fixed stop loss:** Optional **fixed distance from entry** (not only candle low/high) on Entry/Exit tab — Hammer, Hammer with candles, and Doji.
- **Trades tab:** Trade list from last run (UI row cap; full `trade_ledger.csv` on disk).
- **Charts tab:** Thumbnails with click-to-zoom.
- **History tab:** SQLite `run_history.db` with **expanded metrics** and **Backtest_Metrics_Breakdown** (session, timeframe, direction, year, month); double-click to open run folder; **Export to Excel**.
- **Compare tab:** Two Strategy IDs side-by-side — **overall by exit model** plus **by session (worst case)** for Asian / London / US.
- **Toolbar:** Open output folder · Open ledger · Export last run metrics · Open charts folder.

### 2.6 Live trading (MT5)

- **Dock:** **Live Trading** panel (F9) — structured layout: strategy summary, MT5 connection, execution, safety limits, activity log.
- **Strategy alignment:** Pattern selector (Hammer/Doji) synced with Backtest Parameters; summary shows preset, indicators, chart symbol/timeframe.
- **Same rules as backtest:** Uses current dashboard config (or loaded preset) for hammer/doji logic and indicator stack.
- **MT5 connection:** Terminal path (optional), login, password, server; connect/disconnect; demo and **live/real accounts supported**.
- **Execution fields:** Symbol, timeframe, lot size, magic number, max open positions, poll interval.
- **Safety limits (enforced on real orders only from this panel):**
  - Max trades per day  
  - Max daily loss ($) — **required before live orders** on real accounts  
  - Cooldown (minutes between trades)  
  - Max spread (points)  
  - Max lot cap  
  - Optional “restrict to demo accounts only”  
- **Dry run:** Default **on** — logs signals, does not send orders.
- **Menu — Live → Performance** (advanced, for operator use): dry run toggle, thread pool for signal CPU, optional Ray if installed.
- **Worker thread:** Live poll loop runs on a **background QThread** so the dashboard UI stays responsive; heartbeat monitoring; consecutive-error stop to avoid runaway loops.
- **Confirmations:** Warning when turning off dry run; risk validation before sending orders to a live account.

**Client environment for live:**

| Requirement | Notes |
|-------------|--------|
| OS | **Windows** for MT5 API |
| MT5 terminal | Installed and logged in (demo or live) |
| Python package | `pip install MetaTrader5` |
| macOS | Use for backtest/UI development only; connect/start live on Windows |

### 2.7 Desktop layout & stability

- **Dockable panels:** Backtest Parameters (top), Pattern & Preview (left), Results (F3/F4/F8), Live Trading (F9).
- **Arrange panels:** Drag title bars to dock side-by-side or **tab** panels together; drag splitters to resize.
- **F2 — Reset layout:** Restores default panel arrangement if layout looks wrong.
- **Saved window size:** Main window geometry remembered between sessions.
- **macOS:** Floating panels in separate windows **disabled** to prevent Qt/AppKit crashes with results tables; tabbing and docking **inside** the main window remain available.
- **Windows:** Full dock behavior including floatable panels (as in standard Qt apps).

### 2.8 Packaging & operations

- Path anchoring: `data/`, `output/`, `plots/`, `presets/`, `run_history.db` next to app/exe.
- GitHub Actions: Windows exe build includes dashboard, live, broker modules.
- Run outputs unchanged for backtest; live writes to MT5 and **Activity log** in the UI.

---------------------------------------

## 3. Module overview

### Module 1 — Strategy configuration

- Hammer and Doji parameters across Body, Wicks, Direction, Entry/Exit, Risk, Timeframes, Run Settings.
- Live preview; optional JSON presets.

### Module 2 — Backtest execution

- Multi-timeframe batch runs; three exit models; indicator filters.

### Module 3 — Results & history

- Metrics, trades, charts, history, compare, Excel export, row colors.

### Module 4 — Indicators (extensibility)

- Registry-driven catalog; SuperTrend & VWAP shipped; filter rules documented.

### Module 5 — Live trading (MT5)

- Connect, dry-run/live modes, safety limits, background worker, same strategy as backtest.

---------------------------------------

## 4. Inputs

- Historical OHLC(V) CSVs under `data/`.
- Dashboard parameters (or optional preset).
- **Live:** MT5 credentials, symbol/timeframe aligned with strategy, safety limits set on Live panel.

---------------------------------------

## 5. Outputs

- Backtest: per-run folder under `output/`, plots under `plots/`, `run_history.db`, optional Excel.
- Presets: `presets/*.json`.
- Live: MT5 orders (when dry run off and safety checks pass); in-app **Activity log**.

---------------------------------------

## 6. Technologies

| Layer | Technology |
|-------|------------|
| Desktop UI | Python 3.11+, PySide6 (Qt) |
| Backtest / data | Polars, NumPy |
| Charts | Matplotlib |
| History / export | SQLite, openpyxl |
| Live (Windows) | MetaTrader5 Python API, custom `broker.py` / `live.py` |
| Optional live CPU | Thread pool (default); Ray optional via menu |
| Packaging | PyInstaller (Windows via GitHub Actions) |

**Not included in this delivery:** mobile app, cloud hosting, built-in parameter optimization sweeps, guaranteed SLA on broker/MT5 uptime, or financial advice / live account management as a managed service.

---------------------------------------

## 7. Deliverables checklist

| Deliverable | Status |
|-------------|--------|
| Source: dashboard, logic, doji_logic, backtest, plotting, indicators | ✓ |
| Source: `broker.py`, `live.py` | ✓ |
| Sample data layout (`data/XAUUSD/…`) | ✓ |
| Requirements file | ✓ |
| Windows exe build workflow + packaging guide | ✓ |
| Client delivery summary (this document) | ✓ |
| Example presets (`presets/`) | ✓ (optional use) |
| Installer (.exe from CI) | ✓ (artifact via GitHub Actions) |

---------------------------------------

## 8. How the client uses the application

### Backtest workflow

1. Launch: `python dashboard.py` or packaged `.exe`.
2. Choose **Hammer** or **Doji**, adjust tabs, **Run Backtest** (F5).
3. Review **Results** → Metrics, Trades, Charts; **Compare** past runs if needed.
4. Optionally save settings under **Run Settings** → preset file.

### Live workflow (Windows + MT5)

1. Tune strategy on backtest; optionally **load preset**.
2. Open **Live Trading** (F9): confirm **Strategy** summary (pattern, indicators, symbol/timeframe).
3. Set **Safety limits** (daily loss, max trades, lot cap, etc.).
4. Leave **Live → Performance → Dry run** **on** until signals in **Activity log** look correct.
5. **Connect MT5** → **Start live** → monitor log; turn off dry run only when ready for real/demo orders (confirmations apply).

### Layout tips

- **F2** — reset panel layout.  
- Drag panel **titles** to tab or dock side-by-side.  
- **F1** — keyboard shortcuts help.

---------------------------------------

## 9. Support (as per proposal template)

**Included:** _____ days bug fix / minor clarification support from delivery date: __________  

**Excluded:** New features after sign-off unless covered by a change order.

---------------------------------------

## 10. Cost & payment

*(Fill in before presenting to client.)*

| Item | Amount |
|------|--------|
| **Total project cost** | ________________ |
| Advance (on approval) | ________________ |
| Milestone 1 — Core dashboard + hammer/doji backtest | ________________ |
| Milestone 2 — Indicators, results UX, history/compare/presets | ________________ |
| Milestone 3 — Live trading module (MT5, safety, UI integration) | ________________ |
| Final payment (on delivery sign-off) | ________________ |
| **Currency** | ________________ |
| **Payment terms / notes** | ________________ |

---------------------------------------

## 11. Change log (enhancements in this build)

### Backtest & UI (earlier phase)

1. Doji fully integrated in UI and preview.  
2. Hammer classic/inverted direction gating.  
3. Doji direction: CANDLE_COLOR + green/red mapping.  
4. Indicators: SuperTrend, VWAP, dropdown, combine mode, filter docs.  
5. Indicator filters for Doji backtests.  
6. Combined Timeframes tab (include + RR + max SL).  
7. Results color coding; Trades tab; export/open shortcuts.  
8. Run comparison (two Strategy IDs).  
9. Optional presets (Run Settings).  
10. Packaging, CI Windows exe, startup stability improvements.

### Live trading & operations (this phase)

11. **`broker.py` / `live.py`** — MT5 connect, rates, positions, market orders with SL/TP.  
12. **Live Trading dock** — strategy selector + summary, MT5 credentials, execution, safety limits, log.  
13. **Same config as backtest** — hammer/doji + indicators; preset load refreshes live summary.  
14. **Safety limits** — daily loss/trades, spread, cooldown, lot cap; demo-only optional; validation before real orders.  
15. **Dry run default**; advanced toggles under **Live → Performance** menu.  
16. **Background QThread** live worker; error budget; optional thread pool / Ray for signal CPU.  
17. **Results vs Live** — separate docks (F8 / F9).  
18. **Live panel UX** — structured scroll layout (strategy / MT5 / execution / safety / log).  
19. **Layout tools** — F2 reset, tabbed docking; macOS crash mitigation (no float windows).  
20. **Client document** — this template updated for live scope and platform notes.

### Analytics & research (v1.2)

21. **Trading sessions** — Asian / London / US breakdown in Results, charts, CSV, and run history (IC Markets server clock).  
22. **Session backtest filter** — Include/exclude sessions on Timeframes tab before running.  
23. **Fixed stop loss from entry** — `FIXED_FROM_ENTRY` mode alongside candle-extreme SL.  
24. **Metrics tabs** — By Year and By Month added to Results UI.  
25. **Compare by session** — History compare shows worst-case session rows for Run A vs Run B.  
26. **Run history DB** — Full metrics on `Backtest_Results` + granular `Backtest_Metrics_Breakdown` table.  
27. **Hammer with candles** — Context pattern (lookback, separate BUY/SELL wick rules) fully integrated in UI and backtest.

---------------------------------------

## 12. Client approval

By signing below, the client confirms receipt of the deliverables described in this document and acceptance of scope as delivered (subject to any agreed support period).

**Client name:** _______________________________  

**Signature:** _______________________________  

**Date:** _______________________________  

**Developer name:** _______________________________  

**Signature:** _______________________________  

**Date:** _______________________________  

---------------------------------------

*End of document*
