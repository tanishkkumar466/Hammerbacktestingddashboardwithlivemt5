"""
Run every pattern's backtest on local data and print a fingerprint per pattern.

Usage (from repo root):
    python scripts/pattern_parity_probe.py [data_root] [timeframe_folder] [year]

Compare the output between two checkouts (e.g. release tag vs working tree)
to prove calculations did not drift.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import logic  # noqa: E402
import doji_logic  # noqa: E402
import hammer_context_logic as hc  # noqa: E402
from backtest import (  # noqa: E402
    BacktestConfig,
    ExitModel,
    PositionSizingMode,
    run_full_backtest,
)


def _cases():
    return [
        ("hammer", "hammer", logic.StrategyConfig()),
        ("doji", "doji", doji_logic.DojiStrategyConfig()),
        ("hwc", hc.PATTERN_TYPE, hc.HammerContextConfig(entry_pullback_pct=0.0)),
        ("hwc35@35", hc.PATTERN_TYPE_35, hc.HammerContextConfig(entry_pullback_pct=35.0)),
    ]


def main() -> None:
    data_root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data")
    tf = sys.argv[2] if len(sys.argv) > 2 else "1hour"
    year = int(sys.argv[3]) if len(sys.argv) > 3 else 2024
    for name, ptype, strat in _cases():
        cfg = BacktestConfig(
            strategy_config=strat,
            pattern_type=ptype,
            symbol="XAUUSD",
            data_root=data_root,
            start_date=datetime(year, 1, 1),
            end_date=datetime(year + 1, 1, 1),
            timeframes_to_test=[tf],
            max_forward_candles=200,
            position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
            fixed_risk_usd=100.0,
            starting_capital=10000.0,
            allow_overlapping_trades=False,
            sessions_enabled={"Asian": True, "London": True, "US": True},
        )
        with contextlib.redirect_stdout(io.StringIO()):
            ledger, ignored = run_full_backtest(cfg)
        m = ExitModel.WORST_CASE
        cnt = Counter(t.outcomes[m][0].name for t in ledger)
        net = sum(t.pnl.get(m, 0.0) for t in ledger)
        h = hashlib.sha1()
        for t in ledger:
            o, px, ts, bars = t.outcomes[m]
            h.update(
                f"{t.entry_time}|{t.signal.direction.value}|{t.signal.entry_price:.5f}|"
                f"{t.signal.stop_loss:.5f}|{t.signal.target:.5f}|{o.name}|{px}|{ts}|"
                f"{t.pnl.get(m, 0.0):.4f}\n".encode()
            )
        print(
            f"{name:9s} trades={len(ledger):5d} ignored={len(ignored):5d} "
            f"{dict(sorted(cnt.items()))} net={net:,.2f} sha={h.hexdigest()[:12]}"
        )


if __name__ == "__main__":
    main()
