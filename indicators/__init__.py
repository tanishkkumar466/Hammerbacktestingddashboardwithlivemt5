"""
Extensible indicator stack for hammer/doji backtests and dashboard previews.

Add new indicators by:
  1. Creating a module with compute_*() and default config dataclass
  2. Registering in INDICATOR_REGISTRY
  3. Adding filter rules in filter.py (or indicator-specific filter fn)
"""

from indicators.config import IndicatorStackConfig, SuperTrendConfig, VWAPConfig
from indicators.filter import apply_indicator_filters
from indicators.registry import INDICATOR_REGISTRY, list_indicator_ids

__all__ = [
    "IndicatorStackConfig",
    "SuperTrendConfig",
    "VWAPConfig",
    "apply_indicator_filters",
    "INDICATOR_REGISTRY",
    "list_indicator_ids",
]
