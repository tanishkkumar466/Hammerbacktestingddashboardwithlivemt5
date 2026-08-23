"""
Hammer Candle Backtest Dashboard — entry point.

  python main.py              Launch the Qt dashboard (default)
  python main.py backtest     Run a quick backtest export to output/run_no_overlap
  python main.py plots        Generate charts from output/run_no_overlap
"""

from __future__ import annotations

import argparse
import os
import sys


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _cleanup_stale_update_artifacts() -> None:
    """Remove leftover PyInstaller / updater temp folders next to the app."""
    if not getattr(sys, "frozen", False):
        return
    root = _app_dir()
    for name in (
        "_hammer_update_staging",
        "_hammer_update_extract",
        "_hammer_apply_update.bat",
        "_hammer_relaunch.bat",
    ):
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _run_dashboard() -> None:
    os.chdir(_app_dir())
    # Import the module (not runpy on a .py path). Frozen onefile has no
    # dashboard.py next to the exe — only inside the bundle as a module.
    import dashboard
    dashboard.launch()


def _run_backtest() -> int:
    os.chdir(_app_dir())
    from backtest import BacktestConfig, PositionSizingMode, run_backtest_and_export

    config = BacktestConfig(
        symbol="XAUUSD",
        timeframes_to_test=["1hour"],
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
        allow_overlapping_trades=False,
        run_name="run_no_overlap",
    )
    run_backtest_and_export(config)
    return 0


def _run_plots() -> int:
    os.chdir(_app_dir())
    from plotting import PlottingConfig, run_full_report

    run_full_report(
        PlottingConfig(
            backtest_output_dir="output/run_no_overlap",
            primary_exit_model="worst_case",
            output_dir="plots",
        )
    )
    return 0


def main() -> int:
    _cleanup_stale_update_artifacts()
    parser = argparse.ArgumentParser(description="Hammer candle backtest dashboard")
    parser.add_argument(
        "command",
        nargs="?",
        default="gui",
        choices=("gui", "backtest", "plots"),
        help="gui (default), backtest, or plots",
    )
    args = parser.parse_args()

    if args.command == "gui":
        _run_dashboard()
        return 0
    if args.command == "backtest":
        return _run_backtest()
    return _run_plots()


if __name__ == "__main__":
    raise SystemExit(main())
