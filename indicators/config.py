from dataclasses import dataclass, field
from enum import Enum
from typing import List


class IndicatorCombineMode(Enum):
    """How multiple enabled indicator filters combine."""
    ALL = "ALL"   # every enabled filter must pass (AND)
    ANY = "ANY"   # at least one enabled filter must pass (OR)


@dataclass
class SuperTrendConfig:
    enabled: bool = False
    atr_period: int = 10
    multiplier: float = 3.0
    # Filter: classic BUY requires bullish ST + close above ST line;
    # inverted SELL requires bearish ST + close below ST line.
    apply_trade_filter: bool = True


@dataclass
class VWAPConfig:
    enabled: bool = False
    # Filter uses hammer variant + direction (see filter.py).
    apply_trade_filter: bool = True


@dataclass
class IndicatorStackConfig:
    combine_mode: IndicatorCombineMode = IndicatorCombineMode.ALL
    supertrend: SuperTrendConfig = field(default_factory=SuperTrendConfig)
    vwap: VWAPConfig = field(default_factory=VWAPConfig)

    def enabled_indicator_ids(self) -> List[str]:
        ids = []
        if self.supertrend.enabled:
            ids.append("supertrend")
        if self.vwap.enabled:
            ids.append("vwap")
        return ids
