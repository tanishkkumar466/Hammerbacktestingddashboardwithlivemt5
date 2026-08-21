# Hammer Candle Backtest Dashboard — Client Delivery Summary

> **Share with client:** Send **`Client_Delivery_Summary.txt`** (plain text — opens in Word, Google Docs, email). This `.md` file mirrors that document for the repo.

**Project:** Hammer Candle Backtest Dashboard (XAUUSD) + Live Trading (MT5)  
**Version:** 1.2  
**Build focus:** Session analytics · Fixed stop loss · Expanded run history · Hammer with candles  
**Document date:** 20 August 2026  

**Prepared for:** _______________________________  

**Prepared by:** _______________________________  

---------------------------------------

## 1. Executive summary

This document confirms delivery of the **Hammer Candle Backtest Dashboard** for candlestick strategy research on historical XAUUSD data, plus an integrated **Live Trading** module for MetaTrader 5 (MT5).

**What you receive:**

- Desktop application (PySide6 / Qt) with parameter tabs, live pattern preview, multi-timeframe backtests, metrics, charts, and run history  
- Three pattern engines: **Hammer**, **Hammer with candles**, and **Doji**  
- SuperTrend and VWAP indicator filters (same rules in backtest and live)  
- Results broken down by timeframe, direction, **session**, year, and month  
- Optional Windows executable via GitHub Actions packaging  
- Live MT5 signal polling and optional order execution using the **same strategy settings** as backtest (dry-run default and safety limits)

Saving a preset is **optional** (Run Settings). Live order risk controls live on the **Live Trading panel only** — not on backtest commission / slippage fields.

**Platform note:** Backtest and UI run on **macOS and Windows**. **MT5 live connection and orders require Windows** with the MT5 terminal and `MetaTrader5` Python package.

---------------------------------------

## 2. Scope delivered

### 2.1 Core application

| Area | Delivered |
|------|-----------|
| Desktop dashboard | Parameter tabs, dockable panels, menus (View, Live, Run, Help) |
| Hammer engine | Shape rules, entry / SL / target, classic vs inverted, direction matrix |
| Hammer with candles | Prior-N context, separate BUY/SELL body & wick rules, wick on/off |
| Doji engine | Styles and direction modes (including CANDLE_COLOR) |
| Backtest engine | Multi-timeframe runs, three exit models, session filter, CSV exports, indicator filters |
| Charts | Equity, drawdown, session, timeframe, and summary plots |
| Live trading | Poll loop, safety gates, optional orders (MT5) |
| CLI entry | GUI / backtest / plots modes |
| Dependencies | `requirements.txt` |
| Windows packaging | GitHub Actions workflow + packaging notes |
| Data layout | Historical CSVs under `data/<symbol>/<timeframe>/` |
| Client document | This delivery summary |

### 2.2 Patterns and preview

- Pattern selector: **Hammer · Hammer with candles · Doji** (fields show/hide automatically)  
- Signal Shape preview; Pattern in Context charts with entry, SL, TP  
- Hammer Direction tab; Hammer with candles Context tab (lookback N, separate BUY/SELL, optional wick)  
- Doji Direction tab; Body / Wicks / Entry-Exit tabs with tooltips  

### 2.3 Timeframes and run settings

- **Timeframes tab:** include TF, RR, Max SL ($), signal risk filters, **session checkboxes** (Asian / London / US — backtest filter; not live yet)  
- **Run Settings:** symbol, data folder, date range, sizing, overlap, commission, slippage  
- Optional JSON presets in `presets/`  

### 2.4 Indicators (SuperTrend and VWAP)

- Extensible registry; Indicators tab; ALL / ANY combine mode  
- Same filters in backtest and live when enabled  

### 2.5 Results and reporting

