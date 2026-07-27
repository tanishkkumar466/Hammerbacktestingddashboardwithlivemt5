"""
logic.py
========
Hammer Candle Detection + Entry / SL / Target Strategy Engine
FULLY PARAMETERIZED VERSION -- every rule, threshold, and mode used anywhere
in this file is a named field inside StrategyConfig (Section 2). Nothing
that affects a trading decision is hardcoded inside a function body.
Change a config field -> behavior changes. That's it.

DIRECTION RULE:
    GREEN (bullish, close > open) hammer -> BUY
    RED   (bearish, close < open) hammer -> SELL
    (This mapping itself is also a config switch -- see `color_direction_map`
    if you ever want to flip it or test the opposite mapping.)

--------------------------------------------------------------------------
STEP-BY-STEP LOGIC (matches your numbered steps + flowchart)
--------------------------------------------------------------------------
STEP 1  - Take each candle's OHLC data (Open, High, Low, Close).

STEP 2  - Determine hammer candle shape dynamically using 3 editable bands:
              Dominant wick : target % of range (default 60, tol +/-18)
              Body          : target % of range (default 20, tol up to 25)
              Small wick    : target % of range (default 20, tol +/-18)
          ALL SIX numbers (3 targets + 3 tolerances) are separate editable
          fields. Which wick (upper/lower) is treated as "dominant" is
          ALSO a config switch (hammer_wick_side).

STEP 3  - Entry point is configurable (entry_rule): default = next candle's
          OPEN, but you can switch to hammer's CLOSE, or hammer's HIGH/LOW,
          or a custom offset, to test alternate entry timing.

STEP 4  - Stop Loss = hammer LOW (BUY) / HIGH (SELL), adjusted by a Buffer.
          Buffer MODE is configurable: percent-of-range, percent-of-price,
          or a flat $ amount. Buffer SIZE is also editable per mode.

STEP 5  - Target = Risk x RR multiple. RR multiple is set per timeframe
          (fully editable dict), following the Golden Ratio family
          (1.6x - 2.4x) by default.

STEP 6  - Max $ risk allowed is set per timeframe (fully editable dict).
          If risk > max -> ignore trade. This check itself can be turned
          off (enable_risk_limit=False) if you want to see ALL signals
          including the ones that would normally be filtered out, useful
          for testing/backtesting every possibility.

--------------------------------------------------------------------------
FLOWCHART (unchanged, exactly as given)
--------------------------------------------------------------------------
    New candle closes
          |
    Calculate Body / Upper Wick / Lower Wick
          |
    Check Hammer
          |
    Valid? --NO--> (do nothing, move to next candle)
          |
         YES
          |
    Wait for next candle
          |
    Entry = Next Candle Open   (configurable, see entry_rule)
          |
    SL = Hammer Low (BUY) / Hammer High (SELL) +/- Buffer  (buffer configurable)
          |
    Risk = |Entry - SL|
          |
    Check Risk Limit           (can be disabled for testing)
          |
    Risk > Max? --YES--> Ignore Trade
          |
         NO
          |
    Target = Risk x RR
          |
    Place BUY Order (GREEN hammer)  /  Place SELL Order (RED hammer)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List


# ============================================================================
# SECTION 1: CANDLE DATA STRUCTURE
# ============================================================================

@dataclass
class Candle:
    """
    A single OHLC candle.
    timestamp : any identifier (datetime, string, index...)
    open, high, low, close : prices
    """
    timestamp: object
    open: float
    high: float
    low: float
    close: float

    @property
    def range_(self) -> float:
        """Total candle range = High - Low. Base used for every % calc."""
        return self.high - self.low

    @property
    def body(self) -> float:
        """Absolute size of the candle body = |Close - Open|."""
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        """Distance from top of body to the High."""
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        """Distance from the Low to the bottom of body."""
        return min(self.open, self.close) - self.low

    @property
    def is_green(self) -> bool:
        """GREEN / bullish candle -> Close > Open."""
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        """RED / bearish candle -> Close < Open."""
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        """Close == Open (no color)."""
        return self.close == self.open


class CandleColor(Enum):
    GREEN = "GREEN"
    RED = "RED"
    DOJI = "DOJI"


class TradeDirection(Enum):
    BUY = "BUY"
    SELL = "SELL"


class WickSide(Enum):
    """Which wick is checked as the 'dominant' (long) wick for the hammer shape."""
    LOWER = "LOWER"   # classic hammer: long lower wick        (default)
    UPPER = "UPPER"   # inverted hammer / shooting star: long upper wick
    EITHER = "EITHER"  # pass if EITHER wick qualifies as dominant (most permissive)


class HammerVariant(Enum):
    CLASSIC = "CLASSIC"       # long lower wick hammer
    INVERTED = "INVERTED"     # long upper wick inverted hammer


class EntryRule(Enum):
    """Where the entry price is taken from -- fully swappable for testing."""
    NEXT_CANDLE_OPEN = "NEXT_CANDLE_OPEN"     # default, matches your flowchart
    HAMMER_CLOSE = "HAMMER_CLOSE"             # enter at the hammer's own close
    HAMMER_HIGH = "HAMMER_HIGH"               # enter at hammer's high (breakout style)
    HAMMER_LOW = "HAMMER_LOW"                 # enter at hammer's low
    NEXT_CANDLE_CLOSE = "NEXT_CANDLE_CLOSE"   # enter at next candle's close instead of open


class BufferMode(Enum):
    """How the SL buffer is measured/applied."""
    PERCENT_OF_RANGE = "PERCENT_OF_RANGE"   # buffer = hammer range * buffer_value/100  (default)
    PERCENT_OF_PRICE = "PERCENT_OF_PRICE"   # buffer = hammer low/high * buffer_value/100
    FLAT_AMOUNT = "FLAT_AMOUNT"             # buffer = buffer_value (a fixed $ amount)
    NONE = "NONE"                           # no buffer at all, SL = exact wick tip


# ============================================================================
# SECTION 2: MASTER CONFIG -- EVERY EDITABLE PARAMETER LIVES HERE
# ============================================================================

@dataclass
class HammerRatioConfig:
    """
    Dynamic percentage rules used to validate a hammer's SHAPE.
    All percentages are relative to the candle's TOTAL RANGE (high - low).
    Every target and every tolerance below is independently editable.

    dominant_wick_pct : ideal % of range for the LONG wick        (default 60)
    dominant_wick_tol : +/- tolerance around dominant_wick_pct    (default 18)
                        -> e.g. 60+/-18 passes from 42% to 78%

    body_pct          : ideal % of range for the body             (default 20)
    body_tol          : tolerance for body                        (default 25)
                        -> e.g. 20+/-25 (clipped at 0) passes from 0% to 45%

    small_wick_pct    : ideal % of range for the SHORT wick       (default 20)
    small_wick_tol    : +/- tolerance for the short wick          (default 18)
                        -> e.g. 20+/-18 passes from 2% to 38%

    You can also set body_tol_is_symmetric=False to give body an
    ASYMMETRIC tolerance (different lower vs upper bound), using
    body_tol_lower / body_tol_upper instead of the single body_tol.
    This lets you test e.g. "body must be between 5% and 45%" instead
    of a symmetric +/- band.
    """
    dominant_wick_pct: float = 60.0
    dominant_wick_tol: float = 18.0

    body_pct: float = 20.0
    body_tol: float = 25.0

    small_wick_pct: float = 20.0
    small_wick_tol: float = 18.0

    # ---- optional asymmetric override for body bounds ----
    body_tol_is_symmetric: bool = True
    body_tol_lower: float = 20.0   # only used if body_tol_is_symmetric=False
    body_tol_upper: float = 25.0   # only used if body_tol_is_symmetric=False

    # ---- optional asymmetric override for dominant wick bounds ----
    dominant_tol_is_symmetric: bool = True
    dominant_tol_lower: float = 18.0
    dominant_tol_upper: float = 18.0

    # ---- optional asymmetric override for small wick bounds ----
    small_tol_is_symmetric: bool = True
    small_tol_lower: float = 18.0
    small_tol_upper: float = 18.0

    def dominant_wick_bounds(self):
        if self.dominant_tol_is_symmetric:
            lo_tol = hi_tol = self.dominant_wick_tol
        else:
            lo_tol, hi_tol = self.dominant_tol_lower, self.dominant_tol_upper
        lo = max(0.0, self.dominant_wick_pct - lo_tol)
        hi = min(100.0, self.dominant_wick_pct + hi_tol)
        return lo, hi

    def body_bounds(self):
        if self.body_tol_is_symmetric:
            lo_tol = hi_tol = self.body_tol
        else:
            lo_tol, hi_tol = self.body_tol_lower, self.body_tol_upper
        lo = max(0.0, self.body_pct - lo_tol)
        hi = min(100.0, self.body_pct + hi_tol)
        return lo, hi

    def small_wick_bounds(self):
        if self.small_tol_is_symmetric:
            lo_tol = hi_tol = self.small_wick_tol
        else:
            lo_tol, hi_tol = self.small_tol_lower, self.small_tol_upper
        lo = max(0.0, self.small_wick_pct - lo_tol)
        hi = min(100.0, self.small_wick_pct + hi_tol)
        return lo, hi


@dataclass
class TimeframeSetting:
    """
    rr_multiple : Target multiple applied to risk (fully editable per timeframe)
    max_sl_usd  : Maximum $ risk allowed for this timeframe (fully editable)
    """
    rr_multiple: float
    max_sl_usd: float


# ---- Timeframe -> (Target Multiple, Max SL in $) --------------------------
# Add / edit / remove timeframes freely. This is just a starting point.
DEFAULT_TIMEFRAME_SETTINGS: Dict[str, TimeframeSetting] = {
    "1h":  TimeframeSetting(rr_multiple=1.6, max_sl_usd=18),
    "30m": TimeframeSetting(rr_multiple=1.6, max_sl_usd=15),
    "15m": TimeframeSetting(rr_multiple=2.0, max_sl_usd=15),
    "10m": TimeframeSetting(rr_multiple=2.0, max_sl_usd=15),
    "5m":  TimeframeSetting(rr_multiple=2.4, max_sl_usd=10),
    "3m":  TimeframeSetting(rr_multiple=2.4, max_sl_usd=8),
    # 1-minute added on request: fastest/noisiest timeframe, not part of
    # the original spec's RR/SL table, so these values are a reasonable
    # extrapolation of the existing pattern (higher RR + tighter max SL
    # as timeframes get faster) -- adjust freely, this is just a sane
    # starting point, not a tuned recommendation.
    "1m":  TimeframeSetting(rr_multiple=2.4, max_sl_usd=6),
}


@dataclass
class StrategyConfig:
    """
    Top-level config object. EVERYTHING that affects the trade decision
    logic is a field here -- change any field, nothing else needs editing.
    """
    # ---- shape detection ----
    hammer_ratios: HammerRatioConfig = field(default_factory=HammerRatioConfig)
    hammer_wick_side: WickSide = WickSide.LOWER   # which wick must be dominant

    # ---- hammer type toggles (classic vs inverted) ----
    enable_classic_hammer: bool = True
    enable_inverted_hammer: bool = True
    classic_hammer_allow_buy: bool = True
    classic_hammer_allow_sell: bool = True
    inverted_hammer_allow_buy: bool = True
    inverted_hammer_allow_sell: bool = True

    # ---- color -> direction mapping (swap if you ever want to test inverse) ----
    green_direction: TradeDirection = TradeDirection.BUY
    red_direction: TradeDirection = TradeDirection.SELL
    allow_doji_signals: bool = False   # if True, doji candles won't auto-fail shape check

    # ---- entry ----
    entry_rule: EntryRule = EntryRule.NEXT_CANDLE_OPEN
    entry_offset: float = 0.0   # optional flat $ nudge added to whatever entry_rule picks
                                 # (positive = higher entry, negative = lower entry)

    # ---- stop loss / buffer ----
    buffer_mode: BufferMode = BufferMode.PERCENT_OF_RANGE
    sl_buffer_pct: float = 5.0     # used when buffer_mode = PERCENT_OF_RANGE or PERCENT_OF_PRICE
    sl_buffer_flat: float = 0.0    # used when buffer_mode = FLAT_AMOUNT

    # ---- risk / target ----
    timeframe_settings: Dict[str, TimeframeSetting] = field(
        default_factory=lambda: dict(DEFAULT_TIMEFRAME_SETTINGS)
    )
    enable_risk_limit: bool = True   # set False to see ALL signals, even over-risk ones
    reject_zero_or_negative_risk: bool = True  # safety guard, can be disabled for testing

    # ---- misc / safety ----
    min_range: float = 1e-9   # floor to avoid divide-by-zero on flat candles


# ============================================================================
# SECTION 3: RESULT STRUCTURES
# ============================================================================

@dataclass
class HammerResult:
    """Output of the hammer-detection step for a single candle."""
    candle: Candle
    color: CandleColor
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    is_valid_shape: bool
    direction: Optional[TradeDirection]
    hammer_variant: Optional[HammerVariant] = None


@dataclass
class TradeSignal:
    """Final trade plan produced once entry conditions are confirmed."""
    direction: TradeDirection
    hammer_candle: Candle
    entry_candle: Candle
    entry_price: float
    stop_loss: float
    risk: float
    rr_multiple: float
    target: float
    timeframe: str
    ignored: bool = False
    ignore_reason: Optional[str] = None
    pattern_variant: Optional[str] = None  # CLASSIC / INVERTED for hammer (+ indicators)


# ============================================================================
# SECTION 4: STEP 1 & 2 -- CALCULATE METRICS AND CHECK HAMMER SHAPE
# ============================================================================

def calculate_candle_metrics(candle: Candle, config: StrategyConfig):
    """STEP 1 + first half of STEP 2: Body %, Upper Wick %, Lower Wick %."""
    rng = candle.range_
    if rng <= config.min_range:
        return 0.0, 0.0, 0.0  # flat candle, nothing to measure

    body_pct = (candle.body / rng) * 100.0
    upper_wick_pct = (candle.upper_wick / rng) * 100.0
    lower_wick_pct = (candle.lower_wick / rng) * 100.0
    return body_pct, upper_wick_pct, lower_wick_pct


def check_hammer(candle: Candle, config: StrategyConfig) -> HammerResult:
    """
    STEP 2: Validate hammer SHAPE using the fully editable wick/body bounds
    in config.hammer_ratios, and using config.hammer_wick_side to decide
    which wick must be the dominant one (LOWER / UPPER / EITHER).

    COLOR -> DIRECTION MAPPING (editable via config.green_direction /
    config.red_direction, default GREEN->BUY, RED->SELL).
    """
    body_pct, upper_pct, lower_pct = calculate_candle_metrics(candle, config)

    dom_lo, dom_hi = config.hammer_ratios.dominant_wick_bounds()
    small_lo, small_hi = config.hammer_ratios.small_wick_bounds()
    body_lo, body_hi = config.hammer_ratios.body_bounds()

    body_ok = body_lo <= body_pct <= body_hi

    lower_is_dominant = dom_lo <= lower_pct <= dom_hi and small_lo <= upper_pct <= small_hi
    upper_is_dominant = dom_lo <= upper_pct <= dom_hi and small_lo <= lower_pct <= small_hi

    if config.hammer_wick_side == WickSide.LOWER:
        shape_ok = body_ok and lower_is_dominant
        variant = HammerVariant.CLASSIC if shape_ok else None
    elif config.hammer_wick_side == WickSide.UPPER:
        shape_ok = body_ok and upper_is_dominant
        variant = HammerVariant.INVERTED if shape_ok else None
    else:  # EITHER
        if body_ok and lower_is_dominant:
            shape_ok = True
            variant = HammerVariant.CLASSIC
        elif body_ok and upper_is_dominant:
            shape_ok = True
            variant = HammerVariant.INVERTED
        else:
            shape_ok = False
            variant = None

    if shape_ok and variant == HammerVariant.CLASSIC and not config.enable_classic_hammer:
        shape_ok = False
        variant = None
    if shape_ok and variant == HammerVariant.INVERTED and not config.enable_inverted_hammer:
        shape_ok = False
        variant = None

    is_valid_shape = shape_ok

    # ---- decide color ----
    if candle.is_green:
        color = CandleColor.GREEN
    elif candle.is_red:
        color = CandleColor.RED
    else:
        color = CandleColor.DOJI
        if not config.allow_doji_signals:
            is_valid_shape = False  # doji has no color signal -> ignored by default

    # ---- decide direction from color (fully editable mapping) ----
    direction = None
    if is_valid_shape:
        if color == CandleColor.GREEN:
            direction = config.green_direction
        elif color == CandleColor.RED:
            direction = config.red_direction
        # DOJI stays None unless allow_doji_signals AND you extend this
        # mapping yourself (left as None here since a doji has no color).

    return HammerResult(
        candle=candle,
        color=color,
        body_pct=body_pct,
        upper_wick_pct=upper_pct,
        lower_wick_pct=lower_pct,
        is_valid_shape=is_valid_shape,
        direction=direction,
        hammer_variant=variant,
    )


# ============================================================================
# SECTION 5: STEP 3 -- ENTRY PRICE (fully configurable via EntryRule)
# ============================================================================

def calculate_entry_price(
    hammer_candle: Candle,
    next_candle: Candle,
    config: StrategyConfig,
) -> float:
    """
    STEP 3: Resolve entry price according to config.entry_rule.
    Default = NEXT_CANDLE_OPEN (matches your flowchart exactly).
    Other modes exist purely so you can test alternate entry timing.
    An optional flat entry_offset $ is then added (default 0, no effect).
    """
    rule = config.entry_rule

    if rule == EntryRule.NEXT_CANDLE_OPEN:
        base = next_candle.open
    elif rule == EntryRule.NEXT_CANDLE_CLOSE:
        base = next_candle.close
    elif rule == EntryRule.HAMMER_CLOSE:
        base = hammer_candle.close
    elif rule == EntryRule.HAMMER_HIGH:
        base = hammer_candle.high
    elif rule == EntryRule.HAMMER_LOW:
        base = hammer_candle.low
    else:
        raise ValueError(f"Unknown entry_rule: {rule}")

    return base + config.entry_offset


# ============================================================================
# SECTION 6: STEP 4 -- STOP LOSS (fully configurable via BufferMode)
# ============================================================================

def calculate_stop_loss(
    hammer_candle: Candle,
    direction: TradeDirection,
    config: StrategyConfig,
) -> float:
    """
    STEP 4: SL = hammer LOW (BUY) / HIGH (SELL), adjusted by a buffer.
    Buffer size and mode are both configurable:
        PERCENT_OF_RANGE : buffer = hammer_candle.range_ * sl_buffer_pct/100
        PERCENT_OF_PRICE : buffer = (low or high price) * sl_buffer_pct/100
        FLAT_AMOUNT       : buffer = sl_buffer_flat  (a fixed $ value)
        NONE              : buffer = 0 (SL sits exactly at the wick tip)
    """
    anchor = hammer_candle.low if direction == TradeDirection.BUY else hammer_candle.high

    if config.buffer_mode == BufferMode.PERCENT_OF_RANGE:
        buffer_amount = hammer_candle.range_ * (config.sl_buffer_pct / 100.0)
    elif config.buffer_mode == BufferMode.PERCENT_OF_PRICE:
        buffer_amount = anchor * (config.sl_buffer_pct / 100.0)
    elif config.buffer_mode == BufferMode.FLAT_AMOUNT:
        buffer_amount = config.sl_buffer_flat
    elif config.buffer_mode == BufferMode.NONE:
        buffer_amount = 0.0
    else:
        raise ValueError(f"Unknown buffer_mode: {config.buffer_mode}")

    if direction == TradeDirection.BUY:
        return anchor - buffer_amount   # SL pushed further below the low
    else:
        return anchor + buffer_amount   # SL pushed further above the high


# ============================================================================
# SECTION 7: STEP 5 & 6 -- BUILD THE FULL TRADE SIGNAL
# ============================================================================

def build_trade_signal(
    hammer_result: HammerResult,
    next_candle: Candle,
    timeframe: str,
    config: StrategyConfig,
) -> Optional[TradeSignal]:
    """
    STEP 3: Entry  (via calculate_entry_price, config.entry_rule)
    STEP 4: SL     (via calculate_stop_loss, config.buffer_mode)
    STEP 5: Target = Risk x RR multiple (per-timeframe, config.timeframe_settings)
    STEP 6: Risk-limit check (config.enable_risk_limit, per-timeframe max_sl_usd)
    """
    if hammer_result.direction is None:
        return None  # not a valid, colored hammer -- nothing to do

    direction = hammer_result.direction
    variant = hammer_result.hammer_variant

    if variant == HammerVariant.CLASSIC:
        if direction == TradeDirection.BUY and not config.classic_hammer_allow_buy:
            return TradeSignal(
                direction=direction,
                hammer_candle=hammer_result.candle,
                entry_candle=next_candle,
                entry_price=0.0,
                stop_loss=0.0,
                risk=0.0,
                rr_multiple=0.0,
                target=0.0,
                timeframe=timeframe,
                ignored=True,
                ignore_reason="Classic hammer BUY direction is disabled in settings.",
                pattern_variant=variant.value,
            )
        if direction == TradeDirection.SELL and not config.classic_hammer_allow_sell:
            return TradeSignal(
                direction=direction,
                hammer_candle=hammer_result.candle,
                entry_candle=next_candle,
                entry_price=0.0,
                stop_loss=0.0,
                risk=0.0,
                rr_multiple=0.0,
                target=0.0,
                timeframe=timeframe,
                ignored=True,
                ignore_reason="Classic hammer SELL direction is disabled in settings.",
                pattern_variant=variant.value,
            )
    elif variant == HammerVariant.INVERTED:
        if direction == TradeDirection.BUY and not config.inverted_hammer_allow_buy:
            return TradeSignal(
                direction=direction,
                hammer_candle=hammer_result.candle,
                entry_candle=next_candle,
                entry_price=0.0,
                stop_loss=0.0,
                risk=0.0,
                rr_multiple=0.0,
                target=0.0,
                timeframe=timeframe,
                ignored=True,
                ignore_reason="Inverted hammer BUY direction is disabled in settings.",
                pattern_variant=variant.value,
            )
        if direction == TradeDirection.SELL and not config.inverted_hammer_allow_sell:
            return TradeSignal(
                direction=direction,
                hammer_candle=hammer_result.candle,
                entry_candle=next_candle,
                entry_price=0.0,
                stop_loss=0.0,
                risk=0.0,
                rr_multiple=0.0,
                target=0.0,
                timeframe=timeframe,
                ignored=True,
                ignore_reason="Inverted hammer SELL direction is disabled in settings.",
                pattern_variant=variant.value,
            )

    if timeframe not in config.timeframe_settings:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. "
            f"Available: {list(config.timeframe_settings.keys())}"
        )

    tf_setting = config.timeframe_settings[timeframe]
    hammer_candle = hammer_result.candle
    direction = hammer_result.direction

    entry_price = calculate_entry_price(hammer_candle, next_candle, config)
    stop_loss = calculate_stop_loss(hammer_candle, direction, config)

    if direction == TradeDirection.BUY:
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price

    # ---- STEP 6: RISK LIMIT CHECK (fully toggleable) -------------------
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

    # ---- STEP 5: TARGET CALCULATION -----------------------------------
    reward = risk * tf_setting.rr_multiple
    if direction == TradeDirection.BUY:
        target = entry_price + reward
    else:
        target = entry_price - reward

    return TradeSignal(
        direction=direction,
        hammer_candle=hammer_candle,
        entry_candle=next_candle,
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk=risk,
        rr_multiple=tf_setting.rr_multiple,
        target=target,
        timeframe=timeframe,
        ignored=ignored,
        ignore_reason=ignore_reason,
        pattern_variant=variant.value if variant else None,
    )


# ============================================================================
# SECTION 8: FULL PIPELINE -- RUN OVER A LIST OF CANDLES (per flowchart)
# ============================================================================

def run_strategy(
    candles: List[Candle],
    timeframe: str,
    config: Optional[StrategyConfig] = None,
) -> List[TradeSignal]:
    """
    Runs the full flowchart over a sequence of candles. Returns a list of
    TradeSignal (including ignored ones, with reasons attached), so you
    can inspect every possibility -- taken and rejected -- for testing.
    """
    if config is None:
        config = StrategyConfig()

    signals: List[TradeSignal] = []

    for i in range(len(candles) - 1):
        current_candle = candles[i]
        next_candle = candles[i + 1]

        hammer_result = check_hammer(current_candle, config)

        if hammer_result.direction is not None:
            signal = build_trade_signal(
                hammer_result=hammer_result,
                next_candle=next_candle,
                timeframe=timeframe,
                config=config,
            )
            if signal is not None:
                signals.append(signal)

    return signals


# ============================================================================
# SECTION 9: DEMO / EXAMPLE USAGE -- shows how to change EVERY parameter
# ============================================================================

if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Sample candles: one GREEN hammer (-> BUY) and one RED hammer (-> SELL)
    # ------------------------------------------------------------------
    sample_candles = [
        Candle("09:00", 100, 101, 99.5, 100.3),
        Candle("10:00", 43, 50, 30, 45),   # GREEN hammer -> BUY signal
        Candle("11:00", 44, 53, 42, 52),   # next candle (entry source)
        Candle("12:00", 52, 55, 51, 54),
        Candle("13:00", 60, 62, 58, 59),
        Candle("14:00", 45, 50, 30, 43),   # RED hammer -> SELL signal
        Candle("15:00", 42, 46, 40, 41),   # next candle (entry source)
        Candle("16:00", 41, 43, 38, 39),
    ]

    print("=" * 80)
    print("RUN 1: Default config (matches your original flowchart exactly)")
    print("=" * 80)
    default_config = StrategyConfig()
    default_config.timeframe_settings["1h"] = TimeframeSetting(rr_multiple=1.6, max_sl_usd=18)
    results = run_strategy(sample_candles, timeframe="1h", config=default_config)
    for r in results:
        status = "IGNORED" if r.ignored else "TAKEN"
        print(f"[{status}] {r.direction.value:<4} Entry={r.entry_price:<8.2f} "
              f"SL={r.stop_loss:<8.2f} Risk={r.risk:<8.2f} Target={r.target:<8.2f} "
              f"RR={r.rr_multiple} TF={r.timeframe}"
              + (f"  | {r.ignore_reason}" if r.ignored else ""))

    print()
    print("=" * 80)
    print("RUN 2: Testing a DIFFERENT parameter set")
    print("       - wider body tolerance (headline 35% body, up to +/-30)")
    print("       - flat $2 buffer instead of % buffer")
    print("       - entry at hammer's CLOSE instead of next candle's open")
    print("       - risk limit disabled (see every signal, even over-risk ones)")
    print("=" * 80)
    test_config = StrategyConfig(
        hammer_ratios=HammerRatioConfig(
            dominant_wick_pct=65,   # your headline "65% wick"
            dominant_wick_tol=18,
            body_pct=35,            # your headline "35% body"
            body_tol=30,
            small_wick_pct=20,
            small_wick_tol=18,
        ),
        buffer_mode=BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=2.0,
        entry_rule=EntryRule.HAMMER_CLOSE,
        enable_risk_limit=False,
    )
    results2 = run_strategy(sample_candles, timeframe="1h", config=test_config)
    for r in results2:
        status = "IGNORED" if r.ignored else "TAKEN"
        print(f"[{status}] {r.direction.value:<4} Entry={r.entry_price:<8.2f} "
              f"SL={r.stop_loss:<8.2f} Risk={r.risk:<8.2f} Target={r.target:<8.2f} "
              f"RR={r.rr_multiple} TF={r.timeframe}"
              + (f"  | {r.ignore_reason}" if r.ignored else ""))