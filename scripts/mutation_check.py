"""
Mutation check — proves the test suite catches real bugs (not just "says OK").

How it works
  1. Copies the code (not data/, output/, plots/) into a temp folder.
  2. Injects ONE realistic bug at a time (wrong sign, off-by-one, wrong field,
     skipped filter, wrong pattern branch …).
  3. Runs the test suite against the broken copy.
  4. A bug the tests catch is "killed". A bug the tests miss "SURVIVED" — that is a
     hole in the tests.

Your working files are never modified.

Usage
  python3.11 scripts/mutation_check.py              # all mutants, 4 workers
  python3.11 scripts/mutation_check.py --jobs 6
  python3.11 scripts/mutation_check.py --only B1 L3 # selected mutants
  python3.11 scripts/mutation_check.py --list

Exit code 1 when any mutant survives or a mutation no longer applies to the code
(then update the `old` text below so the check keeps guarding that line).
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", "data", "output", "plots", "__pycache__", ".pytest_cache",
    "build", "dist", ".venv", "venv", "node_modules",
}
LINK_DIRS = ("data",)  # read-only inputs some tests use


@dataclass(frozen=True)
class Mutant:
    id: str
    area: str
    bug: str
    path: str
    old: str
    new: str


MUTANTS: List[Mutant] = [
    # ---------------------------------------------------------------- Hammer maths
    Mutant("L1", "Hammer", "entry offset subtracted instead of added",
           "strategy/logic.py", "    return base + offset\n", "    return base - offset\n"),
    Mutant("L2", "Hammer", "inverted row uses the classic entry offset",
           "strategy/logic.py",
           'offset = float(getattr(config, "inverted_entry_offset", config.entry_offset) or 0.0)',
           "offset = float(config.entry_offset or 0.0)"),
    Mutant("L3", "Hammer", "BUY SL buffer added above the low",
           "strategy/logic.py",
           "    if direction == TradeDirection.BUY:\n        return anchor - buffer_amount\n",
           "    if direction == TradeDirection.BUY:\n        return anchor + buffer_amount\n"),
    Mutant("L4", "Hammer", "target ignores the RR multiple",
           "strategy/logic.py",
           "    reward = risk * tf_setting.rr_multiple\n    if direction == TradeDirection.BUY:",
           "    reward = risk\n    if direction == TradeDirection.BUY:"),
    Mutant("L5", "Hammer", "max-SL risk limit never applied",
           "strategy/logic.py",
           "    elif config.enable_risk_limit and risk > tf_setting.max_sl_usd:",
           "    elif False and config.enable_risk_limit and risk > tf_setting.max_sl_usd:"),
    Mutant("L6", "Hammer", "fixed SL from entry on the wrong side for SELL",
           "strategy/logic.py",
           "            return base - dist\n        return base + dist\n",
           "            return base - dist\n        return base - dist\n"),
    Mutant("L7", "Hammer", "body % bound not checked",
           "strategy/logic.py",
           "    body_ok = body_lo <= body_pct <= body_hi\n", "    body_ok = True\n"),
    Mutant("L8", "Hammer", "Direction matrix ignored (SELL cells trade BUY)",
           "strategy/logic.py",
           "        if action == TradeAction.BUY:\n            direction = TradeDirection.BUY",
           "        if action in (TradeAction.BUY, TradeAction.SELL):\n            direction = TradeDirection.BUY"),
    Mutant("L10", "Hammer", "zero/negative risk setups not rejected",
           "strategy/logic.py",
           "    if config.reject_zero_or_negative_risk and risk <= 0:",
           "    if config.reject_zero_or_negative_risk and risk < -1e9:"),
    Mutant("L11", "Hammer", "entry rule HAMMER_HIGH uses the low",
           "strategy/logic.py",
           "    elif rule == EntryRule.HAMMER_HIGH:\n        base = hammer_candle.high",
           "    elif rule == EntryRule.HAMMER_HIGH:\n        base = hammer_candle.low"),
    # ---------------------------------------------------------------- Doji maths
    Mutant("D1", "Doji", "entry offset dropped",
           "strategy/doji_logic.py", "    return base + config.entry_offset\n", "    return base\n"),
    Mutant("D2", "Doji", "SELL SL anchored at the low",
           "strategy/doji_logic.py",
           "    anchor = signal_candle.low if direction == logic.TradeDirection.BUY else signal_candle.high",
           "    anchor = signal_candle.low"),
    Mutant("D3", "Doji", "target ignores the RR multiple",
           "strategy/doji_logic.py",
           "    reward = risk * tf_setting.rr_multiple\n", "    reward = risk\n"),
    Mutant("D4", "Doji", "last signal/entry pair never scanned",
           "strategy/doji_logic.py",
           "    for i in range(len(candles) - 1):", "    for i in range(len(candles) - 2):"),
    # ---------------------------------------------------------------- Hammer with candles (+35%)
    Mutant("H1", "HWC", "SELL row uses the BUY entry offset",
           "strategy/hammer_context_core.py",
           "        cfg.inverted_entry_offset = self.inverted_entry_offset\n",
           "        cfg.inverted_entry_offset = self.entry_offset\n"),
    Mutant("H2", "HWC", "SELL row uses the BUY flat SL buffer",
           "strategy/hammer_context_core.py",
           "        cfg.inverted_sl_buffer_flat = self.inverted_sl_buffer_flat\n",
           "        cfg.inverted_sl_buffer_flat = self.sl_buffer_flat\n"),
    Mutant("H3", "HWC 35%", "BUY pullback moves away from the SL",
           "strategy/hammer_context_core.py",
           "        return base - shift, dist\n", "        return base + shift, dist\n"),
    Mutant("H4", "HWC 35%", "TP rebuilt from the pullback fill (must stay at signal entry × RR)",
           "strategy/hammer_context_core.py",
           "        target = base_entry + reward\n    else:\n        target = base_entry - reward\n",
           "        target = entry_price + reward\n    else:\n        target = entry_price - reward\n"),
    Mutant("H5", "HWC", "BUY prior-close context rule not applied",
           "strategy/hammer_context_core.py",
           "        if prev.close < signal.low:", "        if prev.close < signal.low - 1e9:"),
    Mutant("H6", "HWC", "min-wick filter never rejects",
           "strategy/hammer_context_core.py",
           "    if actual < need:", "    if actual < need - 1000:"),
    Mutant("H7", "HWC", "min-wick filter measures the wrong wick",
           "strategy/hammer_context_core.py",
           '    wick = candle.lower_wick if side == "lower" else candle.upper_wick',
           '    wick = candle.upper_wick if side == "lower" else candle.lower_wick'),
    Mutant("H8", "HWC 35%", "limit fill search skips the first bar",
           "strategy/hammer_context_core.py",
           "    for i in range(start_idx, end):", "    for i in range(start_idx + 1, end):"),
    Mutant("H9", "HWC 35%", "BUY limit 'fills' even when price never reached it",
           "strategy/hammer_context_core.py",
           "            if l <= limit_price:\n                return i",
           "            if True:\n                return i"),
    Mutant("H10", "HWC", "wick-off direction flipped (prev green → BUY)",
           "strategy/hammer_context_core.py",
           '    if prev.is_red:\n        return logic.TradeDirection.BUY, ""',
           '    if prev.is_green:\n        return logic.TradeDirection.BUY, ""'),
    Mutant("H11", "HWC", "context lookback checks one candle too few (BUY)",
           "strategy/hammer_context_core.py",
           "    start = signal_index - lookback\n    for j in range(start, signal_index):\n"
           "        prev = candles[j]\n        if prev.close < signal.low:",
           "    start = signal_index - lookback + 1\n    for j in range(start, signal_index):\n"
           "        prev = candles[j]\n        if prev.close < signal.low:"),
    Mutant("H12", "HWC", "plain HWC no longer forces pullback off (35% fix spills into HWC)",
           "strategy/hammer_with_candles_logic.py",
           "    out.entry_pullback_pct = 0.0\n", "    out.entry_pullback_pct = current\n"),
    Mutant("H13", "HWC 35%", "HWC 35% missing pullback falls back to 0 instead of 35",
           "strategy/hammer_with_candles_35_logic.py",
           "    if raw_pct is None or (isinstance(raw_pct, str) and not raw_pct.strip()):\n"
           "        return float(default)",
           "    if raw_pct is None or (isinstance(raw_pct, str) and not raw_pct.strip()):\n"
           "        return 0.0"),
    # ---------------------------------------------------------------- Backtest simulator
    Mutant("B1", "Backtest", "SELL P&L sign flipped",
           "backtest.py",
           "        gross_pnl = (entry_adj - exit_adj) * position_size",
           "        gross_pnl = (exit_adj - entry_adj) * position_size"),
    Mutant("B2", "Backtest", "slippage improves BUY entry instead of worsening it",
           "backtest.py",
           "    entry_adj = entry + config.slippage_usd if direction",
           "    entry_adj = entry - config.slippage_usd if direction"),
    Mutant("B3", "Backtest", "commission added to P&L",
           "backtest.py",
           "    return gross_pnl - config.commission_per_trade",
           "    return gross_pnl + config.commission_per_trade"),
    Mutant("B4", "Backtest", "worst-case model books a win when SL and TP hit on one bar",
           "backtest.py",
           "            ExitModel.WORST_CASE: loss,", "            ExitModel.WORST_CASE: win,"),
    Mutant("B5", "Backtest", "gap through SL filled at SL (should be the worse open)",
           "backtest.py",
           "            result = (TradeOutcome.LOSS, o, ts, i + 1)",
           "            result = (TradeOutcome.LOSS, sl, ts, i + 1)"),
    Mutant("B6", "Backtest", "gap through TP credited at the open (more than planned RR)",
           "backtest.py",
           "            result = (TradeOutcome.WIN, target, ts, i + 1)",
           "            result = (TradeOutcome.WIN, o, ts, i + 1)"),
    Mutant("B7", "Backtest", "long-scan SELL SL check uses the low",
           "backtest.py",
           "        sl_mask = high >= sl\n", "        sl_mask = low >= sl\n"),
    Mutant("B8", "Backtest", "short-scan BUY SL touch (low == SL) not counted",
           "backtest.py",
           "        hit_sl = (l <= sl) if is_buy else (h >= sl)",
           "        hit_sl = (l < sl) if is_buy else (h >= sl)"),
    Mutant("B9", "Backtest", "fixed-risk sizing multiplies instead of divides",
           "backtest.py",
           "        return risk_usd / risk_per_unit, risk_usd\n\n    if mode == PositionSizingMode.PERCENT_OF_EQUITY:",
           "        return risk_usd * risk_per_unit, risk_usd\n\n    if mode == PositionSizingMode.PERCENT_OF_EQUITY:"),
    Mutant("B10", "Backtest", "equity-multiple cap ignored (lots explode)",
           "backtest.py",
           "        sizing_equity = min(float(current_equity), equity_cap)",
           "        sizing_equity = float(current_equity)"),
    Mutant("B11", "Backtest", "equity floor ignored",
           "backtest.py",
           "        if current_equity <= config.equity_floor_usd or current_equity <= 0:",
           "        if current_equity <= 0:"),
    Mutant("B12", "Backtest", "NEXT_CANDLE_CLOSE entry scans its own entry bar",
           "backtest.py",
           "    if getattr(sig, \"entry_rule\", None) == logic.EntryRule.NEXT_CANDLE_CLOSE.value:\n"
           "        return fill_idx + 1",
           "    if getattr(sig, \"entry_rule\", None) == logic.EntryRule.NEXT_CANDLE_CLOSE.value:\n"
           "        return fill_idx"),
    Mutant("B13", "Backtest", "overlap filter disabled",
           "backtest.py",
           "                if still_open_blocking:", "                if False:"),
    Mutant("B14", "Backtest", "unfilled pullback limit treated as filled",
           "backtest.py",
           "            if found is None:\n                # Limit never filled",
           "            if found is None:\n                found = entry_idx\n"
           "            if False:\n                # Limit never filled"),
    Mutant("B15", "Backtest", "BUY gap below the limit not filled at the better open",
           "backtest.py",
           "                if fill_open <= limit_px and fill_open > sl_px:\n                    sig.entry_price = fill_open",
           "                if False:\n                    sig.entry_price = fill_open"),
    Mutant("B16", "Backtest", "HWC 35% not dispatched to its own module",
           "backtest.py",
           "    elif hwc35_logic.is_pattern_type(config.pattern_type):\n        # Separate strategy",
           "    elif False:\n        # Separate strategy"),
    Mutant("B17", "Backtest", "Doji not dispatched to its own module",
           "backtest.py",
           '    if config.pattern_type == "doji":\n        all_signals = doji_logic.run_strategy(',
           '    if config.pattern_type == "doji_x":\n        all_signals = doji_logic.run_strategy('),
    Mutant("B18", "Backtest", "session / IST filter skipped",
           "backtest.py",
           "    session_skipped, ist_skipped = sessions.apply_session_and_time_filters(",
           "    session_skipped, ist_skipped = (lambda *a, **k: (0, 0))("),
    Mutant("B19", "Backtest", "indicator filters skipped",
           "backtest.py",
           "    if config.indicator_stack.enabled_indicator_ids():\n        all_signals = apply_indicator_filters(",
           "    if False:\n        all_signals = apply_indicator_filters("),
    Mutant("B20", "Metrics", "win rate divides by all rows (incl. skipped/open)",
           "backtest.py",
           '          .then(pl.col("wins") / pl.col("total_trades") * 100.0)',
           '          .then(pl.col("wins") / pl.col("total_rows") * 100.0)'),
    Mutant("B21", "Metrics", "drawdown measured from start capital, not the running peak",
           "backtest.py",
           "    running_peak = np.maximum.accumulate(equity_curve)",
           "    running_peak = np.full_like(equity_curve, equity_curve[0])"),
    Mutant("B22", "Metrics", "profit factor inverted",
           "backtest.py",
           '          .then(pl.col("gross_profit") / pl.col("gross_loss").abs())',
           '          .then(pl.col("gross_loss").abs() / pl.col("gross_profit"))'),
    Mutant("B23", "Backtest", "%-equity sizing never compounds (always start capital)",
           "backtest.py",
           "                sized[i] = calculate_position_size(sig, config, equity)",
           "                sized[i] = calculate_position_size(sig, config, config.starting_capital)"),
    # ---------------------------------------------------------------- Filters
    Mutant("S1", "Sessions", "session filter never applied",
           "sessions.py",
           "    filter_sessions = enabled != set(SESSION_ORDER)", "    filter_sessions = False"),
    Mutant("I1", "Indicators", "ANY combine mode behaves like ALL",
           "indicators/filter.py",
           "    if any(ok for ok, _ in checks):\n        return True, \"\"",
           "    if all(ok for ok, _ in checks):\n        return True, \"\""),
    Mutant("I2", "Indicators", "indicator check uses the wrong bar (one late)",
           "indicators/filter.py",
           "        checks = _indicator_checks_at_index(\n            sig.direction, idx, close,",
           "        checks = _indicator_checks_at_index(\n            sig.direction, idx + 1, close,"),
    # ---------------------------------------------------------------- Live
    Mutant("V1", "Live", "market fill keeps the strategy TP (RR broken)",
           "live.py", "        return sl, fill + rr * risk\n", "        return sl, float(sig.target)\n"),
    Mutant("V2", "Live", "limit at strategy entry sent at the market price",
           "live.py",
           "            exec_entry = strat_entry\n            sl, tp = reanchor_sl_tp_to_fill(sig, exec_entry)\n"
           "            limit_price = strat_entry",
           "            exec_entry = market\n            sl, tp = reanchor_sl_tp_to_fill(sig, exec_entry)\n"
           "            limit_price = market"),
    Mutant("V3", "Live", "max entry deviation check inverted",
           "live.py",
           "        force_market_dev = max_dev > 0 and dev_pts > max_dev",
           "        force_market_dev = max_dev > 0 and dev_pts < max_dev"),
    Mutant("V4", "Live", "HWC 35% Limit order type sends market anyway",
           "live.py",
           '            if order_mode == "market":\n                sl, tp = reanchor_sl_tp_to_fill(sig, market)',
           '            if True:\n                sl, tp = reanchor_sl_tp_to_fill(sig, market)'),
    Mutant("V5", "Live", "HWC 35% not dispatched to its own module",
           "live.py",
           "    elif hwc35_logic.is_pattern_type(pattern_type):\n        strategy_config = hwc35_logic.prepare_config(",
           "    elif False:\n        strategy_config = hwc35_logic.prepare_config("),
    Mutant("V6", "Live", "Doji not dispatched to its own module",
           "live.py",
           '    if pattern_type == "doji":\n        signals = doji_logic.run_strategy(',
           '    if pattern_type == "doji_x":\n        signals = doji_logic.run_strategy('),
    Mutant("V7", "Live", "indicator filters skipped",
           "live.py",
           "    if indicator_stack.enabled_indicator_ids():\n        df = _candles_to_polars(closed, forming)",
           "    if False:\n        df = _candles_to_polars(closed, forming)"),
    Mutant("V8", "Live", "session / IST filter skipped",
           "live.py",
           "    sessions.apply_session_and_time_filters(\n        signals,\n        sessions_enabled=sessions_enabled,",
           "    (lambda *a, **k: None)(\n        signals,\n        sessions_enabled=sessions_enabled,"),
    Mutant("V9", "Live", "acts on an old signal bar (not the last closed bar)",
           "live.py",
           "        if sig.hammer_candle.timestamp != signal_bar_ts:\n            continue\n"
           "        if sig.entry_candle.timestamp != entry_bar_ts:\n            continue",
           "        if False:\n            continue\n        if False:\n            continue"),
    Mutant("V12", "Live", "forming bar left out, so the last closed bar never signals",
           "live.py", "    extended = closed + [forming]\n", "    extended = list(closed)\n"),
    Mutant("V10", "Live", "'Limit ± offset' ignores the from-market setting",
           "live.py",
           "            base = market if cfg.limit_offset_from_market else strat_entry",
           "            base = market"),
    Mutant("V11", "Live", "real orders allowed without a daily loss limit",
           "live.py",
           "    if cfg.max_daily_loss_usd <= 0:\n        return \"Set Max daily loss",
           "    if cfg.max_daily_loss_usd < -1:\n        return \"Set Max daily loss"),
    # ---------------------------------------------------------------- API accounts
    Mutant("A1", "API", "per-account order type ignored (always market)",
           "API/runtime.py",
           '        order_mode=coerce_order_mode(getattr(binding, "order_mode", "market")),',
           '        order_mode="market",'),
    Mutant("A2", "API", "Hammer preset inverted entry offset dropped",
           "API/runtime.py",
           '        inverted_entry_offset=_float(_f(fields, "inverted_entry_offset"), 0.0),\n'
           '        inverted_sl_mode=logic.coerce_stop_loss_mode(\n'
           '            _f(fields, "inverted_sl_mode", _f(fields, "sl_mode", "CANDLE_EXTREME"))',
           '        inverted_entry_offset=0.0,\n'
           '        inverted_sl_mode=logic.coerce_stop_loss_mode(\n'
           '            _f(fields, "inverted_sl_mode", _f(fields, "sl_mode", "CANDLE_EXTREME"))'),
    Mutant("A3", "API", "HWC 35% preset loses its pullback %",
           "API/runtime.py", "        entry_pullback_pct=pull,\n", "        entry_pullback_pct=0.0,\n"),
    Mutant("A4", "API", "min-wick switch not loaded from preset",
           "API/runtime.py",
           '        buy_min_lower_wick_enabled=_bool(_f(fields, "buy_min_lower_wick_enabled"), False),',
           "        buy_min_lower_wick_enabled=False,"),
    Mutant("A5", "API", "Doji ratio fields not loaded from preset",
           "API/runtime.py",
           '        f.name: _float(_f(fields, f"doji_{f.name}"), getattr(rd, f.name))',
           "        f.name: getattr(rd, f.name)"),
    Mutant("A6", "API", "timeframe RR not loaded from preset",
           "API/runtime.py",
           '            rr_multiple=_float(cfg.get("rr"), 2.0),\n            max_sl_usd=_float(cfg.get("sl"), 20.0),\n'
           "        )\n    for tf",
           "            rr_multiple=2.0,\n            max_sl_usd=_float(cfg.get(\"sl\"), 20.0),\n"
           "        )\n    for tf"),
    # ---------------------------------------------------------------- Alerts / history
    Mutant("T1", "Telegram", "HWC 35% shown as HWC",
           "notification/telegram.py",
           '    "hammer with candle 35%": "HWC 35%",', '    "hammer with candle 35%": "HWC",'),
    Mutant("T2", "Telegram", "user text not HTML-escaped",
           "notification/telegram.py", "    e = _html.escape\n", "    e = lambda s, *a, **k: s\n"),
    Mutant("W1", "Live history", "HWC SELL explained with the BUY row settings",
           "live_history.py", "        inverted = not is_buy\n", "        inverted = False\n"),
]


# ---------------------------------------------------------------------------

def _ignore(dirpath: str, names: List[str]) -> List[str]:
    return [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")]


def _make_copy(dest_parent: str) -> str:
    dest = os.path.join(dest_parent, "repo")
    shutil.copytree(ROOT, dest, ignore=_ignore, symlinks=True)
    for name in LINK_DIRS:
        src = os.path.join(ROOT, name)
        if os.path.isdir(src) and not os.path.exists(os.path.join(dest, name)):
            os.symlink(src, os.path.join(dest, name))
    return dest


def _pytest(repo: str, timeout: int, extra: Optional[List[str]] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", HAMMER_MUTATION_RUN="1")
    cmd = [sys.executable, "-m", "pytest", "tests", "-x", "-q", "-p", "no:cacheprovider"]
    cmd += extra or []
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)


def _apply(repo: str, m: Mutant) -> Optional[str]:
    """Write the mutant; returns an error string when it cannot be applied."""
    path = os.path.join(repo, m.path)
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return f"cannot read {m.path}: {exc}"
    count = src.count(m.old)
    if count != 1:
        return f"'old' text found {count}× in {m.path} (must be exactly 1) — update the mutant"
    mutated = src.replace(m.old, m.new)
    try:
        compile(mutated, path, "exec")
    except SyntaxError as exc:
        return f"mutant is not valid Python: {exc}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    return None


def _first_failure(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            return line.split(" - ")[0].replace("FAILED ", "").replace("ERROR ", "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    mutants = MUTANTS
    if args.only:
        wanted = set(args.only)
        mutants = [m for m in MUTANTS if m.id in wanted]
    if args.list:
        for m in mutants:
            print(f"{m.id:5} {m.area:12} {m.bug}")
        return 0

    tmp = tempfile.mkdtemp(prefix="hammer_mutation_")
    print(f"Copying code to {tmp} …")
    base = _make_copy(os.path.join(tmp, "base"))
    t0 = time.time()
    res = _pytest(base, args.timeout)
    if res.returncode != 0:
        print("Baseline test run FAILED on the unmodified copy — fix that first:\n")
        print(res.stdout[-3000:], res.stderr[-2000:])
        return 2
    print(f"Baseline green in {time.time() - t0:.1f}s. Running {len(mutants)} mutants on {args.jobs} workers …\n")

    workers = []
    for w in range(max(1, args.jobs)):
        workers.append(_make_copy(os.path.join(tmp, f"w{w}")))

    work: "queue.Queue[Mutant]" = queue.Queue()
    for m in mutants:
        work.put(m)
    results: Dict[str, tuple] = {}
    lock = threading.Lock()

    def run(repo: str) -> None:
        while True:
            try:
                m = work.get_nowait()
            except queue.Empty:
                return
            path = os.path.join(repo, m.path)
            with open(path, encoding="utf-8") as fh:
                original = fh.read()
            err = _apply(repo, m)
            if err:
                status, detail = "BAD", err
            else:
                try:
                    r = _pytest(repo, args.timeout)
                    if r.returncode == 0:
                        status, detail = "SURVIVED", ""
                    else:
                        status, detail = "killed", _first_failure(r.stdout)
                except subprocess.TimeoutExpired:
                    status, detail = "killed", "timeout"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            with lock:
                results[m.id] = (status, detail)
                mark = {"killed": "ok ", "SURVIVED": "!! ", "BAD": "?? "}[status]
                print(f"  {mark}{m.id:5} {status:8} {m.area:12} {m.bug}"
                      + (f"   [{detail}]" if detail and status != "killed" else ""))
                sys.stdout.flush()

    threads = [threading.Thread(target=run, args=(repo,)) for repo in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    killed = [m for m in mutants if results.get(m.id, ("",))[0] == "killed"]
    survived = [m for m in mutants if results.get(m.id, ("",))[0] == "SURVIVED"]
    bad = [m for m in mutants if results.get(m.id, ("",))[0] == "BAD"]
    print(f"\nKilled {len(killed)}/{len(mutants)}  ·  survived {len(survived)}  ·  not applicable {len(bad)}"
          f"  ·  {time.time() - t0:.0f}s")
    if survived:
        print("\nBugs the tests did NOT catch:")
        for m in survived:
            print(f"  {m.id:5} {m.area:12} {m.bug}   ({m.path})")
    if bad:
        print("\nMutants that no longer apply (code changed — update them):")
        for m in bad:
            print(f"  {m.id:5} {results[m.id][1]}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if (survived or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
