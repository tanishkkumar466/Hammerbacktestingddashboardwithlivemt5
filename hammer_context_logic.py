"""
hammer_context_logic.py
========================
Multi-candle hammer context pattern (separate from single-candle Hammer in logic.py).

BUY setup:
    Signal bar = classic hammer (long lower wick) that closes GREEN,
    unless buy_require_wick is False — then any GREEN candle whose body
    is at most Body % of the range (wick up/down ignored; color still required).
    Prior N candles: every close must be >= signal low.

SELL setup:
    Signal bar = inverted hammer (long upper wick) that closes RED,
    unless sell_require_wick is False — then any RED candle whose body
    is at most Body % of the range (wick up/down ignored; color still required).
    Prior N candles: every close must be <= signal high.

Entry / SL / TP reuse logic.py helpers. BUY uses classic entry+SL fields;
SELL uses inverted entry+SL fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import logic


@dataclass
class HammerContextConfig:
    buy_hammer_ratios: logic.HammerRatioConfig = field(default_factory=logic.HammerRatioConfig)
    sell_hammer_ratios: logic.HammerRatioConfig = field(default_factory=logic.HammerRatioConfig)
    lookback_candles: int = 5
    enable_buy: bool = True
    enable_sell: bool = True
    # When False, that side matches on body % + color only (wick up/down ignored).
    buy_require_wick: bool = True
    sell_require_wick: bool = True

    # BUY — classic green hammer
    entry_rule: logic.EntryRule = logic.EntryRule.NEXT_CANDLE_OPEN
    entry_offset: float = 0.0
    buffer_mode: logic.BufferMode = logic.BufferMode.PERCENT_OF_RANGE
    sl_buffer_pct: float = 5.0
    sl_buffer_flat: float = 0.0

    # SELL — inverted red hammer
    inverted_entry_rule: logic.EntryRule = logic.EntryRule.NEXT_CANDLE_OPEN
    inverted_entry_offset: float = 0.0
    inverted_buffer_mode: logic.BufferMode = logic.BufferMode.PERCENT_OF_RANGE
    inverted_sl_buffer_pct: float = 5.0
    inverted_sl_buffer_flat: float = 0.0

    timeframe_settings: Dict[str, logic.TimeframeSetting] = field(
        default_factory=lambda: dict(logic.DEFAULT_TIMEFRAME_SETTINGS)
    )
    enable_risk_limit: bool = True
    reject_zero_or_negative_risk: bool = True
    min_range: float = 1e-9

    def __post_init__(self):
        try:
            self.lookback_candles = max(0, int(self.lookback_candles))
        except (TypeError, ValueError):
            self.lookback_candles = 5
        self.entry_rule = logic.coerce_entry_rule(self.entry_rule)
        self.inverted_entry_rule = logic.coerce_entry_rule(self.inverted_entry_rule)
        self.buffer_mode = logic.coerce_buffer_mode(self.buffer_mode)
        self.inverted_buffer_mode = logic.coerce_buffer_mode(self.inverted_buffer_mode)

    def to_buy_shape_config(self) -> logic.StrategyConfig:
        """Hammer shape check: classic only, green → BUY."""
        return logic.StrategyConfig(
            hammer_ratios=self.buy_hammer_ratios,
            hammer_wick_side=logic.WickSide.LOWER,
            classic_green=logic.TradeAction.BUY,
            classic_red=logic.TradeAction.NO,
            inverted_green=logic.TradeAction.NO,
            inverted_red=logic.TradeAction.NO,
            allow_doji_signals=False,
            min_range=self.min_range,
        )

    def to_sell_shape_config(self) -> logic.StrategyConfig:
        """Hammer shape check: inverted only, red → SELL."""
        return logic.StrategyConfig(
            hammer_ratios=self.sell_hammer_ratios,
            hammer_wick_side=logic.WickSide.UPPER,
            classic_green=logic.TradeAction.NO,
            classic_red=logic.TradeAction.NO,
            inverted_green=logic.TradeAction.NO,
            inverted_red=logic.TradeAction.SELL,
            allow_doji_signals=False,
            min_range=self.min_range,
        )

    def to_buy_trade_config(self) -> logic.StrategyConfig:
        cfg = self.to_buy_shape_config()
        cfg.entry_rule = self.entry_rule
        cfg.entry_offset = self.entry_offset
        cfg.buffer_mode = self.buffer_mode
        cfg.sl_buffer_pct = self.sl_buffer_pct
        cfg.sl_buffer_flat = self.sl_buffer_flat
        cfg.timeframe_settings = dict(self.timeframe_settings)
        cfg.enable_risk_limit = self.enable_risk_limit
        cfg.reject_zero_or_negative_risk = self.reject_zero_or_negative_risk
        return cfg

    def to_sell_trade_config(self) -> logic.StrategyConfig:
        cfg = self.to_sell_shape_config()
        cfg.entry_rule = self.inverted_entry_rule
        cfg.entry_offset = self.inverted_entry_offset
        cfg.inverted_entry_rule = self.inverted_entry_rule
        cfg.inverted_entry_offset = self.inverted_entry_offset
        cfg.buffer_mode = self.inverted_buffer_mode
        cfg.inverted_buffer_mode = self.inverted_buffer_mode
        cfg.sl_buffer_pct = self.inverted_sl_buffer_pct
        cfg.sl_buffer_flat = self.inverted_sl_buffer_flat
        cfg.inverted_sl_buffer_pct = self.inverted_sl_buffer_pct
        cfg.inverted_sl_buffer_flat = self.inverted_sl_buffer_flat
        cfg.timeframe_settings = dict(self.timeframe_settings)
        cfg.enable_risk_limit = self.enable_risk_limit
        cfg.reject_zero_or_negative_risk = self.reject_zero_or_negative_risk
        return cfg


def body_pct_of_candle(candle: logic.Candle, min_range: float = 1e-9) -> float:
    rng = candle.range_
    if rng <= min_range:
        return 0.0
    return (candle.body / rng) * 100.0


def body_only_max_pct(ratios: logic.HammerRatioConfig) -> float:
    """When wick is off, Body % is a hard cap (tolerance is not used)."""
    raw = getattr(ratios, "body_pct", 10.0)
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        cap = 10.0
    if cap != cap:  # NaN
        cap = 10.0
    return max(0.0, min(100.0, cap))


def body_only_ok(
    candle: logic.Candle,
    ratios: logic.HammerRatioConfig,
    min_range: float = 1e-9,
) -> Tuple[bool, str]:
    """Legal when body % of range is at most Body % (wick ignored; color checked separately)."""
    actual = body_pct_of_candle(candle, min_range)
    cap = body_only_max_pct(ratios)
    if actual > cap:
        return False, f"Body {actual:.1f}% above max {cap:g}%"
    return True, ""


def describe_hammer_context_rules(config: HammerContextConfig) -> str:
    """Human-readable rules for Hammer with candles pattern."""
    n = config.lookback_candles
    if n <= 0:
        buy_ctx = "no prior-candle context"
        sell_ctx = "no prior-candle context"
    else:
        buy_ctx = f"{n} prior close(s) not below hammer low"
        sell_ctx = f"{n} prior close(s) not above hammer high"
    parts = []
    if config.enable_buy:
        if config.buy_require_wick:
            buy_shape = "classic green hammer"
        else:
            buy_hi = body_only_max_pct(config.buy_hammer_ratios)
            buy_shape = (
                f"green candle, body ≤ {buy_hi:g}% "
                f"(rest is wick; upper/lower ignored)"
            )
        parts.append(f"BUY: {buy_shape} + {buy_ctx}")
    if config.enable_sell:
        if config.sell_require_wick:
            sell_shape = "inverted red hammer"
        else:
            sell_hi = body_only_max_pct(config.sell_hammer_ratios)
            sell_shape = (
                f"red candle, body ≤ {sell_hi:g}% "
                f"(rest is wick; upper/lower ignored)"
            )
        parts.append(f"SELL: {sell_shape} + {sell_ctx}")
    note = " | ".join(parts) if parts else "Both BUY and SELL setups disabled"
    if (config.enable_buy and not config.buy_require_wick) or (
        config.enable_sell and not config.sell_require_wick
    ):
        note += " | SL still at candle low (BUY) / high (SELL) — long wicks still set stop distance"
    return note


def describe_hammer_context_entry_exit(config: HammerContextConfig) -> str:
    c_rule = logic.coerce_entry_rule(config.entry_rule)
    s_rule = logic.coerce_entry_rule(config.inverted_entry_rule)
    return (
        f"BUY entry={c_rule.value} offset=${config.entry_offset:g} "
        f"SL={config.buffer_mode.value} | "
        f"SELL entry={s_rule.value} offset=${config.inverted_entry_offset:g} "
        f"SL={config.inverted_buffer_mode.value}"
    )


def prior_closes_ok_for_buy(
    candles: List[logic.Candle],
    signal_index: int,
    lookback: int,
) -> Tuple[bool, str]:
    """Every prior candle close must be >= hammer low (not below hammer low)."""
    signal = candles[signal_index]
    start = signal_index - lookback
    for j in range(start, signal_index):
        prev = candles[j]
        if prev.close < signal.low:
            return False, (
                f"Context fail BUY: bar {j} close {prev.close:.2f} "
                f"below hammer low {signal.low:.2f}"
            )
    return True, ""


def prior_closes_ok_for_sell(
    candles: List[logic.Candle],
    signal_index: int,
    lookback: int,
) -> Tuple[bool, str]:
    """Every prior candle close must be <= hammer high (not above hammer high)."""
    signal = candles[signal_index]
    start = signal_index - lookback
    for j in range(start, signal_index):
        prev = candles[j]
        if prev.close > signal.high:
            return False, (
                f"Context fail SELL: bar {j} close {prev.close:.2f} "
                f"above hammer high {signal.high:.2f}"
            )
    return True, ""


def detect_buy_setup(
    candles: List[logic.Candle],
    signal_index: int,
    config: HammerContextConfig,
) -> Optional[str]:
    """Returns None if valid BUY context+shape; otherwise ignore reason."""
    if not config.enable_buy:
        return "BUY setup disabled"
    lookback = config.lookback_candles
    if signal_index < lookback:
        return f"Need {lookback} candles before signal bar"
    signal = candles[signal_index]
    if not config.buy_require_wick:
        if not signal.is_green:
            return "Not a green candle"
        ok_body, msg = body_only_ok(
            signal, config.buy_hammer_ratios, config.min_range,
        )
        if not ok_body:
            return msg
    else:
        hr = logic.check_hammer(signal, config.to_buy_shape_config())
        if hr.direction != logic.TradeDirection.BUY:
            return "Not a classic green hammer"
        if hr.hammer_variant != logic.HammerVariant.CLASSIC:
            return "Not classic hammer shape"
    ok, msg = prior_closes_ok_for_buy(candles, signal_index, lookback)
    if not ok:
        return msg
    return None


def detect_sell_setup(
    candles: List[logic.Candle],
    signal_index: int,
    config: HammerContextConfig,
) -> Optional[str]:
    if not config.enable_sell:
        return "SELL setup disabled"
    lookback = config.lookback_candles
    if signal_index < lookback:
        return f"Need {lookback} candles before signal bar"
    signal = candles[signal_index]
    if not config.sell_require_wick:
        if not signal.is_red:
            return "Not a red candle"
        ok_body, msg = body_only_ok(
            signal, config.sell_hammer_ratios, config.min_range,
        )
        if not ok_body:
            return msg
    else:
        hr = logic.check_hammer(signal, config.to_sell_shape_config())
        if hr.direction != logic.TradeDirection.SELL:
            return "Not an inverted red hammer"
        if hr.hammer_variant != logic.HammerVariant.INVERTED:
            return "Not inverted hammer shape"
    ok, msg = prior_closes_ok_for_sell(candles, signal_index, lookback)
    if not ok:
        return msg
    return None


def build_context_signal(
    direction: logic.TradeDirection,
    signal_candle: logic.Candle,
    next_candle: logic.Candle,
    timeframe: str,
    config: HammerContextConfig,
) -> Optional[logic.TradeSignal]:
    if timeframe not in config.timeframe_settings:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. "
            f"Available: {list(config.timeframe_settings.keys())}"
        )
    tf_setting = config.timeframe_settings[timeframe]
    is_buy = direction == logic.TradeDirection.BUY
    trade_cfg = config.to_buy_trade_config() if is_buy else config.to_sell_trade_config()
    variant = logic.HammerVariant.CLASSIC if is_buy else logic.HammerVariant.INVERTED
    wick_required = config.buy_require_wick if is_buy else config.sell_require_wick
    stored_variant = variant.value if wick_required else "BODY_ONLY"

    entry_price = logic.calculate_entry_price(
        signal_candle, next_candle, trade_cfg, variant,
    )
    stop_loss = logic.calculate_stop_loss(
        signal_candle, direction, trade_cfg, variant,
    )

    if direction == logic.TradeDirection.BUY:
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price

    ignored = False
    ignore_reason = None
    if config.reject_zero_or_negative_risk and risk <= 0:
        ignored = True
        ignore_reason = "Risk is zero or negative — check entry vs SL."
    elif config.enable_risk_limit and risk > tf_setting.max_sl_usd:
        ignored = True
        ignore_reason = (
            f"Risk (${risk:.2f}) exceeds max SL (${tf_setting.max_sl_usd}) for '{timeframe}'."
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
        pattern_variant=stored_variant,
    )


def run_strategy(
    candles: List[logic.Candle],
    timeframe: str,
    config: Optional[HammerContextConfig] = None,
) -> List[logic.TradeSignal]:
    if config is None:
        config = HammerContextConfig()

    lookback = config.lookback_candles
    signals: List[logic.TradeSignal] = []
    min_index = lookback
    max_index = len(candles) - 2  # need entry bar after signal

    for i in range(min_index, max_index + 1):
        signal_candle = candles[i]
        next_candle = candles[i + 1]

        buy_fail = detect_buy_setup(candles, i, config)
        if buy_fail is None:
            sig = build_context_signal(
                logic.TradeDirection.BUY,
                signal_candle,
                next_candle,
                timeframe,
                config,
            )
            if sig is not None:
                signals.append(sig)
            continue

        sell_fail = detect_sell_setup(candles, i, config)
        if sell_fail is None:
            sig = build_context_signal(
                logic.TradeDirection.SELL,
                signal_candle,
                next_candle,
                timeframe,
                config,
            )
            if sig is not None:
                signals.append(sig)
            continue

        # Optional: record ignored shape matches for debugging (skip — backtest lists taken only)

    return signals
