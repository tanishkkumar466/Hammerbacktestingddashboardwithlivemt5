"""
Compatibility shim — Classic / inverted Hammer engine.

Real module: strategy.logic
Do not add pattern rules here; edit strategy/logic.py.
"""
from strategy.logic import *  # noqa: F401,F403
from strategy import logic as _impl
import sys

sys.modules[__name__] = _impl
