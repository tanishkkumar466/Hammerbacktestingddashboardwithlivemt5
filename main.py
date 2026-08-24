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


def _report_pending_update_failure() -> None:
    """Show a dialog if the last Check-for-Updates bat failed (Windows)."""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    try:
        import updater

        message = updater.read_pending_update_message()
    except Exception:
        return
    if not message:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "Hammer — update failed", 0x10)
    except Exception:
        pass


def _run_dashboard() -> None:
    from hammer_boot import boot_log, enable_faulthandler

    enable_faulthandler()
    boot_log("main: _run_dashboard start")
    os.chdir(_app_dir())
    boot_log(f"main: cwd={os.getcwd()}")
    # Stepwise imports in frozen builds — pinpoints "module could not be found" DLL errors.
    if getattr(sys, "frozen", False):
        for mod in (
            "sqlite3", "_sqlite3", "ssl", "_ssl",
            "PySide6", "shiboken6",
            "numpy", "polars", "pyarrow",
            "matplotlib", "PIL",
        ):
            try:
                boot_log(f"main: import {mod} ...")
                __import__(mod)
                boot_log(f"main: import {mod} OK")
            except Exception as exc:
                raise ImportError(
                    f"Hammer could not load native library for '{mod}': {exc}\n"
                    "Usually a missing .dll in the exe bundle (rebuild with latest spec)."
                ) from exc
    boot_log("main: import dashboard ...")
    import dashboard
    boot_log("main: import dashboard OK")
    boot_log("main: dashboard.launch() ...")
    dashboard.launch()


def _report_frozen_crash(exc: BaseException) -> None:
    """Write hammer_crash.log and show a Windows message box when the exe fails to start."""
    if not getattr(sys, "frozen", False):
        raise exc
    import traceback

    log_path = os.path.join(_app_dir(), "hammer_crash.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Hammer failed to start\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except OSError:
        log_path = "(could not write log)"

    if os.name == "nt":
        try:
            import ctypes

            msg = f"{exc}\n\nDetails saved to:\n{log_path}"
            ctypes.windll.user32.MessageBoxW(0, msg, "Hammer — startup failed", 0x10)
        except Exception:
            pass
    raise exc


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
    # If this process was launched as a fresh instance after update, drop the
    # public reset flag so child workers (ray/multiprocessing) behave normally.
    os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)

    if getattr(sys, "frozen", False):
        from hammer_boot import boot_log

        boot_log("main: entered")

    _report_pending_update_failure()
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
    try:
        raise SystemExit(main())
    except Exception as _exc:
        _report_frozen_crash(_exc)
        raise SystemExit(1)
