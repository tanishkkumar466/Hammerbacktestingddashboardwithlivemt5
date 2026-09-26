"""
API trading runtime — real orders via the same stack as Live.

Bridge (what “order bridge” means):
  C++ engine only launches a hidden /portable terminal.
  Real trading = Python MetaTrader5 API (broker.py) inside AccountWorkerHandle
  → LiveTradingEngine (monitor candles + signals) → mt5.order_send.

That is the Excel-VBA-macro model: keep watching, place orders when rules fire.
API Accounts reuse Live’s worker + engine; they do NOT use C++ /orders/batch fills.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import doji_logic
import hammer_context_logic as hc
import live as live_trading
import logic
from broker import BrokerCredentials
from indicators.config import (
    IndicatorCombineMode,
    IndicatorStackConfig,
    RollingVWAPConfig,
    RSIConfig,
    SuperTrendConfig,
    VWAPConfig,
)

from .accounts import ApiAccount
from .bindings import StrategyBinding, coerce_order_mode
from .paths import is_isolated_terminal_path
from .preset_meta import read_preset_file


def api_worker_key(account_id: str) -> str:
    """Namespace so API handles never collide with Live desk account ids."""
    return f"api:{account_id}"


def api_slot_id(binding_id: str) -> str:
    return f"api-{binding_id}"


def _f(fields: dict, key: str, default=None):
    if key not in fields or fields[key] in ("", None):
        return default
    return fields[key]


def _bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return default if v is None else v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _ratio_from_fields(fields: dict, prefix: str = "") -> logic.HammerRatioConfig:
    p = f"{prefix}_" if prefix else ""
    return logic.HammerRatioConfig(
        body_pct=_float(_f(fields, f"{p}body_pct"), 20.0),
        body_tol=_float(_f(fields, f"{p}body_tol"), 25.0),
        body_tol_is_symmetric=_bool(_f(fields, f"{p}body_tol_is_symmetric"), True),
        body_tol_lower=_float(_f(fields, f"{p}body_tol_lower"), 20.0),
        body_tol_upper=_float(_f(fields, f"{p}body_tol_upper"), 25.0),
        dominant_wick_pct=_float(_f(fields, f"{p}dominant_wick_pct"), 60.0),
        dominant_wick_tol=_float(_f(fields, f"{p}dominant_wick_tol"), 18.0),
        dominant_tol_is_symmetric=_bool(_f(fields, f"{p}dominant_tol_is_symmetric"), True),
        dominant_tol_lower=_float(_f(fields, f"{p}dominant_tol_lower"), 18.0),
        dominant_tol_upper=_float(_f(fields, f"{p}dominant_tol_upper"), 18.0),
        small_wick_pct=_float(_f(fields, f"{p}small_wick_pct"), 20.0),
        small_wick_tol=_float(_f(fields, f"{p}small_wick_tol"), 18.0),
        small_tol_is_symmetric=_bool(_f(fields, f"{p}small_tol_is_symmetric"), True),
        small_tol_lower=_float(_f(fields, f"{p}small_tol_lower"), 18.0),
        small_tol_upper=_float(_f(fields, f"{p}small_tol_upper"), 18.0),
    )


def _timeframes_from_preset(data: dict) -> dict:
    out = {}
    for tf, cfg in (data.get("timeframes") or {}).items():
        if not isinstance(cfg, dict):
            continue
        out[str(tf)] = logic.TimeframeSetting(
            rr_multiple=_float(cfg.get("rr"), 2.0),
            max_sl_usd=_float(cfg.get("sl"), 20.0),
        )
    for tf, cfg in (data.get("custom_timeframes") or {}).items():
        if not isinstance(cfg, dict):
            continue
        out[str(tf)] = logic.TimeframeSetting(
            rr_multiple=_float(cfg.get("rr"), 2.0),
            max_sl_usd=_float(cfg.get("sl"), 20.0),
        )
    return out or dict(logic.DEFAULT_TIMEFRAME_SETTINGS)


def build_hammer_strategy(data: dict) -> logic.StrategyConfig:
    fields = data.get("fields") or {}
    return logic.StrategyConfig(
        hammer_ratios=_ratio_from_fields(fields),
        hammer_wick_side=logic.WickSide(str(_f(fields, "hammer_wick_side", "LOWER"))),
        classic_green=logic.coerce_trade_action(_f(fields, "classic_green", "BUY")),
        classic_red=logic.coerce_trade_action(_f(fields, "classic_red", "SELL")),
        inverted_green=logic.coerce_trade_action(_f(fields, "inverted_green", "BUY")),
        inverted_red=logic.coerce_trade_action(_f(fields, "inverted_red", "SELL")),
        entry_rule=logic.coerce_entry_rule(_f(fields, "entry_rule", "NEXT_CANDLE_OPEN")),
        entry_offset=_float(_f(fields, "entry_offset"), 0.0),
        sl_mode=logic.coerce_stop_loss_mode(_f(fields, "sl_mode", "CANDLE_EXTREME")),
        sl_fixed_distance=_float(_f(fields, "sl_fixed_distance"), 5.0),
        buffer_mode=logic.coerce_buffer_mode(_f(fields, "buffer_mode", "PERCENT_OF_RANGE")),
        sl_buffer_pct=_float(_f(fields, "sl_buffer_pct"), 5.0),
        sl_buffer_flat=_float(_f(fields, "sl_buffer_flat"), 0.0),
        inverted_entry_rule=logic.coerce_entry_rule(
            _f(fields, "inverted_entry_rule", _f(fields, "entry_rule", "NEXT_CANDLE_OPEN"))
        ),
        inverted_entry_offset=_float(_f(fields, "inverted_entry_offset"), 0.0),
        inverted_sl_mode=logic.coerce_stop_loss_mode(
            _f(fields, "inverted_sl_mode", _f(fields, "sl_mode", "CANDLE_EXTREME"))
        ),
        inverted_sl_fixed_distance=_float(_f(fields, "inverted_sl_fixed_distance"), 5.0),
        inverted_buffer_mode=logic.coerce_buffer_mode(
            _f(fields, "inverted_buffer_mode", _f(fields, "buffer_mode", "PERCENT_OF_RANGE"))
        ),
        inverted_sl_buffer_pct=_float(_f(fields, "inverted_sl_buffer_pct"), 5.0),
        inverted_sl_buffer_flat=_float(_f(fields, "inverted_sl_buffer_flat"), 0.0),
        timeframe_settings=_timeframes_from_preset(data),
        enable_risk_limit=_bool(_f(fields, "enable_risk_limit"), True),
        reject_zero_or_negative_risk=_bool(_f(fields, "reject_zero_or_negative_risk"), True),
    )


def build_hammer_context_strategy(data: dict) -> hc.HammerContextConfig:
    fields = data.get("fields") or {}
    pattern = data.get("pattern", "")
    for_35 = hc.is_35_pattern_label(pattern)
    raw_pull = _f(fields, "entry_pullback_pct")
    pull = hc.resolve_entry_pullback_pct(
        _float(raw_pull, hc.DEFAULT_PULLBACK_PCT_35 if for_35 else 0.0),
        for_35_pattern=for_35,
    )
    return hc.HammerContextConfig(
        buy_hammer_ratios=_ratio_from_fields(fields, "buy"),
        sell_hammer_ratios=_ratio_from_fields(fields, "sell"),
        lookback_candles=_int(_f(fields, "context_lookback_candles"), 5),
        enable_buy=_bool(_f(fields, "context_enable_buy"), True),
        enable_sell=_bool(_f(fields, "context_enable_sell"), True),
        buy_require_wick=_bool(_f(fields, "buy_require_wick"), True),
        sell_require_wick=_bool(_f(fields, "sell_require_wick"), True),
        buy_min_lower_wick_enabled=_bool(_f(fields, "buy_min_lower_wick_enabled"), False),
        buy_min_lower_wick_pct=_float(_f(fields, "buy_min_lower_wick_pct"), 35.0),
        sell_min_upper_wick_enabled=_bool(_f(fields, "sell_min_upper_wick_enabled"), False),
        sell_min_upper_wick_pct=_float(_f(fields, "sell_min_upper_wick_pct"), 35.0),
        entry_rule=logic.coerce_entry_rule(_f(fields, "entry_rule", "NEXT_CANDLE_OPEN")),
        entry_offset=_float(_f(fields, "entry_offset"), 0.0),
        entry_pullback_pct=pull,
        sl_mode=logic.coerce_stop_loss_mode(_f(fields, "sl_mode", "CANDLE_EXTREME")),
        sl_fixed_distance=_float(_f(fields, "sl_fixed_distance"), 5.0),
        buffer_mode=logic.coerce_buffer_mode(_f(fields, "buffer_mode", "PERCENT_OF_RANGE")),
        sl_buffer_pct=_float(_f(fields, "sl_buffer_pct"), 5.0),
        sl_buffer_flat=_float(_f(fields, "sl_buffer_flat"), 0.0),
        inverted_entry_rule=logic.coerce_entry_rule(
            _f(fields, "inverted_entry_rule", "NEXT_CANDLE_OPEN")
        ),
        inverted_entry_offset=_float(_f(fields, "inverted_entry_offset"), 0.0),
        inverted_sl_mode=logic.coerce_stop_loss_mode(
            _f(fields, "inverted_sl_mode", "CANDLE_EXTREME")
        ),
        inverted_sl_fixed_distance=_float(_f(fields, "inverted_sl_fixed_distance"), 5.0),
        inverted_buffer_mode=logic.coerce_buffer_mode(
            _f(fields, "inverted_buffer_mode", "PERCENT_OF_RANGE")
        ),
        inverted_sl_buffer_pct=_float(_f(fields, "inverted_sl_buffer_pct"), 5.0),
        inverted_sl_buffer_flat=_float(_f(fields, "inverted_sl_buffer_flat"), 0.0),
        timeframe_settings=_timeframes_from_preset(data),
        enable_risk_limit=_bool(_f(fields, "enable_risk_limit"), True),
        reject_zero_or_negative_risk=_bool(_f(fields, "reject_zero_or_negative_risk"), True),
    )


def _enum(enum_cls, raw, default):
    try:
        return enum_cls(raw)
    except (ValueError, TypeError):
        return default


def build_doji_strategy(data: dict) -> doji_logic.DojiStrategyConfig:
    fields = data.get("fields") or {}
    rd = doji_logic.DojiRatioConfig()
    ratios = doji_logic.DojiRatioConfig(**{
        f.name: _float(_f(fields, f"doji_{f.name}"), getattr(rd, f.name))
        for f in dataclasses.fields(doji_logic.DojiRatioConfig)
    })
    d = doji_logic.DojiStrategyConfig()
    return doji_logic.DojiStrategyConfig(
        doji_ratios=ratios,
        doji_style=_enum(doji_logic.DojiStyle, _f(fields, "doji_style"), d.doji_style),
        doji_direction_mode=_enum(
            doji_logic.DojiDirectionMode, _f(fields, "doji_direction_mode"), d.doji_direction_mode,
        ),
        fallback_direction=_enum(
            logic.TradeDirection, _f(fields, "doji_fallback_direction"), d.fallback_direction,
        ),
        green_direction=_enum(logic.TradeDirection, _f(fields, "doji_green_direction"), d.green_direction),
        red_direction=_enum(logic.TradeDirection, _f(fields, "doji_red_direction"), d.red_direction),
        allow_green_trades=_bool(_f(fields, "doji_allow_green_trades"), d.allow_green_trades),
        allow_red_trades=_bool(_f(fields, "doji_allow_red_trades"), d.allow_red_trades),
        entry_rule=_enum(logic.EntryRule, _f(fields, "entry_rule"), d.entry_rule),
        entry_offset=_float(_f(fields, "entry_offset"), d.entry_offset),
        sl_mode=_enum(logic.StopLossMode, _f(fields, "sl_mode"), d.sl_mode),
        sl_fixed_distance=_float(_f(fields, "sl_fixed_distance"), d.sl_fixed_distance),
        buffer_mode=_enum(logic.BufferMode, _f(fields, "buffer_mode"), d.buffer_mode),
        sl_buffer_pct=_float(_f(fields, "sl_buffer_pct"), d.sl_buffer_pct),
        sl_buffer_flat=_float(_f(fields, "sl_buffer_flat"), d.sl_buffer_flat),
        timeframe_settings=_timeframes_from_preset(data),
        enable_risk_limit=_bool(_f(fields, "enable_risk_limit"), d.enable_risk_limit),
        reject_zero_or_negative_risk=_bool(
            _f(fields, "reject_zero_or_negative_risk"), d.reject_zero_or_negative_risk,
        ),
    )


def build_indicator_stack(data: dict) -> IndicatorStackConfig:
    fields = data.get("fields") or {}
    inds = data.get("indicators") or {}
    st = inds.get("supertrend") or {}
    vw = inds.get("vwap") or {}
    rv = inds.get("rolling_vwap") or {}
    rsi = inds.get("rsi") or {}
    added = set(data.get("indicators_added") or [])
    from indicators.rolling_vwap import coerce_rolling_period
    from indicators.rsi import coerce_rsi_level, coerce_rsi_period

    combine = str(
        _f(fields, "indicators_combine_mode", inds.get("combine_mode", "ALL")) or "ALL"
    )
    return IndicatorStackConfig(
        combine_mode=IndicatorCombineMode(combine),
        supertrend=SuperTrendConfig(
            enabled="supertrend" in added or _bool(st.get("enabled"), False),
            atr_period=_int(_f(fields, "indicators_st_atr_period", st.get("atr_period")), 10),
            multiplier=_float(_f(fields, "indicators_st_multiplier", st.get("multiplier")), 3.0),
            apply_trade_filter=_bool(
                _f(fields, "indicators_st_apply_filter", st.get("apply_trade_filter")), True
            ),
        ),
        vwap=VWAPConfig(
            enabled="vwap" in added or _bool(vw.get("enabled"), False),
            apply_trade_filter=_bool(
                _f(fields, "indicators_vwap_apply_filter", vw.get("apply_trade_filter")), True
            ),
        ),
        rolling_vwap=RollingVWAPConfig(
            enabled="rolling_vwap" in added or _bool(rv.get("enabled"), False),
            period=coerce_rolling_period(
                _int(_f(fields, "indicators_rolling_vwap_period", rv.get("period")), 20)
            ),
            apply_trade_filter=_bool(
                _f(fields, "indicators_rolling_vwap_apply_filter", rv.get("apply_trade_filter")),
                True,
            ),
        ),
        rsi=RSIConfig(
            enabled="rsi" in added or _bool(rsi.get("enabled"), False),
            period=coerce_rsi_period(_int(_f(fields, "indicators_rsi_period", rsi.get("period")), 14)),
            buy_above=coerce_rsi_level(
                _float(_f(fields, "indicators_rsi_buy_above", rsi.get("buy_above")), 50.0), 50.0
            ),
            sell_below=coerce_rsi_level(
                _float(_f(fields, "indicators_rsi_sell_below", rsi.get("sell_below")), 60.0), 60.0
            ),
            apply_trade_filter=_bool(
                _f(fields, "indicators_rsi_apply_filter", rsi.get("apply_trade_filter")), True
            ),
        ),
    )


def strategy_from_preset(data: dict) -> Tuple[str, str, Any, IndicatorStackConfig]:
    """Return (pattern_label, pattern_type, strategy_config, indicator_stack)."""
    pattern = str(data.get("pattern") or "Hammer")
    stack = build_indicator_stack(data)
    if hc.is_context_pattern_label(pattern):
        return pattern, hc.pattern_type_for_label(pattern), build_hammer_context_strategy(data), stack
    if pattern == "Doji":
        return pattern, "doji", build_doji_strategy(data), stack
    return pattern, "hammer", build_hammer_strategy(data), stack


def credentials_for_api_account(
    acc: ApiAccount,
    terminal_path: str = "",
) -> BrokerCredentials:
    login_raw = (acc.login or "").strip()
    login = int(login_raw) if login_raw.isdigit() else 0
    path = (terminal_path or acc.terminal_path or "").strip()
    return BrokerCredentials(
        terminal_path=path,
        login=login,
        password=acc.password or "",
        server=(acc.server or "").strip(),
        portable=is_isolated_terminal_path(path),
    )


SHARED_LIVE_SETTING_KEYS = frozenset({
    "max_open_positions",
    "max_daily_trades",
    "max_daily_loss_usd",
    "min_minutes_between_trades",
    "max_spread_points",
    "demo_accounts_only",
    "max_lot_size",
    "price_deviation_points",
    "max_entry_deviation_points",
    "fallback_to_market_on_limit_fail",
    "poll_interval_sec",
    "history_bars",
    "telegram_enabled",
    "telegram_bot_token",
    "telegram_chat_id",
    "notification_bots",
})


def shared_live_settings_from(live_cfg: Any) -> Dict[str, Any]:
    """Pick the Live-tab safety + notification settings that API accounts share."""
    return {k: getattr(live_cfg, k) for k in SHARED_LIVE_SETTING_KEYS if hasattr(live_cfg, k)}


@dataclass
class ApiTradeStart:
    """Payload for AccountWorkerHandle connect + start_slot (same as Live)."""

    worker_key: str
    slot_id: str
    credentials: BrokerCredentials
    live_config: live_trading.LiveRunConfig
    pattern_label: str
    pattern_type: str
    strategy_config: Any
    indicator_stack: IndicatorStackConfig


def build_api_trade_start(
    acc: ApiAccount,
    binding: StrategyBinding,
    *,
    presets_dir: str,
    terminal_path: str = "",
    journal_dir: str = "",
    dry_run: Optional[bool] = None,
    shared_live_settings: Optional[Dict[str, Any]] = None,
) -> ApiTradeStart:
    """
    Build a full Live-compatible start payload from an API account + strategy binding.
    Orders will go through MT5Broker.order_send inside the account worker.

    shared_live_settings: safety limits / notifications from the Live tab
    (keys in SHARED_LIVE_SETTING_KEYS); missing keys keep the defaults below.
    """
    if not (acc.login or "").strip() or not (acc.server or "").strip():
        raise ValueError("API account needs login and server.")
    if not acc.login.strip().isdigit():
        raise ValueError(f"Login '{acc.login.strip()}' must be the numeric MT5 account number.")
    if not (acc.password or "").strip():
        raise ValueError("Enter the account password (session) before Start trading.")
    if not (binding.preset_file or "").strip():
        raise ValueError("Assign a strategy preset first.")
    if not (binding.timeframe or "").strip():
        raise ValueError("Assign a timeframe on the strategy binding.")

    path = os.path.join(presets_dir, binding.preset_file)
    data, err = read_preset_file(path)
    if data is None:
        raise FileNotFoundError(err or f"Preset not found: {path}")

    pattern_label, pattern_type, strategy_config, indicator_stack = strategy_from_preset(data)
    tf_settings = getattr(strategy_config, "timeframe_settings", None) or {}
    if binding.timeframe not in tf_settings:
        # Still allow start — RR/SL may fall back; warn via caller
        pass

    raw_volume = str(binding.volume or "0.01").strip().replace(",", ".")
    try:
        volume = float(raw_volume)
    except (TypeError, ValueError):
        raise ValueError(f"Volume '{binding.volume}' is not a number (lots, e.g. 0.01).") from None
    if not volume > 0:
        raise ValueError(f"Volume must be above 0 lots (got {binding.volume}).")

    sess = data.get("sessions") or {}
    sessions_enabled = [
        name for name in ("Asian", "London", "US") if _bool(sess.get(name), True)
    ]
    use_dry = bool(getattr(binding, "dry_run", True)) if dry_run is None else bool(dry_run)

    live_cfg = live_trading.LiveRunConfig(
        symbol=(binding.symbol or "XAUUSD").strip() or "XAUUSD",
        timeframe_label=(binding.timeframe or "").strip(),
        volume=volume,
        magic=int(binding.magic or 1100),
        max_open_positions=1,
        poll_interval_sec=2.0,
        dry_run=use_dry,
        history_bars=400,
        max_daily_trades=20,
        max_daily_loss_usd=100.0,
        min_minutes_between_trades=0.0,
        max_spread_points=50.0,
        demo_accounts_only=False,
        max_lot_size=1.0,
        use_thread_pool_signal_cpu=True,
        use_ray_if_available=False,
        journal_dir=(journal_dir or "").strip(),
        order_mode=coerce_order_mode(getattr(binding, "order_mode", "market")),
        limit_offset_points=float(getattr(binding, "limit_offset_points", 0.0) or 0.0),
        order_comment=f"API-{acc.name[:12]}"[:31],
        sessions_enabled=sessions_enabled or None,
        session_clock=str(sess.get("session_clock") or "broker"),
        ist_time_filter_enabled=_bool(sess.get("ist_time_filter_enabled"), False),
        ist_time_start=str(sess.get("ist_time_start") or "00:00"),
        ist_time_end=str(sess.get("ist_time_end") or "23:59"),
        slot_id=api_slot_id(binding.id),
        slot_name=f"{acc.name}/{binding.timeframe}",
        preset_name=str(binding.preset_file or ""),
        account_id=acc.id,
        account_name=acc.name,
        notify_enabled=True,
    )
    for key, value in (shared_live_settings or {}).items():
        if key in SHARED_LIVE_SETTING_KEYS:
            setattr(live_cfg, key, value)
    risk_err = live_trading.validate_risk_config(live_cfg, real_money=not use_dry)
    if risk_err:
        raise ValueError(f"{risk_err} (Live tab safety limits apply to API accounts.)")

    return ApiTradeStart(
        worker_key=api_worker_key(acc.id),
        slot_id=api_slot_id(binding.id),
        credentials=credentials_for_api_account(acc, terminal_path=terminal_path),
        live_config=live_cfg,
        pattern_label=pattern_label,
        pattern_type=pattern_type,
        strategy_config=strategy_config,
        indicator_stack=indicator_stack,
    )


def start_slot_kwargs(start: ApiTradeStart) -> Dict[str, Any]:
    """Keyword args for AccountWorkerHandle.send('start_slot', ...)."""
    return {
        "slot_id": start.slot_id,
        "live_config": start.live_config,
        "pattern_type": start.pattern_type,
        "pattern_label": start.pattern_label,
        "strategy_config": start.strategy_config,
        "indicator_stack": start.indicator_stack,
    }