- Metrics: Overall · By Timeframe · By Direction · **By Session** · **By Year** · **By Month**  
- Colour key: green = BUY · red = SELL · blue = candle-bias  
- Three exit models: Best case · Candle bias · Worst case  
- **Sessions (IC Markets MT5 server time):** Asian 00:00–07:59 · London 08:00–15:59 · US 16:00–23:59 (GMT+2 / GMT+3)  
- Session Net PnL / wins / losses **add to Overall**; session Max DD / Return % are **as if that session alone**  
- Fixed stop loss from entry (optional) alongside candle-extreme SL  
- Trades, Charts (incl. session), History (`run_history.db` + breakdown), Compare (overall + by session), Excel export  

### 2.6 Live trading (MT5)

- Live panel (F9); pattern synced with backtest (Hammer / Hammer with candles / Doji)  
- Safety limits: max trades/day, max daily loss ($), cooldown, spread, lot cap, optional demo-only  
- Dry run default **ON**; background worker; confirmations before live orders  

| Requirement | Notes |
|-------------|--------|
| OS | **Windows** for MT5 API |
| MT5 terminal | Installed and logged in |
| Python package | `pip install MetaTrader5` |
| macOS | Backtest / UI only |

### 2.7 Layout and packaging

- Dockable panels; F2 reset layout; path anchoring for `data/`, `output/`, `plots/`, `presets/`, `run_history.db`  
- Windows exe via GitHub Actions  

---------------------------------------

## 3. Module overview

1. **Strategy configuration** — Hammer / Hammer with candles / Doji + presets  
2. **Backtest** — multi-TF, three exit models, session filter, indicators  
3. **Results & history** — metrics, charts, compare, Excel  
4. **Indicators** — SuperTrend & VWAP  
5. **Live trading** — MT5, dry-run, safety limits  

---------------------------------------

## 4–5. Inputs and outputs

**Inputs:** OHLC CSVs under `data/` (IC Markets / MT5 server time); dashboard params or preset; live MT5 credentials + safety limits.  

**Outputs:** `output/<run>/` (ledger + summaries including session), `plots/`, `run_history.db`, `presets/*.json`, live Activity log / MT5 orders.

---------------------------------------

## 6. Technologies

Python 3.11+, PySide6, Polars, NumPy, Matplotlib, SQLite, openpyxl, MetaTrader5 (Windows), PyInstaller.

**Not included:** mobile/cloud, UI optimization sweeps, broker uptime SLA, managed live trading, **live session filter** (backtest only for now).

---------------------------------------

## 7. Deliverables checklist

| Deliverable | Status |
|-------------|--------|
| Source: dashboard, logic, hammer_context_logic, doji_logic, backtest, plotting, sessions, indicators | Done |
| Source: broker.py, live.py, live_journal.py | Done |
| Sample data layout (`data/XAUUSD/…`) | Done |
| Requirements + Windows exe workflow | Done |
| Client delivery summary | Done |
| Example presets | Done |
| Installer (.exe from CI) | Done (GitHub artifact) |

---------------------------------------

## 8. How to use

**Backtest:** Launch → choose Hammer / Hammer with candles / Doji → set Timeframes & sessions → Run (F5) → review Metrics / Trades / Charts / History / Compare → optional preset.  

**Live (Windows):** Tune on backtest → Live (F9) → safety limits → dry run ON → Connect MT5 → Start live → disable dry run only when ready.  

**Shortcuts:** F1 help · F2 reset layout · F5 run · F8 Results · F9 Live.

---------------------------------------

## 9–10. Support and cost

**Support included:** _____ days bug-fix / clarification from delivery date: __________  
**Excluded:** New features after sign-off unless change-ordered.

*(Fill cost / payment table in the `.txt` before presenting.)*

---------------------------------------

## 11. Change log (v1.2 highlights)

21–29: Session analytics & filter · Fixed SL from entry · By Year / By Month · Compare by session · Expanded run history DB · Hammer with candles · Metrics integrity tests · Risk controls on Timeframes · PyInstaller `sessions` module.

Full numbered changelog is in **`Client_Delivery_Summary.txt`**.

---------------------------------------

## 12. Client approval

**Client name:** _______________________________  
**Signature / Date:** _______________________________  

**Developer name:** _______________________________  
**Signature / Date:** _______________________________  

---------------------------------------

*End of document — client handoff copy: `Client_Delivery_Summary.txt`*
