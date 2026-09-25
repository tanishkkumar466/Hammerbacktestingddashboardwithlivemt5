"""
Compatibility shim — Hammer with candles (market entry).

Real module: strategy.hammer_with_candles_logic
"""
from strategy.hammer_with_candles_logic import *  # noqa: F401,F403
from strategy import hammer_with_candles_logic as _impl
import sys

sys.modules[__name__] = _impl
