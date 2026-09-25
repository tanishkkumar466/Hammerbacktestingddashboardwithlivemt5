#!/usr/bin/env python3
"""
Hammer master test runner — strategy, backtest, indicators, Live, API, notifications.

Usage (from repo root):
  python scripts/run_all_tests.py              # standard (recommended)
  python scripts/run_all_tests.py --smoke      # fast cross-check
  python scripts/run_all_tests.py --full       # everything including heavy backtests
  python scripts/run_all_tests.py --area api   # one area only

Exit code = pytest exit code.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# ---- curated suites (paths relative to repo root) ----

SMOKE = [
    "tests/test_direction_matrix.py",
    "tests/test_stop_loss_mode.py",
    "tests/test_backtest_pipeline_fixes.py",
    "tests/test_rsi.py",
    "tests/test_rolling_vwap.py",
    "tests/test_presets.py",
    "tests/test_live_accounts.py",
    "tests/test_live_safety_hot_reload.py",
    "tests/test_live_multi_account_hardening.py",
    "tests/test_api_pipeline.py",
    "tests/test_api_algo.py",
    "tests/test_api_trading_runtime.py",
    "tests/test_notification_manager.py",
    "tests/test_telegram_notify.py",
    "tests/test_master_smoke.py",
]

AREAS = {
    "strategy": [
        "tests/test_direction_matrix.py",
        "tests/test_stop_loss_mode.py",
        "tests/test_hammer_context.py",
        "tests/test_backtest_pipeline_fixes.py",
        "tests/test_presets.py",
    ],
    "backtest": [
        "tests/test_backtest_pipeline_fixes.py",
        "tests/test_position_sizing.py",
        "tests/test_sessions.py",
        "tests/test_metrics_integrity.py",
        "tests/test_metrics_avg_win.py",
        "tests/test_run_database_schema.py",
    ],
    "indicators": [
        "tests/test_rsi.py",
        "tests/test_rolling_vwap.py",
        "tests/test_trade_inspect_indicators.py",
    ],
    "live": [
        "tests/test_live_accounts.py",
        "tests/test_live_pullback_orders.py",
        "tests/test_live_preset_pullback.py",
        "tests/test_live_price_feed.py",
        "tests/test_live_safety_hot_reload.py",
        "tests/test_live_multi_account_hardening.py",
        "tests/test_live_account_ipc.py",
    ],
    "api": [
        "tests/test_api_pipeline.py",
        "tests/test_api_algo.py",
        "tests/test_api_trading_runtime.py",
        "tests/test_master_smoke.py",
    ],
    "notification": [
        "tests/test_notification_manager.py",
        "tests/test_telegram_notify.py",
    ],
}

# Heavy / slow — included in --full only when using STANDARD (excluded from standard)
HEAVY = {
    "tests/test_metrics_integrity.py",
    "tests/test_metrics_avg_win.py",
    "tests/test_run_database_schema.py",
    "tests/test_live_account_ipc.py",
}


def _all_test_files() -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in TESTS.glob("test_*.py"))


def _standard() -> list[str]:
    return [p for p in _all_test_files() if p not in HEAVY]


def _resolve(args: argparse.Namespace) -> list[str]:
    if args.area:
        key = args.area.lower().strip()
        if key not in AREAS:
            print(f"Unknown area {args.area!r}. Choose: {', '.join(AREAS)}", file=sys.stderr)
            sys.exit(2)
        return list(AREAS[key])
    if args.smoke:
        return list(SMOKE)
    if args.full:
        return _all_test_files()
    return _standard()


def main() -> int:
    parser = argparse.ArgumentParser(description="Hammer master test runner")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--smoke", action="store_true", help="Fast curated suite")
    g.add_argument("--full", action="store_true", help="All tests including heavy backtests / IPC")
    g.add_argument(
        "--area",
        choices=sorted(AREAS.keys()),
        help="Run one area only (strategy|backtest|indicators|live|api|notification)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Less pytest chatter")
    parser.add_argument("-k", default=None, help="pytest -k expression")
    args = parser.parse_args()

    targets = _resolve(args)
    missing = [t for t in targets if not (ROOT / t).is_file()]
    if missing:
        print("Missing test files:", *missing, sep="\n  ", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        "-v",
        "--tb=short",
    ]
    if args.quiet:
        cmd.append("-q")
    if args.k:
        cmd.extend(["-k", args.k])

    label = "smoke" if args.smoke else ("full" if args.full else (f"area:{args.area}" if args.area else "standard"))
    print("=" * 60)
    print(f"Hammer tests — mode={label}  files={len(targets)}")
    print("=" * 60)
    for t in targets:
        print(f"  • {t}")
    print("-" * 60)

    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    print("-" * 60)
    if proc.returncode == 0:
        print(f"PASS — {label} ({len(targets)} files)")
    else:
        print(f"FAIL — {label} (exit {proc.returncode})", file=sys.stderr)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
