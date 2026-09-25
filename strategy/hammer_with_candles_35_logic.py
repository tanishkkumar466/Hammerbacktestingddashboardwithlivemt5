"""
hammer_with_candles_35_logic.py
===============================
Product pattern: **Hammer with candle 35%**

Same context detection as Hammer with candles, but entry waits for a
pullback toward SL (default 35% of |entry−SL|). TP stays on signal entry × RR.

This is a separate strategy from plain Hammer with candles — do not share
exit/entry special-cases with that module (see workspace pattern rule).
"""

from __future__ import annotations

import copy
from typing import List, Optional

from . import hammer_context_core as core
from . import logic

PATTERN_LABEL = "Hammer with candle 35%"
PATTERN_TYPE = "hammer_with_candles_35"
DEFAULT_PULLBACK_PCT = 35.0

HammerContextConfig = core.HammerContextConfig

# Shared fill helper used by backtest/live for await_limit_fill signals
pullback_entry_price = core.pullback_entry_price
find_limit_fill_index = core.find_limit_fill_index


def is_pattern_label(pattern: str) -> bool:
    return str(pattern or "") == PATTERN_LABEL


def is_pattern_type(pattern_type: str) -> bool:
    return str(pattern_type or "") == PATTERN_TYPE


def resolve_entry_pullback_pct(
    raw_pct: object,
    *,
    default: float = DEFAULT_PULLBACK_PCT,
) -> float:
    """
    Pullback % of |signal_entry − SL| toward the stop.

    Missing / blank / unparseable → default 35.
    Explicit 0 is honored: no pullback, market entry — identical to plain
    Hammer with candles on the same data/config.
    """
    if raw_pct is None or (isinstance(raw_pct, str) and not raw_pct.strip()):
        return float(default)
    try:
        pct = float(raw_pct)
    except (TypeError, ValueError):
        return float(default)
    if pct != pct:  # NaN
        return float(default)
    return max(0.0, min(100.0, pct))


def prepare_config(config: Optional[HammerContextConfig] = None) -> HammerContextConfig:
    """Resolve pullback for this product (copy when changing). 0 stays 0."""
    if config is None:
        config = HammerContextConfig(entry_pullback_pct=DEFAULT_PULLBACK_PCT)
    resolved = resolve_entry_pullback_pct(getattr(config, "entry_pullback_pct", None))
    current = float(getattr(config, "entry_pullback_pct", 0.0) or 0.0)
    if abs(current - resolved) < 1e-12:
        return config
    out = copy.copy(config)
    out.entry_pullback_pct = resolved
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


body_pct_of_candle = core.body_pct_of_candle
detect_buy_setup = core.detect_buy_setup
detect_sell_setup = core.detect_sell_setup
build_context_signal = core.build_context_signal
