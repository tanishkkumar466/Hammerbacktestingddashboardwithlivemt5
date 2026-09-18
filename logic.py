"""
logic.py
========
Hammer Candle Detection + Entry / SL / Target Strategy Engine
FULLY PARAMETERIZED VERSION -- every rule, threshold, and mode used anywhere
in this file is a named field inside StrategyConfig (Section 2). Nothing
that affects a trading decision is hardcoded inside a function body.
Change a config field -> behavior changes. That's it.

DIRECTION RULE (shape vs color — read both):
    SHAPE (two independent patterns):
        CLASSIC  = long LOWER wick (hammer at support)
        INVERTED = long UPPER wick (inverted hammer / shooting star)
        Controlled by enable_classic_hammer / enable_inverted_hammer and
        resolve_hammer_wick_side() -> LOWER | UPPER | EITHER for detection.

    COLOR -> TRADE (same for classic and inverted):
        GREEN candle (close > open) -> config.green_direction  (default BUY)
        RED   candle (close < open) -> config.red_direction    (default SELL)

    Example: inverted hammer + red candle -> SELL if red_direction is SELL.
    Example: classic hammer + green candle -> BUY if green_direction is BUY.

    Per-variant allow flags (classic_hammer_allow_buy/sell, inverted_hammer_allow_*)
    can block a matched shape without changing detection.

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

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Tuple


# ============================================================================
# SECTION 1: CANDLE DATA STRUCTURE
# ============================================================================

@dataclass
class Candle:
    """
    A single OHLC candle.
    timestamp : any identifier (datetime, string, index...)
    open, high, low, close : prices
    volume    : bar volume (MT5 tick_volume in live) — needed for real VWAP
    """
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

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


class TradeAction(Enum):
    """Per hammer-type × candle-color choice on the Direction tab."""
    BUY = "BUY"
    SELL = "SELL"
    NO = "NO"


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


class StopLossMode(Enum):
    """Where stop-loss is anchored."""
    CANDLE_EXTREME = "CANDLE_EXTREME"       # SL at signal candle low (BUY) / high (SELL) + buffer
    FIXED_FROM_ENTRY = "FIXED_FROM_ENTRY"   # SL = entry price +/- fixed distance (price units)


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


def coerce_trade_action(value, default: "TradeAction" = None) -> "TradeAction":
    """Accept TradeAction, TradeDirection, or BUY/SELL/NO strings."""
    if default is None:
        default = TradeAction.NO
    if isinstance(value, TradeAction):
        return value
    if isinstance(value, TradeDirection):
        return TradeAction.BUY if value == TradeDirection.BUY else TradeAction.SELL
    raw = str(value or "").strip().upper()
    if raw in ("NO", "NONE", "SKIP", "OFF", "N"):
        return TradeAction.NO
    if raw == "BUY":
        return TradeAction.BUY
    if raw == "SELL":
        return TradeAction.SELL
    return default


def hammer_trade_action(
    config: "StrategyConfig",
    variant: Optional["HammerVariant"],
    color: "CandleColor",
) -> TradeAction:
    """Look up Direction-tab action for this shape × candle color."""
    if variant == HammerVariant.CLASSIC:
        if color == CandleColor.GREEN:
            return coerce_trade_action(config.classic_green, TradeAction.BUY)
        if color == CandleColor.RED:
            return coerce_trade_action(config.classic_red, TradeAction.SELL)
    elif variant == HammerVariant.INVERTED:
        if color == CandleColor.GREEN:
            return coerce_trade_action(config.inverted_green, TradeAction.BUY)
        if color == CandleColor.RED:
            return coerce_trade_action(config.inverted_red, TradeAction.SELL)
    return TradeAction.NO


def sync_legacy_direction_flags(config: "StrategyConfig") -> None:
    """Keep enable_*/allow_*/green_direction in sync with the four Direction-tab actions."""
    cg = coerce_trade_action(config.classic_green, TradeAction.BUY)
    cr = coerce_trade_action(config.classic_red, TradeAction.SELL)
    ig = coerce_trade_action(config.inverted_green, TradeAction.BUY)
    ir = coerce_trade_action(config.inverted_red, TradeAction.SELL)
    config.classic_green, config.classic_red = cg, cr
    config.inverted_green, config.inverted_red = ig, ir
    config.enable_classic_hammer = cg != TradeAction.NO or cr != TradeAction.NO
    config.enable_inverted_hammer = ig != TradeAction.NO or ir != TradeAction.NO
    config.classic_hammer_allow_buy = TradeAction.BUY in (cg, cr)
    config.classic_hammer_allow_sell = TradeAction.SELL in (cg, cr)
    config.inverted_hammer_allow_buy = TradeAction.BUY in (ig, ir)
    config.inverted_hammer_allow_sell = TradeAction.SELL in (ig, ir)
    for action in (cg, ig):
        if action != TradeAction.NO:
            config.green_direction = (
                TradeDirection.BUY if action == TradeAction.BUY else TradeDirection.SELL
            )
            break
    for action in (cr, ir):
        if action != TradeAction.NO:
            config.red_direction = (
                TradeDirection.BUY if action == TradeAction.BUY else TradeDirection.SELL
            )
            break
    ui = config.hammer_wick_side
    if not isinstance(ui, WickSide):
        try:
            ui = WickSide(str(ui))
        except ValueError:
            ui = WickSide.LOWER
    config.hammer_wick_side = resolve_hammer_wick_side(
        config.enable_classic_hammer,
        config.enable_inverted_hammer,
        ui,
    )


def describe_hammer_direction_matrix(config: "StrategyConfig") -> str:
    return (
        f"Classic green→{coerce_trade_action(config.classic_green).value} "
        f"red→{coerce_trade_action(config.classic_red).value} | "
        f"Inverted green→{coerce_trade_action(config.inverted_green).value} "
        f"red→{coerce_trade_action(config.inverted_red).value}"
    )


def describe_hammer_entry_exit(config: "StrategyConfig") -> str:
    """Classic vs inverted entry/SL — same string for backtest logs and live start."""
    c_rule = coerce_entry_rule(getattr(config, "entry_rule", EntryRule.NEXT_CANDLE_OPEN))
    i_rule = coerce_entry_rule(getattr(config, "inverted_entry_rule", c_rule))
    c_off = float(getattr(config, "entry_offset", 0.0) or 0.0)
    i_off = float(getattr(config, "inverted_entry_offset", 0.0) or 0.0)
    c_buf = coerce_buffer_mode(getattr(config, "buffer_mode", BufferMode.PERCENT_OF_RANGE))
    i_buf = coerce_buffer_mode(getattr(config, "inverted_buffer_mode", c_buf))
    c_sl = coerce_stop_loss_mode(getattr(config, "sl_mode", StopLossMode.CANDLE_EXTREME))
    i_sl = coerce_stop_loss_mode(getattr(config, "inverted_sl_mode", c_sl))
    c_sl_txt = (
        f"fixed ${float(getattr(config, 'sl_fixed_distance', 0) or 0):g} from entry"
        if c_sl == StopLossMode.FIXED_FROM_ENTRY
        else f"{c_buf.value} {float(getattr(config, 'sl_buffer_pct', 0) or 0):g}%/${float(getattr(config, 'sl_buffer_flat', 0) or 0):g}"
    )
    i_sl_txt = (
        f"fixed ${float(getattr(config, 'inverted_sl_fixed_distance', 0) or 0):g} from entry"
        if i_sl == StopLossMode.FIXED_FROM_ENTRY
        else f"{i_buf.value} {float(getattr(config, 'inverted_sl_buffer_pct', 0) or 0):g}%/${float(getattr(config, 'inverted_sl_buffer_flat', 0) or 0):g}"
    )
    return (
        f"Classic entry={c_rule.value} offset=${c_off:g} SL={c_sl_txt} | "
        f"Inverted entry={i_rule.value} offset=${i_off:g} SL={i_sl_txt}"
    )


def entry_rule_label_for_variant(config: "StrategyConfig", variant=None) -> str:
    if _is_inverted_variant(variant):
        return coerce_entry_rule(getattr(config, "inverted_entry_rule", config.entry_rule)).value
    return coerce_entry_rule(config.entry_rule).value


def direction_matrix_from_legacy_fields(
    green_direction="BUY",
    red_direction="SELL",
    enable_classic=True,
    enable_inverted=True,
    classic_allow_buy=True,
    classic_allow_sell=True,
    inverted_allow_buy=True,
    inverted_allow_sell=True,
) -> Dict[str, str]:
    """Convert old Direction-tab presets into the four BUY/SELL/NO fields."""
    green = coerce_trade_action(green_direction, TradeAction.BUY)
    red = coerce_trade_action(red_direction, TradeAction.SELL)

    def _slot(enabled: bool, action: TradeAction, allow_buy: bool, allow_sell: bool) -> str:
        if not enabled or action == TradeAction.NO:
            return TradeAction.NO.value
        if action == TradeAction.BUY and not allow_buy:
            return TradeAction.NO.value
        if action == TradeAction.SELL and not allow_sell:
            return TradeAction.NO.value
        return action.value

    return {
        "classic_green": _slot(bool(enable_classic), green, classic_allow_buy, classic_allow_sell),
        "classic_red": _slot(bool(enable_classic), red, classic_allow_buy, classic_allow_sell),
        "inverted_green": _slot(bool(enable_inverted), green, inverted_allow_buy, inverted_allow_sell),
        "inverted_red": _slot(bool(enable_inverted), red, inverted_allow_buy, inverted_allow_sell),
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

    # ---- Direction tab: one choice per (shape × color) ----
    # BUY / SELL / NO. NO = do not trade that combination.
    classic_green: TradeAction = TradeAction.BUY
    classic_red: TradeAction = TradeAction.SELL
    inverted_green: TradeAction = TradeAction.BUY
    inverted_red: TradeAction = TradeAction.SELL

    # ---- derived / legacy (kept so old presets and code still load) ----
    enable_classic_hammer: bool = True
    enable_inverted_hammer: bool = True
    classic_hammer_allow_buy: bool = True
    classic_hammer_allow_sell: bool = True
    inverted_hammer_allow_buy: bool = True
    inverted_hammer_allow_sell: bool = True
    green_direction: TradeDirection = TradeDirection.BUY
    red_direction: TradeDirection = TradeDirection.SELL
    allow_doji_signals: bool = False   # if True, doji candles won't auto-fail shape check

    def __post_init__(self):
        self.classic_green = coerce_trade_action(self.classic_green, TradeAction.BUY)
        self.classic_red = coerce_trade_action(self.classic_red, TradeAction.SELL)
        self.inverted_green = coerce_trade_action(self.inverted_green, TradeAction.BUY)
        self.inverted_red = coerce_trade_action(self.inverted_red, TradeAction.SELL)
        if isinstance(self.green_direction, str):
            self.green_direction = TradeDirection(self.green_direction)
        if isinstance(self.red_direction, str):
            self.red_direction = TradeDirection(self.red_direction)
        # Old constructors that only flipped enable_* : turn that type to NO.
        if (
            not self.enable_classic_hammer
            and self.classic_green == TradeAction.BUY
            and self.classic_red == TradeAction.SELL
        ):
            self.classic_green = TradeAction.NO
            self.classic_red = TradeAction.NO
        if (
            not self.enable_inverted_hammer
            and self.inverted_green == TradeAction.BUY
            and self.inverted_red == TradeAction.SELL
        ):
            self.inverted_green = TradeAction.NO
            self.inverted_red = TradeAction.NO
        sync_legacy_direction_flags(self)
        self.entry_rule = coerce_entry_rule(self.entry_rule)
        self.inverted_entry_rule = coerce_entry_rule(self.inverted_entry_rule)
        self.sl_mode = coerce_stop_loss_mode(self.sl_mode)
        self.inverted_sl_mode = coerce_stop_loss_mode(self.inverted_sl_mode)
        self.buffer_mode = coerce_buffer_mode(self.buffer_mode)
        self.inverted_buffer_mode = coerce_buffer_mode(self.inverted_buffer_mode)

    # ---- entry (classic uses entry_rule; inverted has its own — HAMMER_HIGH
    #      on a classic is the short-wick side, on inverted it is the long wick) ----
    entry_rule: EntryRule = EntryRule.NEXT_CANDLE_OPEN
    entry_offset: float = 0.0
    inverted_entry_rule: EntryRule = EntryRule.NEXT_CANDLE_OPEN
    inverted_entry_offset: float = 0.0

    # ---- stop loss / buffer (per hammer type) ----
    sl_mode: StopLossMode = StopLossMode.CANDLE_EXTREME
    sl_fixed_distance: float = 5.0
    buffer_mode: BufferMode = BufferMode.PERCENT_OF_RANGE
    sl_buffer_pct: float = 5.0
    sl_buffer_flat: float = 0.0
    inverted_sl_mode: StopLossMode = StopLossMode.CANDLE_EXTREME
    inverted_sl_fixed_distance: float = 5.0
    inverted_buffer_mode: BufferMode = BufferMode.PERCENT_OF_RANGE
    inverted_sl_buffer_pct: float = 5.0
    inverted_sl_buffer_flat: float = 0.0

    # ---- risk / target ----
    timeframe_settings: Dict[str, TimeframeSetting] = field(
        default_factory=lambda: dict(DEFAULT_TIMEFRAME_SETTINGS)
    )
    enable_risk_limit: bool = True   # set False to see ALL signals, even over-risk ones
    reject_zero_or_negative_risk: bool = True  # safety guard, can be disabled for testing

    # ---- misc / safety ----
    min_range: float = 1e-9   # floor to avoid divide-by-zero on flat candles


def resolve_hammer_wick_side(
    enable_classic: bool,
    enable_inverted: bool,
    ui_wick: WickSide = WickSide.LOWER,
) -> WickSide:
    """
    Map Classic / Inverted toggles to which shapes check_hammer() accepts.
    When both types are enabled, EITHER (long lower or long upper wick).
    """
    if enable_classic and enable_inverted:
        return WickSide.EITHER
    if enable_inverted and not enable_classic:
        return WickSide.UPPER
    if enable_classic and not enable_inverted:
        return WickSide.LOWER
    return ui_wick


def candle_color_label(candle: Candle) -> str:
    if candle.is_green:
        return "GREEN"
    if candle.is_red:
        return "RED"
    return "DOJI"


def explain_hammer_signal_direction(
    hammer_candle: Candle,
    direction: TradeDirection,
    config: StrategyConfig,
    variant: Optional[str] = None,
) -> str:
    """
    Plain-language direction line for live logs.
    Direction comes from the HAMMER (signal) bar color + classic/inverted shape.
    """
    color = candle_color_label(hammer_candle)
    shape = variant or "—"
    try:
        hv = HammerVariant(shape) if shape in ("CLASSIC", "INVERTED") else None
    except ValueError:
        hv = None
    try:
        cc = CandleColor(color)
    except ValueError:
        cc = CandleColor.DOJI
    mapped = hammer_trade_action(config, hv, cc).value
    return (
        f"Direction: {shape} {color} → settings {mapped} "
        f"→ {direction.value}"
    )


def expected_direction_for_signal_candle(
    candle: Candle,
    config: StrategyConfig,
    variant: Optional[str] = None,
) -> Optional[TradeDirection]:
    """Shape × color → BUY/SELL, or None when that combo is set to NO."""
    if candle.is_green:
        color = CandleColor.GREEN
    elif candle.is_red:
        color = CandleColor.RED
    else:
        return None
    hv = None
    if isinstance(variant, HammerVariant):
        hv = variant
    elif variant in ("CLASSIC", "INVERTED"):
        hv = HammerVariant(variant)
    action = hammer_trade_action(config, hv, color)
    if action == TradeAction.NO:
        return None
    return TradeDirection.BUY if action == TradeAction.BUY else TradeDirection.SELL


def verify_hammer_trade_signal(
    sig: TradeSignal,
    config: StrategyConfig,
) -> Tuple[bool, str]:
    """
    Ensures signal direction matches the Direction-tab matrix for this
    hammer shape × candle color. Returns (False, reason) on mismatch.
    """
    variant = getattr(sig, "pattern_variant", None)
    expected = expected_direction_for_signal_candle(sig.hammer_candle, config, variant)
    if expected is None:
        return False, (
            f"Parameter check: {variant or 'hammer'} "
            f"{candle_color_label(sig.hammer_candle)} is set to NO — should not trade."
        )
    if sig.direction != expected:
        return False, (
            f"Parameter check failed: {variant or 'hammer'} "
            f"{candle_color_label(sig.hammer_candle)} must → {expected.value} "
            f"but signal is {sig.direction.value}. Fix the Direction tab."
        )
    return True, ""


def run_hammer_direction_self_test(config: StrategyConfig) -> List[str]:
    """Prove each of the four Direction-tab combos maps to BUY / SELL / NO."""
    lines: List[str] = [
        f"Settings: {describe_hammer_direction_matrix(config)}",
        f"Detection: {describe_hammer_detection(config)}",
    ]
    # Classic: long lower wick. Inverted: long upper wick.
    # Ratios sit inside default HammerRatioConfig bands (body ~20%,
    # dominant ~60%, small ~20%) so the self-test proves direction, not shape.
    samples = [
        ("Classic GREEN", Candle("c-g", 100.0, 103.5, 90.0, 102.0), HammerVariant.CLASSIC),
        ("Classic RED", Candle("c-r", 102.0, 103.5, 90.0, 100.0), HammerVariant.CLASSIC),
        ("Inverted GREEN", Candle("i-g", 100.0, 108.0, 99.6, 102.0), HammerVariant.INVERTED),
        ("Inverted RED", Candle("i-r", 102.0, 108.0, 99.6, 100.0), HammerVariant.INVERTED),
    ]
    for label, bar, want_variant in samples:
        hr = check_hammer(bar, config)
        color = CandleColor.GREEN if bar.is_green else CandleColor.RED
        action = hammer_trade_action(config, want_variant, color)
        if action == TradeAction.NO:
            if hr.direction is None:
                lines.append(f"  {label}: OK — set to NO (no trade)")
            else:
                lines.append(f"  {label}: FAIL — set to NO but got {hr.direction.value}")
            continue
        if not hr.is_valid_shape or hr.direction is None:
            lines.append(f"  {label}: shape did not pass (tune Body/Wicks).")
            continue
        expected = TradeDirection.BUY if action == TradeAction.BUY else TradeDirection.SELL
        if hr.direction == expected:
            lines.append(f"  {label}: OK — {action.value}")
        else:
            lines.append(f"  {label}: FAIL — expected {expected.value}, got {hr.direction.value}")
    return lines


def describe_hammer_detection(config: StrategyConfig) -> str:
    """Human-readable summary of which hammer shapes the engine will detect."""
    enabled: List[str] = []
    if config.enable_classic_hammer:
        enabled.append("classic (long lower wick)")
    if config.enable_inverted_hammer:
        enabled.append("inverted (long upper wick)")
    if not enabled:
        return "No hammer types enabled — no hammer signals."
    ws = config.hammer_wick_side
    if not isinstance(ws, WickSide):
        try:
            ws = WickSide(str(ws))
        except ValueError:
            ws = WickSide.LOWER
    return f"Detects {' and '.join(enabled)} · wick mode {ws.value}"


def preview_candle_is_green(
    trade_side: TradeDirection,
    config: StrategyConfig,
    variant: str = "CLASSIC",
) -> bool:
    """For UI preview: candle color that produces trade_side for this hammer shape."""
    if variant == HammerVariant.INVERTED.value or variant == "INVERTED":
        green_a = coerce_trade_action(config.inverted_green)
        red_a = coerce_trade_action(config.inverted_red)
    else:
        green_a = coerce_trade_action(config.classic_green)
        red_a = coerce_trade_action(config.classic_red)
    want = TradeAction.BUY if trade_side == TradeDirection.BUY else TradeAction.SELL
    if green_a == want:
        return True
    if red_a == want:
        return False
    return trade_side == TradeDirection.BUY


def preview_draw_wick_side(config: StrategyConfig, shape_choice: str) -> str:
    """LOWER or UPPER string for dashboard candle widgets (shape_choice: CLASSIC | INVERTED)."""
    ws = config.hammer_wick_side
    if not isinstance(ws, WickSide):
        try:
            ws = WickSide(str(ws))
        except ValueError:
            ws = WickSide.LOWER
    if ws == WickSide.EITHER:
        return "LOWER" if shape_choice == "CLASSIC" else "UPPER"
    return ws.value


def variant_name_for_draw_wick(draw_wick: str) -> str:
    return HammerVariant.CLASSIC.value if draw_wick == "LOWER" else HammerVariant.INVERTED.value


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
    # True when entry_price is a limit toward SL (e.g. hammer-with-candle 35% pullback)
    await_limit_fill: bool = False


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


def candle_ohlc_valid(candle: Candle, tol: float = 1e-4) -> bool:
    """True when open/close lie within [low, high] (guards corrupted broker feeds)."""
    if candle.high < candle.low - tol:
        return False
    lo, hi = candle.low - tol, candle.high + tol
    return lo <= candle.open <= hi and lo <= candle.close <= hi


def describe_hammer_probe(candle: Candle, config: StrategyConfig) -> str:
    """Human-readable hammer shape metrics for live debugging."""
    body_pct, upper_pct, lower_pct = calculate_candle_metrics(candle, config)
    rng = candle.range_
    body = candle.body
    uw = candle.upper_wick
    lw = candle.lower_wick
    dom_lo, dom_hi = config.hammer_ratios.dominant_wick_bounds()
    small_lo, small_hi = config.hammer_ratios.small_wick_bounds()
    body_lo, body_hi = config.hammer_ratios.body_bounds()
    body_range_ratio = (body / rng * 100.0) if rng > config.min_range else 0.0
    lw_body = (lw / body) if body > config.min_range else 0.0
    uw_body = (uw / body) if body > config.min_range else 0.0
    color = candle_color_label(candle)
    return (
        f"O={candle.open:.2f} H={candle.high:.2f} L={candle.low:.2f} C={candle.close:.2f} ({color}) | "
        f"range={rng:.2f} body={body:.2f} ({body_range_ratio:.1f}% of range, need {body_lo:.0f}-{body_hi:.0f}%) | "
        f"lower_wick={lw:.2f} ({lower_pct:.1f}%, dom band {dom_lo:.0f}-{dom_hi:.0f}%) | "
        f"upper_wick={uw:.2f} ({upper_pct:.1f}%, small band {small_lo:.0f}-{small_hi:.0f}%) | "
        f"lower/body={lw_body:.2f} upper/body={uw_body:.2f}"
    )


def check_hammer(candle: Candle, config: StrategyConfig) -> HammerResult:
    """
    STEP 2: Validate hammer SHAPE using the fully editable wick/body bounds
    in config.hammer_ratios, and using config.hammer_wick_side to decide
    which wick must be the dominant one (LOWER / UPPER / EITHER).

    COLOR -> DIRECTION comes from the Direction tab matrix:
        classic green / classic red / inverted green / inverted red
        each independently BUY, SELL, or NO.
    """
    body_pct, upper_pct, lower_pct = calculate_candle_metrics(candle, config)

    dom_lo, dom_hi = config.hammer_ratios.dominant_wick_bounds()
    small_lo, small_hi = config.hammer_ratios.small_wick_bounds()
    body_lo, body_hi = config.hammer_ratios.body_bounds()

    body_ok = body_lo <= body_pct <= body_hi

    lower_is_dominant = dom_lo <= lower_pct <= dom_hi and small_lo <= upper_pct <= small_hi
    upper_is_dominant = dom_lo <= upper_pct <= dom_hi and small_lo <= lower_pct <= small_hi

    wick_side = config.hammer_wick_side
    if not isinstance(wick_side, WickSide):
        try:
            wick_side = WickSide(str(wick_side))
        except ValueError:
            wick_side = WickSide.EITHER

    if wick_side == WickSide.LOWER:
        shape_ok = body_ok and lower_is_dominant
        variant = HammerVariant.CLASSIC if shape_ok else None
    elif wick_side == WickSide.UPPER:
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

    classic_on = coerce_trade_action(config.classic_green) != TradeAction.NO or coerce_trade_action(config.classic_red) != TradeAction.NO
    inverted_on = coerce_trade_action(config.inverted_green) != TradeAction.NO or coerce_trade_action(config.inverted_red) != TradeAction.NO
    if shape_ok and variant == HammerVariant.CLASSIC and not classic_on:
        shape_ok = False
        variant = None
    if shape_ok and variant == HammerVariant.INVERTED and not inverted_on:
        shape_ok = False
        variant = None

    is_valid_shape = shape_ok

    if candle.is_green:
        color = CandleColor.GREEN
    elif candle.is_red:
        color = CandleColor.RED
    else:
        color = CandleColor.DOJI
        if not config.allow_doji_signals:
            is_valid_shape = False

    direction = None
    if is_valid_shape and variant is not None and color != CandleColor.DOJI:
        action = hammer_trade_action(config, variant, color)
        if action == TradeAction.BUY:
            direction = TradeDirection.BUY
        elif action == TradeAction.SELL:
            direction = TradeDirection.SELL
        else:
            is_valid_shape = False
            direction = None

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

def coerce_entry_rule(value, default: EntryRule = EntryRule.NEXT_CANDLE_OPEN) -> EntryRule:
    if isinstance(value, EntryRule):
        return value
    try:
        return EntryRule(str(value))
    except ValueError:
        return default


def coerce_buffer_mode(value, default: BufferMode = BufferMode.PERCENT_OF_RANGE) -> BufferMode:
    if isinstance(value, BufferMode):
        return value
    try:
        return BufferMode(str(value))
    except ValueError:
        return default


def coerce_stop_loss_mode(value, default: StopLossMode = StopLossMode.CANDLE_EXTREME) -> StopLossMode:
    if isinstance(value, StopLossMode):
        return value
    try:
        return StopLossMode(str(value))
    except ValueError:
        return default


def _is_inverted_variant(variant) -> bool:
    if variant == HammerVariant.INVERTED:
        return True
    if isinstance(variant, str) and variant.upper() == HammerVariant.INVERTED.value:
        return True
    return False


def calculate_entry_price(
    hammer_candle: Candle,
    next_candle: Candle,
    config: StrategyConfig,
    variant=None,
) -> float:
    """
    STEP 3: Entry from Classic or Inverted Entry Rule (Direction-tab shape).
    HAMMER_HIGH / HAMMER_LOW are the candle high/low — set inverted separately
    so a classic HAMMER_HIGH does not also enter inverted at the long-wick tip.
    """
    if _is_inverted_variant(variant):
        rule = coerce_entry_rule(getattr(config, "inverted_entry_rule", config.entry_rule))
        offset = float(getattr(config, "inverted_entry_offset", config.entry_offset) or 0.0)
    else:
        rule = coerce_entry_rule(config.entry_rule)
        offset = float(config.entry_offset or 0.0)

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

    return base + offset


def calculate_stop_loss(
    hammer_candle: Candle,
    direction: TradeDirection,
    config: StrategyConfig,
    variant=None,
    entry_price: Optional[float] = None,
) -> float:
    """
    STEP 4: SL = hammer LOW (BUY) / HIGH (SELL) + buffer, or fixed distance from entry.
    """
    inverted = _is_inverted_variant(variant)
    if inverted:
        sl_mode = coerce_stop_loss_mode(
            getattr(config, "inverted_sl_mode", StopLossMode.CANDLE_EXTREME),
        )
        fixed_dist = float(getattr(config, "inverted_sl_fixed_distance", 0.0) or 0.0)
        buffer_mode = coerce_buffer_mode(getattr(config, "inverted_buffer_mode", config.buffer_mode))
        sl_pct = float(getattr(config, "inverted_sl_buffer_pct", config.sl_buffer_pct) or 0.0)
        sl_flat = float(getattr(config, "inverted_sl_buffer_flat", config.sl_buffer_flat) or 0.0)
    else:
        sl_mode = coerce_stop_loss_mode(getattr(config, "sl_mode", StopLossMode.CANDLE_EXTREME))
        fixed_dist = float(getattr(config, "sl_fixed_distance", 0.0) or 0.0)
        buffer_mode = coerce_buffer_mode(config.buffer_mode)
        sl_pct = float(config.sl_buffer_pct or 0.0)
        sl_flat = float(config.sl_buffer_flat or 0.0)

    if sl_mode == StopLossMode.FIXED_FROM_ENTRY:
        base = entry_price if entry_price is not None else hammer_candle.close
        dist = max(0.0, fixed_dist)
        if direction == TradeDirection.BUY:
            return base - dist
        return base + dist

    anchor = hammer_candle.low if direction == TradeDirection.BUY else hammer_candle.high

    if buffer_mode == BufferMode.PERCENT_OF_RANGE:
        buffer_amount = hammer_candle.range_ * (sl_pct / 100.0)
    elif buffer_mode == BufferMode.PERCENT_OF_PRICE:
        buffer_amount = anchor * (sl_pct / 100.0)
    elif buffer_mode == BufferMode.FLAT_AMOUNT:
        buffer_amount = sl_flat
    elif buffer_mode == BufferMode.NONE:
        buffer_amount = 0.0
    else:
        raise ValueError(f"Unknown buffer_mode: {buffer_mode}")

    if direction == TradeDirection.BUY:
        return anchor - buffer_amount
    return anchor + buffer_amount


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

    entry_price = calculate_entry_price(hammer_candle, next_candle, config, variant)
    stop_loss = calculate_stop_loss(
        hammer_candle, direction, config, variant, entry_price=entry_price,
    )

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