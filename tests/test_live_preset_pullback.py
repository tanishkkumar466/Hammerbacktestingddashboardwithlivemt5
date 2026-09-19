"""Live path: preset.json for Hammer with candle 35% must carry pullback %."""

from __future__ import annotations

import json
from pathlib import Path

import logic
import hammer_context_logic as hc
from hammer_context_logic import build_context_signal, pullback_entry_price
from live import reanchor_sl_tp_to_fill
from logic import Candle, TimeframeSetting, TradeDirection

ROOT = Path(__file__).resolve().parents[1]
PRESET = ROOT / "template" / "preset_example_hammer_with_candle_35.json"


def _load_preset() -> dict:
    with open(PRESET, encoding="utf-8") as f:
        return json.load(f)


def _cfg_from_preset_fields(data: dict) -> hc.HammerContextConfig:
    fields = data.get("fields") or {}
    pattern = data.get("pattern", "")
    for_35 = hc.is_35_pattern_label(pattern)
    raw = fields.get("entry_pullback_pct")
    try:
        raw_f = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        raw_f = None
    pull = hc.resolve_entry_pullback_pct(raw_f, for_35_pattern=for_35)
    return hc.HammerContextConfig(
        entry_pullback_pct=pull,
        lookback_candles=int(float(fields.get("context_lookback_candles") or 5)),
        enable_buy=True,
        enable_sell=True,
        buffer_mode=logic.BufferMode.NONE,
        sl_buffer_pct=0.0,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )


def test_35_preset_json_has_pullback_field():
    data = _load_preset()
    assert data["pattern"] == hc.PATTERN_LABEL_35
    fields = data.get("fields") or {}
    assert float(fields.get("entry_pullback_pct")) == 35.0


def test_preset_builds_await_limit_signal_for_live():
    """Same idea as live after loading a slot preset.json into Parameters."""
    data = _load_preset()
    data = dict(data)
    fields = dict(data.get("fields") or {})
    fields["entry_pullback_pct"] = "40"
    data["fields"] = fields

    cfg = _cfg_from_preset_fields(data)
    cfg = hc.apply_pullback_for_pattern_type(cfg, hc.pattern_type_for_label(data["pattern"]))
    assert cfg.entry_pullback_pct == 40.0

    signal = Candle("s", 4005.0, 4012.0, 4000.0, 4008.0)
    nxt = Candle("n", 4010.0, 4015.0, 4007.0, 4012.0)
    sig = build_context_signal(TradeDirection.BUY, signal, nxt, "3m", cfg)
    assert sig is not None and not sig.ignored
    assert sig.await_limit_fill is True
    assert abs(sig.entry_price - 4006.0) < 1e-9
    assert abs(float(sig.signal_entry_price) - 4010.0) < 1e-9
    assert abs(sig.target - 4030.0) < 1e-9

    sl, tp = reanchor_sl_tp_to_fill(sig, fill_price=4005.5)
    assert sl == 4000.0 and tp == 4030.0


def test_live_apply_pullback_forces_35_when_preset_missing_pct():
    cfg = hc.HammerContextConfig(entry_pullback_pct=0.0)
    out = hc.apply_pullback_for_pattern_type(cfg, hc.PATTERN_TYPE_35)
    assert out.entry_pullback_pct == 35.0
    buy_e, _ = pullback_entry_price(TradeDirection.BUY, 4010.0, 4000.0, out.entry_pullback_pct)
    assert abs(buy_e - 4006.5) < 1e-9
