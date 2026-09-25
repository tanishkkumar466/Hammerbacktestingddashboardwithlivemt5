"""
hammer_context_logic.py (compatibility façade)
==============================================
Keeps old import paths working.

Real product modules (separate strategies):
  strategy/hammer_with_candles_logic.py
  strategy/hammer_with_candles_35_logic.py

Shared detection engine:
  strategy/hammer_context_core.py
"""

from __future__ import annotations

import copy
from typing import Optional

from . import hammer_context_core as core
from . import hammer_with_candles_35_logic as hwc35
from . import hammer_with_candles_logic as hwc

# ---- Product identity (both strategies) ----
PATTERN_LABEL = hwc.PATTERN_LABEL
PATTERN_LABEL_35 = hwc35.PATTERN_LABEL
PATTERN_TYPE = hwc.PATTERN_TYPE
PATTERN_TYPE_35 = hwc35.PATTERN_TYPE
DEFAULT_PULLBACK_PCT_35 = hwc35.DEFAULT_PULLBACK_PCT

CONTEXT_PATTERN_LABELS = frozenset({PATTERN_LABEL, PATTERN_LABEL_35})
CONTEXT_PATTERN_TYPES = frozenset({PATTERN_TYPE, "hammer_context", PATTERN_TYPE_35})

# ---- Shared core re-exports ----
HammerContextConfig = core.HammerContextConfig
body_pct_of_candle = core.body_pct_of_candle
body_only_max_pct = core.body_only_max_pct
body_only_ok = core.body_only_ok
previous_candle_direction = core.previous_candle_direction
describe_hammer_context_rules = core.describe_hammer_context_rules
describe_hammer_context_entry_exit = core.describe_hammer_context_entry_exit
pullback_entry_price = core.pullback_entry_price
find_limit_fill_index = core.find_limit_fill_index
prior_closes_ok_for_buy = core.prior_closes_ok_for_buy
prior_closes_ok_for_sell = core.prior_closes_ok_for_sell
detect_buy_setup = core.detect_buy_setup
detect_sell_setup = core.detect_sell_setup
build_context_signal = core.build_context_signal


def is_context_pattern_label(pattern: str) -> bool:
    return str(pattern or "") in CONTEXT_PATTERN_LABELS


def is_context_pattern_type(pattern_type: str) -> bool:
    return str(pattern_type or "") in CONTEXT_PATTERN_TYPES


def is_35_pattern_label(pattern: str) -> bool:
    return hwc35.is_pattern_label(pattern)


def is_35_pattern_type(pattern_type: str) -> bool:
    return hwc35.is_pattern_type(pattern_type)


def pattern_type_for_label(pattern: str) -> str:
    if hwc35.is_pattern_label(pattern):
        return hwc35.PATTERN_TYPE
    if hwc.is_pattern_label(pattern) or is_context_pattern_label(pattern):
        return hwc.PATTERN_TYPE
    return hwc.PATTERN_TYPE


def resolve_entry_pullback_pct(
    raw_pct: object,
    *,
    for_35_pattern: bool,
    default_35: float = DEFAULT_PULLBACK_PCT_35,
) -> float:
    if not for_35_pattern:
        return 0.0
    return hwc35.resolve_entry_pullback_pct(raw_pct, default=default_35)


def apply_pullback_for_pattern_type(
    config: HammerContextConfig,
    pattern_type: str,
) -> HammerContextConfig:
    """
    Route to the correct product prepare_config.

    Plain HWC → pullback forced 0.
    35% → pullback resolved (default 35).
    """
    if hwc35.is_pattern_type(pattern_type):
        return hwc35.prepare_config(config)
    if hwc.is_pattern_type(pattern_type) or is_context_pattern_type(pattern_type):
        return hwc.prepare_config(config)
    return config


def run_strategy(candles, timeframe: str, config: Optional[HammerContextConfig] = None):
    """
    Legacy entry point. Prefer product modules:
      hammer_with_candles_logic.run_strategy
      hammer_with_candles_35_logic.run_strategy
    """
    # Without an explicit pattern_type, honor config.entry_pullback_pct as-is
    # (core behavior). Callers that know the product should use that module.
    return core.run_strategy(candles, timeframe, config)
