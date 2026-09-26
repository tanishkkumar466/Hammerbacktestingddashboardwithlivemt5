"""Entry Offset ($) warning: shown when the Live / API order type will not use the offset."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import logic
from API.runtime import strategy_from_preset
from live import LiveTradingEngine
from live_history import configured_entry_offsets, entry_offset_ignored_warning

TEMPLATES = Path(__file__).resolve().parents[1] / "template"
HAMMER = "preset_example_hammer_green_buy_red_sell.json"
HWC = "preset_example_hammer_with_candles.json"
HWC35 = "preset_example_hammer_with_candle_35.json"


def _preset(name, **fields):
    data = json.loads((TEMPLATES / name).read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data.setdefault("fields", {}).update({k: str(v) for k, v in fields.items()})
    return data


def _strategy(name, **fields):
    _label, ptype, cfg, _stack = strategy_from_preset(_preset(name, **fields))
    return ptype, cfg


# ---------------------------------------------------------------- which rows carry an offset

@pytest.mark.parametrize("name", [HAMMER, HWC, HWC35])
def test_no_offset_no_warning_for_any_order_type(name):
    ptype, cfg = _strategy(name, entry_offset=0, inverted_entry_offset=0)
    assert configured_entry_offsets(cfg, ptype) == []
    for mode in ("market", "limit_entry", "limit_offset"):
        assert entry_offset_ignored_warning(cfg, ptype, mode) == ""


def test_hammer_rows_are_classic_and_inverted():
    ptype, cfg = _strategy(HAMMER, entry_offset=1.5, inverted_entry_offset=-0.5)
    assert configured_entry_offsets(cfg, ptype) == [("Classic", 1.5), ("Inverted", -0.5)]


@pytest.mark.parametrize("name", [HWC, HWC35])
def test_hammer_with_candles_rows_are_buy_and_sell(name):
    ptype, cfg = _strategy(name, entry_offset=2, inverted_entry_offset=0)
    assert configured_entry_offsets(cfg, ptype) == [("BUY row", 2.0)]
    ptype, cfg = _strategy(name, entry_offset=0, inverted_entry_offset=-1.25)
    assert configured_entry_offsets(cfg, ptype) == [("SELL row", -1.25)]


def test_doji_single_row():
    _l, ptype, cfg, _s = strategy_from_preset({"pattern": "Doji", "fields": {"entry_offset": "0.4"}})
    assert ptype == "doji"
    assert configured_entry_offsets(cfg, ptype) == [("Entry", 0.4)]


# ---------------------------------------------------------------- when the warning fires

@pytest.mark.parametrize("name", [HAMMER, HWC, HWC35])
def test_market_ignores_offset_and_warns(name):
    ptype, cfg = _strategy(name, entry_offset=1.5)
    msg = entry_offset_ignored_warning(cfg, ptype, "market")
    assert "Entry Offset" in msg and "$+1.5" in msg
    assert "'Market'" in msg and "Limit — at strategy entry price" in msg


@pytest.mark.parametrize("name", [HAMMER, HWC, HWC35])
def test_limit_at_strategy_entry_uses_offset_no_warning(name):
    ptype, cfg = _strategy(name, entry_offset=1.5)
    assert entry_offset_ignored_warning(cfg, ptype, "limit_entry") == ""


@pytest.mark.parametrize("name", [HAMMER, HWC])
def test_limit_offset_from_bid_ask_warns_but_from_entry_does_not(name):
    ptype, cfg = _strategy(name, entry_offset=1.5)
    assert "bid/ask" in entry_offset_ignored_warning(cfg, ptype, "limit_offset", True)
    assert entry_offset_ignored_warning(cfg, ptype, "limit_offset", False) == ""


def test_hwc35_limit_offset_places_pullback_from_entry_no_warning():
    ptype, cfg = _strategy(HWC35, entry_offset=1.5)
    assert entry_offset_ignored_warning(cfg, ptype, "limit_offset", True) == ""


def test_warning_does_not_touch_strategy_config():
    ptype, cfg = _strategy(HWC, entry_offset=1.5, inverted_entry_offset=-2)
    before = (cfg.entry_offset, cfg.inverted_entry_offset)
    entry_offset_ignored_warning(cfg, ptype, "market")
    assert (cfg.entry_offset, cfg.inverted_entry_offset) == before


# ---------------------------------------------------------------- Live order log

def _engine(order_mode, strategy_cfg, pattern_type="hammer", **cfg_extra):
    broker = MagicMock()
    broker.get_tick_prices.return_value = (2651.90, 2652.0)
    broker.symbol_point.return_value = 0.01
    broker.limit_price_with_offset.return_value = (2651.5, True)
    opts = {
        "order_mode": order_mode,
        "max_entry_deviation_points": 200.0,
        "limit_offset_from_market": True,
        "limit_offset_points": 50.0,
    }
    opts.update(cfg_extra)
    eng = LiveTradingEngine.__new__(LiveTradingEngine)
    eng.broker = broker
    eng.live_config = SimpleNamespace(**opts)
    eng.strategy_config = strategy_cfg
    eng.pattern_type = pattern_type
    eng._broker_lock = MagicMock()
    eng._broker_lock.__enter__ = MagicMock(return_value=None)
    eng._broker_lock.__exit__ = MagicMock(return_value=False)
    eng.logs = []
    eng.log = lambda msg, *_a, **_k: eng.logs.append(msg)
    return eng


def _buy_sig(entry, await_limit_fill=False):
    c = logic.Candle("s", 2650, 2660, 2640, 2655)
    return logic.TradeSignal(
        direction=logic.TradeDirection.BUY,
        hammer_candle=c,
        entry_candle=logic.Candle("e", 2652, 2665, 2650, 2660),
        entry_price=entry,
        stop_loss=2639.7,
        risk=entry - 2639.7,
        rr_multiple=2.0,
        target=entry + 2.0 * (entry - 2639.7),
        timeframe="5m",
        await_limit_fill=await_limit_fill,
    )


def _warned(eng):
    return [m for m in eng.logs if "Entry Offset" in m and "NOT applied" in m]


def test_live_market_order_logs_offset_not_applied():
    _p, cfg = _strategy(HAMMER, entry_offset=0.3)
    eng = _engine("market", cfg)
    mode, *_rest = eng._resolve_live_order(_buy_sig(2652.3), "XAUUSD")
    assert mode == "market"
    assert len(_warned(eng)) == 1


def test_live_limit_entry_uses_offset_no_warning():
    _p, cfg = _strategy(HAMMER, entry_offset=0.3)
    eng = _engine("limit_entry", cfg)
    mode, limit_px, *_rest = eng._resolve_live_order(_buy_sig(2652.3), "XAUUSD")
    assert mode == "limit_entry" and limit_px == pytest.approx(2652.3)
    assert _warned(eng) == []


def test_live_limit_falls_back_to_market_logs_offset_not_applied():
    _p, cfg = _strategy(HAMMER, entry_offset=5.0)
    eng = _engine("limit_entry", cfg, max_entry_deviation_points=100.0)
    mode, *_rest = eng._resolve_live_order(_buy_sig(2657.0), "XAUUSD")
    assert mode == "market"
    assert any("max deviation" in m for m in _warned(eng))


def test_live_limit_offset_from_bid_ask_logs_offset_not_applied():
    _p, cfg = _strategy(HAMMER, entry_offset=0.3)
    eng = _engine("limit_offset", cfg)
    mode, *_rest = eng._resolve_live_order(_buy_sig(2652.3), "XAUUSD")
    assert mode == "limit_entry"
    assert len(_warned(eng)) == 1


def test_live_zero_offset_never_warns():
    _p, cfg = _strategy(HAMMER, entry_offset=0, inverted_entry_offset=0)
    for mode in ("market", "limit_entry", "limit_offset"):
        eng = _engine(mode, cfg)
        eng._resolve_live_order(_buy_sig(2652.0), "XAUUSD")
        assert _warned(eng) == []


def test_live_hwc35_market_logs_offset_not_applied_limit_does_not():
    ptype, cfg = _strategy(HWC35, entry_offset=0.3)
    eng = _engine("market", cfg, pattern_type=ptype)
    mode, *_rest = eng._resolve_live_order(_buy_sig(2648.0, await_limit_fill=True), "XAUUSD")
    assert mode == "market" and len(_warned(eng)) == 1
    eng = _engine("limit_entry", cfg, pattern_type=ptype)
    mode, *_rest = eng._resolve_live_order(_buy_sig(2648.0, await_limit_fill=True), "XAUUSD")
    assert mode == "limit_entry" and _warned(eng) == []
