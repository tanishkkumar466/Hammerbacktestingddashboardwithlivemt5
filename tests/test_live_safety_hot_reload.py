"""Live safety hot-reload — spread can change without Stop/Start."""

from __future__ import annotations

from unittest.mock import MagicMock

import live as live_trading


def test_update_runtime_safety_changes_spread_without_restart():
    cfg = live_trading.LiveRunConfig(
        symbol="XAUUSD",
        timeframe_label="3m",
        volume=0.01,
        magic=1100,
        max_open_positions=1,
        poll_interval_sec=2.0,
        max_spread_points=50.0,
        min_minutes_between_trades=5.0,
    )
    broker = MagicMock()
    eng = live_trading.LiveTradingEngine(
        broker=broker,
        live_config=cfg,
        pattern_type="hammer",
        pattern_label="Hammer",
        strategy_config=MagicMock(),
        indicator_stack=MagicMock(),
        log=lambda *_a, **_k: None,
    )
    assert eng.live_config.max_spread_points == 50.0
    eng.update_runtime_safety(max_spread_points=150.0, min_minutes_between_trades=0.0)
    assert eng.live_config.max_spread_points == 150.0
    assert eng.live_config.min_minutes_between_trades == 0.0
