"""
Compatibility shim — Hammer with candle 35% (pullback entry).

Real module: strategy.hammer_with_candles_35_logic
"""
from strategy.hammer_with_candles_35_logic import *  # noqa: F401,F403
from strategy import hammer_with_candles_35_logic as _impl
import sys

sys.modules[__name__] = _impl
