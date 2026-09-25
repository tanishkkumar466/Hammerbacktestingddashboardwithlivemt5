"""Live account audit: executed, skipped, and errors land in live_trades.csv with why."""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import logic
from live import LiveTradingEngine
from live_journal import trades_csv_path


def _candle(ts: str, o: float, h: float, l: float, c: float) -> logic.Candle:
    return logic.Candle(ts, o, h, l, c)


def _engine(tmp_path: Path) -> LiveTradingEngine:
    eng = LiveTradingEngine.__new__(LiveTradingEngine)
    eng._journal_dir = str(tmp_path)
    eng._active_symbol = "XAUUSD"
    eng.live_config = SimpleNamespace(
        symbol="XAUUSD",
        timeframe_label="3m",
        order_mode="market",
        magic=42,
        notify_enabled=False,
        account_id="acc1",
        account_name="Demo",
        slot_name="slot-a",
        volume=0.1,
        dry_run=False,
    )
    eng.pattern_label = "Hammer"
    eng._telegram = MagicMock()
    eng._refresh_tracked_from_broker = MagicMock()
    eng.log = lambda *_a, **_k: None
    return eng


def _sig() -> logic.TradeSignal:
    return logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=_candle("s", 1, 2, 0, 1.5),
        entry_candle=_candle("e", 1, 2, 0, 1.5),
        entry_price=2652.3,
        stop_loss=2639.7,
        risk=12.6,
        rr_multiple=2.0,
        target=2677.5,
        timeframe="3m",
        ignore_reason="Risk exceeds max SL",
        pattern_variant="CLASSIC",
    )


def _read_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_audit_records_executed_order_and_fail_with_reason(tmp_path):
    eng = _engine(tmp_path)
    sig = _sig()
    eng._record_trade_event(
        "ORDER", sig, volume=0.1, dry_run=False,
        mt5_message="ok", reason="Order accepted by broker",
        entry_price=2652.0, stop_loss=2639.7, target=2676.6,
    )
    eng._record_trade_event(
        "ORDER_FAIL", sig, volume=0.1, dry_run=False,
        mt5_message="retcode 10016", reason="retcode 10016",
    )
    rows = _read_rows(Path(trades_csv_path(str(tmp_path))))
    assert [r["event"] for r in rows] == ["ORDER", "ORDER_FAIL"]
    assert rows[0]["reason"] == "Order accepted by broker"
    assert rows[1]["reason"] == "retcode 10016"
    assert rows[0]["slot"] == "slot-a"


def test_audit_records_ignored_and_safety_skip(tmp_path):
    eng = _engine(tmp_path)
    sig = _sig()
    eng._record_skip("IGNORED", "Risk exceeds max SL", sig)
    eng._record_skip("SAFETY_BLOCK", "Spread too wide (80.0 pts > max 50.0).", sig)
    eng._record_skip("STARTUP_FAIL", "MT5 not connected — connect in Live panel before Start.")
    rows = _read_rows(Path(trades_csv_path(str(tmp_path))))
    assert [r["event"] for r in rows] == ["IGNORED", "SAFETY_BLOCK", "STARTUP_FAIL"]
    assert "max SL" in rows[0]["reason"]
    assert "Spread" in rows[1]["reason"]
    assert "not connected" in rows[2]["reason"]
    assert rows[0]["direction"] == "BUY"
    assert rows[2]["direction"] == ""


def test_worker_slot_log_includes_slot_id():
    """Regression: multi-account session logs must carry slot_id for routing."""
    from pathlib import Path as P

    src = P("live_account_worker.py").read_text(encoding="utf-8")
    assert "slot_id=_sid" in src
    assert "log=lambda msg" in src
