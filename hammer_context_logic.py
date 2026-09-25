"""
Compatibility shim — Hammer with candles / 35% engine.

Real module: strategy.hammer_context_logic
Do not add pattern rules here; edit strategy/hammer_context_logic.py.
"""
from strategy.hammer_context_logic import *  # noqa: F401,F403
from strategy import hammer_context_logic as _impl
import sys

sys.modules[__name__] = _impl
