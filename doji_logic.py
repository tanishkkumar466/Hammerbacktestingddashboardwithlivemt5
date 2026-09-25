"""
Compatibility shim — Doji pattern engine.

Real module: strategy.doji_logic
Do not add pattern rules here; edit strategy/doji_logic.py.
"""
from strategy.doji_logic import *  # noqa: F401,F403
from strategy import doji_logic as _impl
import sys

sys.modules[__name__] = _impl
