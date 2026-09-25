"""API real-trading runtime — builds Live-compatible start payloads from presets."""

from __future__ import annotations

import json
from pathlib import Path

from API.accounts import ApiAccount
from API.bindings import StrategyBinding
from API.runtime import (
    api_slot_id,
    api_worker_key,
    build_api_trade_start,
    start_slot_kwargs,
    strategy_from_preset,
)


ROOT = Path(__file__).resolve().parents[1]


def test_strategy_from_hammer_preset():
    path = ROOT / "template" / "preset_example_hammer_green_buy_red_sell.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    label, ptype, cfg, stack = strategy_from_preset(data)
    assert label == "Hammer"
    assert ptype == "hammer"
    assert cfg is not None
    assert stack is not None


def test_strategy_from_context_preset():
    path = ROOT / "template" / "preset_example_hammer_with_candles.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    label, ptype, cfg, stack = strategy_from_preset(data)
    assert "candle" in label.lower() or "Hammer" in label
    assert ptype
    assert getattr(cfg, "lookback_candles", 0) >= 1


def test_build_api_trade_start_uses_live_bridge_fields(tmp_path):
    presets = ROOT / "template"
    acc = ApiAccount.new("IC Demo")
    acc.login = "50123456"
    acc.server = "ICMarketsEU-Demo"
    acc.password = "secret"
    binding = StrategyBinding.new(
        acc.id,
        "preset_example_hammer_green_buy_red_sell.json",
        "3m",
        symbol="XAUUSD",
        magic=1155,
        volume="0.02",
    )
    binding.dry_run = True
    start = build_api_trade_start(
        acc,
        binding,
        presets_dir=str(presets),
        terminal_path=r"C:\MT5\terminal64.exe",
        journal_dir=str(tmp_path / "j"),
    )
    assert start.worker_key == api_worker_key(acc.id)
    assert start.slot_id == api_slot_id(binding.id)
    assert start.credentials.login == 50123456
    assert start.credentials.server == "ICMarketsEU-Demo"
    assert start.live_config.symbol == "XAUUSD"
    assert start.live_config.timeframe_label == "3m"
    assert start.live_config.magic == 1155
    assert start.live_config.dry_run is True
    kwargs = start_slot_kwargs(start)
    assert kwargs["slot_id"] == start.slot_id
    assert "live_config" in kwargs
    assert "strategy_config" in kwargs
    assert "indicator_stack" in kwargs


def test_build_api_trade_start_requires_password():
    acc = ApiAccount.new("X")
    acc.login = "1"
    acc.server = "S"
    acc.password = ""
    binding = StrategyBinding.new(acc.id, "p.json", "3m")
    try:
        build_api_trade_start(acc, binding, presets_dir=str(ROOT / "template"))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "password" in str(exc).lower()


def test_api_pipeline_documents_real_bridge():
    """Real fills = Live mt5.order_send path, not C++ /orders/batch."""
    from API.runtime import build_api_trade_start as _  # noqa: F401

    # Module docstring is the contract for clients
    import API.runtime as rt

    assert "order_send" in (rt.__doc__ or "")
    assert "LiveTradingEngine" in (rt.__doc__ or "")
