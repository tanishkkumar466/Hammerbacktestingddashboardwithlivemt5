"""
hammer_with_candles_logic.py
============================
Product pattern: **Hammer with candles**

Market entry at the configured entry rule (next open, etc.).
Never uses pullback / await_limit_fill — that is a different strategy
(see hammer_with_candles_35_logic.py).

Detection + SL/TP builders live in hammer_context_core.py.
"""

from __future__ import annotations

import copy
from typing import List, Optional

from . import hammer_context_core as core
from . import logic

PATTERN_LABEL = "Hammer with candles"
PATTERN_TYPE = "hammer_with_candles"
# Legacy type string still seen in old presets / runs
_LEGACY_TYPES = frozenset({PATTERN_TYPE, "hammer_context"})

HammerContextConfig = core.HammerContextConfig


def is_pattern_label(pattern: str) -> bool:
    return str(pattern or "") == PATTERN_LABEL


def is_pattern_type(pattern_type: str) -> bool:
    return str(pattern_type or "") in _LEGACY_TYPES


def prepare_config(config: Optional[HammerContextConfig] = None) -> HammerContextConfig:
    """Force pullback off so this product never waits for a limit fill."""
    if config is None:
        config = HammerContextConfig()
    current = float(getattr(config, "entry_pullback_pct", 0.0) or 0.0)
    if abs(current) < 1e-12:
        return config
    out = copy.copy(config)
    out.entry_pullback_pct = 0.0
    return out


def describe_rules(config: HammerContextConfig) -> str:
    return core.describe_hammer_context_rules(config)


def describe_entry_exit(config: HammerContextConfig) -> str:
    cfg = prepare_config(config)
    return core.describe_hammer_context_entry_exit(cfg)


def run_strategy(
    candles: List[logic.Candle],
    timeframe: str,
    config: Optional[HammerContextConfig] = None,
) -> List[logic.TradeSignal]:
    return core.run_strategy(candles, timeframe, prepare_config(config))


# Re-export helpers callers already use for this family
body_pct_of_candle = core.body_pct_of_candle
detect_buy_setup = core.detect_buy_setup
detect_sell_setup = core.detect_sell_setup
build_context_signal = core.build_context_signal
