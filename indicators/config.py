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
class RollingVWAPConfig:
    enabled: bool = False
    period: int = 20
    apply_trade_filter: bool = True


@dataclass
class RSIConfig:
    enabled: bool = False
    period: int = 14
    buy_above: float = 50.0
    sell_below: float = 60.0
    apply_trade_filter: bool = True


@dataclass
class IndicatorStackConfig:
    combine_mode: IndicatorCombineMode = IndicatorCombineMode.ALL
    supertrend: SuperTrendConfig = field(default_factory=SuperTrendConfig)
    vwap: VWAPConfig = field(default_factory=VWAPConfig)
    rolling_vwap: RollingVWAPConfig = field(default_factory=RollingVWAPConfig)
    rsi: RSIConfig = field(default_factory=RSIConfig)

    def enabled_indicator_ids(self) -> List[str]:
        ids = []
        if self.supertrend.enabled:
            ids.append("supertrend")
        if self.vwap.enabled:
            ids.append("vwap")
        if self.rolling_vwap.enabled:
            ids.append("rolling_vwap")
        if self.rsi.enabled:
            ids.append("rsi")
        return ids
