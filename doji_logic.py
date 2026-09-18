"""
doji_logic.py
=============
Doji Candle Detection + Entry / SL / Target Strategy Engine

Separate from logic.py (hammer) so each pattern's rules stay in its own
file. Shared primitives (Candle, TradeSignal, enums, timeframe defaults)
are imported from logic.py -- only doji-specific detection and config
live here.

DOJI DEFINITION (parameterized):
    A doji is a candle whose body is very small relative to its total
    range (open ≈ close). Wick requirements depend on doji_style:

        ANY         -- small body only; wicks unrestricted
        CLASSIC     -- small body + both upper and lower wicks present
        DRAGONFLY   -- small body at top, long lower wick (bullish bias)
        GRAVESTONE  -- small body at bottom, long upper wick (bearish bias)
        LONG_LEGGED -- small body + both wicks are long

DIRECTION RULE (configurable via doji_direction_mode):
    Doji candles often have little body, so direction is resolved separately:
        WICK_BIAS          -- longer lower wick -> BUY, longer upper -> SELL
        CANDLE_COLOR       -- green doji -> green_direction; red -> red_direction
                               (with allow_green_trades / allow_red_trades gates)
        FIXED_BUY / FIXED_SELL
        NEXT_CANDLE_COLOR  -- green next candle -> BUY, red -> SELL
    When wick bias is tied, fallback_direction is used.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import logic


# ============================================================================
# SECTION 1: DOJI-SPECIFIC ENUMS
# ============================================================================

class DojiStyle(Enum):
    """Which doji sub-type must match for a valid signal."""
    ANY = "ANY"
    CLASSIC = "CLASSIC"
    DRAGONFLY = "DRAGONFLY"
    GRAVESTONE = "GRAVESTONE"
    LONG_LEGGED = "LONG_LEGGED"


class DojiDirectionMode(Enum):
    """How trade direction is chosen when the signal candle is a doji."""
    WICK_BIAS = "WICK_BIAS"
    CANDLE_COLOR = "CANDLE_COLOR"   # green/red on the doji candle (classic doji style)
    FIXED_BUY = "FIXED_BUY"
    FIXED_SELL = "FIXED_SELL"
    NEXT_CANDLE_COLOR = "NEXT_CANDLE_COLOR"


# ============================================================================
# SECTION 2: DOJI SHAPE CONFIG
# ============================================================================

@dataclass
class DojiRatioConfig:
    """
    Percentage rules for validating a doji's SHAPE.
    All percentages are relative to the candle's total range (high - low).

    max_body_pct / max_body_tol :
        Body must be <= max_body_pct + max_body_tol (small body rule).

    min_upper_wick_pct / min_lower_wick_pct / min_wick_tol :
        Minimum wick sizes for CLASSIC and LONG_LEGGED styles
        (effective minimum = target - min_wick_tol).

    dominant_wick_pct / dominant_wick_tol :
        For DRAGONFLY / GRAVESTONE -- the long wick must be at least
        dominant_wick_pct - dominant_wick_tol % of range.

    small_wick_pct / small_wick_tol :
        For DRAGONFLY / GRAVESTONE -- the short wick must be at most
        small_wick_pct + small_wick_tol % of range.

    wick_bias_threshold_pct :
        Minimum difference between upper and lower wick % (in range
        points) before WICK_BIAS mode picks a side; below this the
        fallback_direction is used.
    """
    max_body_pct: float = 5.0
    max_body_tol: float = 3.0

    min_upper_wick_pct: float = 15.0
    min_lower_wick_pct: float = 15.0
    min_wick_tol: float = 10.0

    dominant_wick_pct: float = 60.0
    dominant_wick_tol: float = 18.0

    small_wick_pct: float = 20.0
    small_wick_tol: float = 18.0

    wick_bias_threshold_pct: float = 5.0

    def max_body_upper_bound(self) -> float:
        return min(100.0, self.max_body_pct + self.max_body_tol)

    def min_upper_wick_lower_bound(self) -> float:
        return max(0.0, self.min_upper_wick_pct - self.min_wick_tol)

    def min_lower_wick_lower_bound(self) -> float:
        return max(0.0, self.min_lower_wick_pct - self.min_wick_tol)

    def dominant_wick_lower_bound(self) -> float:
        return max(0.0, self.dominant_wick_pct - self.dominant_wick_tol)

    def small_wick_upper_bound(self) -> float:
        return min(100.0, self.small_wick_pct + self.small_wick_tol)


# ============================================================================
# SECTION 3: DOJI STRATEGY CONFIG
# ============================================================================

@dataclass
class DojiStrategyConfig:
    """Top-level config for the doji pattern -- every tunable field lives here."""
    doji_ratios: DojiRatioConfig = field(default_factory=DojiRatioConfig)
    doji_style: DojiStyle = DojiStyle.ANY
    doji_direction_mode: DojiDirectionMode = DojiDirectionMode.WICK_BIAS
    fallback_direction: logic.TradeDirection = logic.TradeDirection.BUY

    green_direction: logic.TradeDirection = logic.TradeDirection.BUY
    red_direction: logic.TradeDirection = logic.TradeDirection.SELL
    allow_green_trades: bool = True
    allow_red_trades: bool = True

    entry_rule: logic.EntryRule = logic.EntryRule.NEXT_CANDLE_OPEN
    entry_offset: float = 0.0

    sl_mode: logic.StopLossMode = logic.StopLossMode.CANDLE_EXTREME
    sl_fixed_distance: float = 5.0
    buffer_mode: logic.BufferMode = logic.BufferMode.PERCENT_OF_RANGE
    sl_buffer_pct: float = 5.0
    sl_buffer_flat: float = 0.0

    timeframe_settings: Dict[str, logic.TimeframeSetting] = field(
        default_factory=lambda: dict(logic.DEFAULT_TIMEFRAME_SETTINGS)
    )
    enable_risk_limit: bool = True
    reject_zero_or_negative_risk: bool = True

    min_range: float = 1e-9


def describe_doji_detection(config: DojiStrategyConfig) -> str:
    """Human-readable doji rules summary (backtest, live log, preview)."""
    style = config.doji_style
    mode = config.doji_direction_mode
    if isinstance(style, DojiStyle):
        style = style.value
    if isinstance(mode, DojiDirectionMode):
        mode = mode.value
    return f"Doji style {style} · direction mode {mode}"


def describe_doji_entry_exit(config: DojiStrategyConfig) -> str:
    rule = logic.coerce_entry_rule(config.entry_rule)
    sl_mode = logic.coerce_stop_loss_mode(getattr(config, "sl_mode", logic.StopLossMode.CANDLE_EXTREME))
    if sl_mode == logic.StopLossMode.FIXED_FROM_ENTRY:
        sl_txt = f"fixed ${float(config.sl_fixed_distance or 0):g} from entry"
    else:
        sl_txt = f"{config.buffer_mode.value} {config.sl_buffer_pct:g}%"
    return f"entry={rule.value} offset=${config.entry_offset:g} SL={sl_txt}"


def preview_candle_is_green(trade_side: logic.TradeDirection, config: DojiStrategyConfig) -> bool:
    """
    UI preview: candle color that would correspond to trade_side under current doji direction rules.
    Falls back to green=BUY / red=SELL for WICK_BIAS and similar modes.
    """
    mode = config.doji_direction_mode
    if not isinstance(mode, DojiDirectionMode):
        try:
            mode = DojiDirectionMode(str(mode))
        except ValueError:
            mode = DojiDirectionMode.WICK_BIAS

    if mode == DojiDirectionMode.CANDLE_COLOR:
        g = logic.coerce_trade_action(config.green_direction, logic.TradeAction.BUY)
        r = logic.coerce_trade_action(config.red_direction, logic.TradeAction.SELL)
        return logic.preview_candle_is_green(
            trade_side,
            logic.StrategyConfig(
                classic_green=g,
                classic_red=r,
                inverted_green=g,
                inverted_red=r,
            ),
        )
    if mode == DojiDirectionMode.FIXED_BUY:
        return trade_side == logic.TradeDirection.BUY
    if mode == DojiDirectionMode.FIXED_SELL:
        return trade_side == logic.TradeDirection.SELL
    return trade_side == logic.TradeDirection.BUY


# ============================================================================
# SECTION 4: RESULT STRUCTURE
# ============================================================================

@dataclass
class DojiResult:
    """Output of doji detection for a single candle."""
    candle: logic.Candle
    color: logic.CandleColor
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    is_valid_shape: bool
    direction: Optional[logic.TradeDirection]
    doji_style_matched: Optional[DojiStyle] = None


# ============================================================================
# SECTION 5: METRICS + SHAPE CHECK
# ============================================================================

def calculate_candle_metrics(candle: logic.Candle, config: DojiStrategyConfig):
    rng = candle.range_
    if rng <= config.min_range:
        return 0.0, 0.0, 0.0
    body_pct = (candle.body / rng) * 100.0
    upper_pct = (candle.upper_wick / rng) * 100.0
    lower_pct = (candle.lower_wick / rng) * 100.0
    return body_pct, upper_pct, lower_pct


def _matches_style(
    body_pct: float,
    upper_pct: float,
    lower_pct: float,
    style: DojiStyle,
    ratios: DojiRatioConfig,
) -> bool:
    """Returns True if the candle metrics satisfy the chosen doji_style."""
    if body_pct > ratios.max_body_upper_bound():
        return False

    if style == DojiStyle.ANY:
        return True

    if style == DojiStyle.CLASSIC:
        return (
            upper_pct >= ratios.min_upper_wick_lower_bound()
            and lower_pct >= ratios.min_lower_wick_lower_bound()
        )

    if style == DojiStyle.LONG_LEGGED:
        dom_lo = ratios.dominant_wick_lower_bound()
        return upper_pct >= dom_lo and lower_pct >= dom_lo

    if style == DojiStyle.DRAGONFLY:
        return (
            lower_pct >= ratios.dominant_wick_lower_bound()
            and upper_pct <= ratios.small_wick_upper_bound()
        )

    if style == DojiStyle.GRAVESTONE:
        return (
            upper_pct >= ratios.dominant_wick_lower_bound()
            and lower_pct <= ratios.small_wick_upper_bound()
        )

    return False


def resolve_doji_direction(
    doji_result: DojiResult,
    next_candle: logic.Candle,
    config: DojiStrategyConfig,
) -> Optional[logic.TradeDirection]:
    """
    Resolves trade direction for a valid doji shape.
    NEXT_CANDLE_COLOR needs the next candle; other modes use the doji itself.
    """
    mode = config.doji_direction_mode
    upper_pct = doji_result.upper_wick_pct
    lower_pct = doji_result.lower_wick_pct

    if mode == DojiDirectionMode.FIXED_BUY:
        return logic.TradeDirection.BUY
    if mode == DojiDirectionMode.FIXED_SELL:
        return logic.TradeDirection.SELL

    if mode == DojiDirectionMode.CANDLE_COLOR:
        candle = doji_result.candle
        if candle.is_green:
            if not config.allow_green_trades:
                return None
            return config.green_direction
        if candle.is_red:
            if not config.allow_red_trades:
                return None
            return config.red_direction
        return config.fallback_direction

    if mode == DojiDirectionMode.NEXT_CANDLE_COLOR:
        if next_candle.is_green:
            return logic.TradeDirection.BUY
        if next_candle.is_red:
            return logic.TradeDirection.SELL
        return config.fallback_direction

    # WICK_BIAS (default)
    diff = lower_pct - upper_pct
    threshold = config.doji_ratios.wick_bias_threshold_pct
    if diff > threshold:
        return logic.TradeDirection.BUY
    if diff < -threshold:
        return logic.TradeDirection.SELL
    return config.fallback_direction


def check_doji(candle: logic.Candle, config: DojiStrategyConfig) -> DojiResult:
    """
    Validates doji SHAPE for one candle. Direction is NOT finalized here
    when mode is NEXT_CANDLE_COLOR -- that happens in build_trade_signal.
    """
    body_pct, upper_pct, lower_pct = calculate_candle_metrics(candle, config)
    ratios = config.doji_ratios

    shape_ok = _matches_style(body_pct, upper_pct, lower_pct, config.doji_style, ratios)

    if candle.is_green:
        color = logic.CandleColor.GREEN
    elif candle.is_red:
        color = logic.CandleColor.RED
    else:
        color = logic.CandleColor.DOJI

    direction = None
    if shape_ok:
        if config.doji_direction_mode in (
            DojiDirectionMode.FIXED_BUY,
            DojiDirectionMode.FIXED_SELL,
            DojiDirectionMode.WICK_BIAS,
            DojiDirectionMode.CANDLE_COLOR,
        ):
            dummy_next = logic.Candle(candle.timestamp, candle.open, candle.high,
                                       candle.low, candle.close)
            direction = resolve_doji_direction(
                DojiResult(candle, color, body_pct, upper_pct, lower_pct, True, None),
                dummy_next,
                config,
            )

    return DojiResult(
        candle=candle,
        color=color,
        body_pct=body_pct,
        upper_wick_pct=upper_pct,
        lower_wick_pct=lower_pct,
        is_valid_shape=shape_ok,
        direction=direction,
        doji_style_matched=config.doji_style if shape_ok else None,
    )


# ============================================================================
# SECTION 6: ENTRY / SL / TARGET (same rules as hammer, doji candle as anchor)
# ============================================================================

def calculate_entry_price(
    signal_candle: logic.Candle,
    next_candle: logic.Candle,
    config: DojiStrategyConfig,
) -> float:
    rule = config.entry_rule
    if rule == logic.EntryRule.NEXT_CANDLE_OPEN:
        base = next_candle.open
    elif rule == logic.EntryRule.NEXT_CANDLE_CLOSE:
        base = next_candle.close
    elif rule == logic.EntryRule.HAMMER_CLOSE:
        base = signal_candle.close
    elif rule == logic.EntryRule.HAMMER_HIGH:
        base = signal_candle.high
    elif rule == logic.EntryRule.HAMMER_LOW:
        base = signal_candle.low
    else:
        raise ValueError(f"Unknown entry_rule: {rule}")
    return base + config.entry_offset


def calculate_stop_loss(
    signal_candle: logic.Candle,
    direction: logic.TradeDirection,
    config: DojiStrategyConfig,
    entry_price: Optional[float] = None,
) -> float:
    sl_mode = logic.coerce_stop_loss_mode(getattr(config, "sl_mode", logic.StopLossMode.CANDLE_EXTREME))
    if sl_mode == logic.StopLossMode.FIXED_FROM_ENTRY:
        base = entry_price if entry_price is not None else signal_candle.close
        dist = max(0.0, float(getattr(config, "sl_fixed_distance", 0.0) or 0.0))
        if direction == logic.TradeDirection.BUY:
            return base - dist
        return base + dist

    anchor = signal_candle.low if direction == logic.TradeDirection.BUY else signal_candle.high

    if config.buffer_mode == logic.BufferMode.PERCENT_OF_RANGE:
        buffer_amount = signal_candle.range_ * (config.sl_buffer_pct / 100.0)
    elif config.buffer_mode == logic.BufferMode.PERCENT_OF_PRICE:
        buffer_amount = anchor * (config.sl_buffer_pct / 100.0)
    elif config.buffer_mode == logic.BufferMode.FLAT_AMOUNT:
        buffer_amount = config.sl_buffer_flat
    elif config.buffer_mode == logic.BufferMode.NONE:
        buffer_amount = 0.0
    else:
        raise ValueError(f"Unknown buffer_mode: {config.buffer_mode}")

    if direction == logic.TradeDirection.BUY:
        return anchor - buffer_amount
    return anchor + buffer_amount


def build_trade_signal(
    doji_result: DojiResult,
    next_candle: logic.Candle,
    timeframe: str,
    config: DojiStrategyConfig,
) -> Optional[logic.TradeSignal]:
    if not doji_result.is_valid_shape:
        return None

    direction = doji_result.direction
    if config.doji_direction_mode == DojiDirectionMode.NEXT_CANDLE_COLOR:
        direction = resolve_doji_direction(doji_result, next_candle, config)

    if direction is None:
        return None

    tf_setting = logic.resolve_timeframe_setting(config.timeframe_settings, timeframe)
    signal_candle = doji_result.candle

    entry_price = calculate_entry_price(signal_candle, next_candle, config)
    stop_loss = calculate_stop_loss(signal_candle, direction, config, entry_price=entry_price)

    if direction == logic.TradeDirection.BUY:
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price

    ignored = False
    ignore_reason = None

    if config.reject_zero_or_negative_risk and risk <= 0:
        ignored = True
        ignore_reason = "Risk is zero or negative -- invalid setup (check entry vs SL)."
    elif config.enable_risk_limit and risk > tf_setting.max_sl_usd:
        ignored = True
        ignore_reason = (
            f"Risk (${risk:.2f}) exceeds max allowed SL "
            f"(${tf_setting.max_sl_usd}) for timeframe '{timeframe}'."
        )

    reward = risk * tf_setting.rr_multiple
    if direction == logic.TradeDirection.BUY:
        target = entry_price + reward
    else:
        target = entry_price - reward

    return logic.TradeSignal(
        direction=direction,
        hammer_candle=signal_candle,
        entry_candle=next_candle,
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk=risk,
        rr_multiple=tf_setting.rr_multiple,
        target=target,
        timeframe=timeframe,
        ignored=ignored,
        ignore_reason=ignore_reason,
        pattern_variant=(
            "CLASSIC" if direction == logic.TradeDirection.BUY else "INVERTED"
        ),
    )


# ============================================================================
# SECTION 7: FULL PIPELINE
# ============================================================================

def run_strategy(
    candles: List[logic.Candle],
    timeframe: str,
    config: Optional[DojiStrategyConfig] = None,
) -> List[logic.TradeSignal]:
    """Runs doji detection + signal building over a candle sequence."""
    if config is None:
        config = DojiStrategyConfig()

    signals: List[logic.TradeSignal] = []

    for i in range(len(candles) - 1):
        current = candles[i]
        next_candle = candles[i + 1]

        doji_result = check_doji(current, config)
        if doji_result.is_valid_shape:
            signal = build_trade_signal(
                doji_result=doji_result,
                next_candle=next_candle,
                timeframe=timeframe,
                config=config,
            )
            if signal is not None:
                signals.append(signal)

    return signals


# ============================================================================
# SECTION 8: DEMO
# ============================================================================

if __name__ == "__main__":
    sample = [
        logic.Candle("09:00", 100, 101, 99, 100.5),
        logic.Candle("10:00", 50, 50.3, 45, 50.1),   # classic doji-ish
        logic.Candle("11:00", 50.2, 52, 49, 51.5),   # entry candle
        logic.Candle("12:00", 51, 53, 50, 52),
        logic.Candle("13:00", 60, 60.2, 52, 60.05),  # dragonfly doji
        logic.Candle("14:00", 60.1, 61, 58, 59),
    ]

    cfg = DojiStrategyConfig(
        doji_style=DojiStyle.ANY,
        doji_direction_mode=DojiDirectionMode.WICK_BIAS,
    )
    cfg.timeframe_settings["1h"] = logic.TimeframeSetting(rr_multiple=1.6, max_sl_usd=18)

    print("=" * 70)
    print("DOJI STRATEGY -- default config")
    print("=" * 70)
    for r in run_strategy(sample, timeframe="1h", config=cfg):
        status = "IGNORED" if r.ignored else "TAKEN"
        print(
            f"[{status}] {r.direction.value:<4} Entry={r.entry_price:.2f} "
            f"SL={r.stop_loss:.2f} Risk={r.risk:.2f} Target={r.target:.2f}"
            + (f"  | {r.ignore_reason}" if r.ignored else "")
        )
