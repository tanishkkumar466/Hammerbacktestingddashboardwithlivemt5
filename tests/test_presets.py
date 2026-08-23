"""
Validate example / saved preset JSON against the real algo constructors.

Presets must be able to build StrategyConfig / HammerContextConfig / BacktestConfig
the same way the dashboard does when the client Loads a preset and Runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backtest
import hammer_context_logic as hc
import logic
from indicators.config import IndicatorCombineMode, IndicatorStackConfig, SuperTrendConfig, VWAPConfig

ROOT = Path(__file__).resolve().parents[1]
PRESET_PATHS = [
    ROOT / "presets" / "preset_example_hammer_green_buy_red_sell.json",
    ROOT / "template" / "preset_example_hammer_green_buy_red_sell.json",
    ROOT / "presets" / "preset_example_hammer_with_candles.json",
    ROOT / "template" / "preset_example_hammer_with_candles.json",
]


def _f(fields: dict, key: str, default=None):
    if key not in fields or fields[key] in ("", None):
        return default
    return fields[key]


def _bool(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
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
        out[tf] = logic.TimeframeSetting(
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
    return hc.HammerContextConfig(
        buy_hammer_ratios=_ratio_from_fields(fields, "buy"),
        sell_hammer_ratios=_ratio_from_fields(fields, "sell"),
        lookback_candles=_int(_f(fields, "context_lookback_candles"), 5),
        enable_buy=_bool(_f(fields, "context_enable_buy"), True),
        enable_sell=_bool(_f(fields, "context_enable_sell"), True),
        buy_require_wick=_bool(_f(fields, "buy_require_wick"), True),
        sell_require_wick=_bool(_f(fields, "sell_require_wick"), True),
        entry_rule=logic.coerce_entry_rule(_f(fields, "entry_rule", "NEXT_CANDLE_OPEN")),
        entry_offset=_float(_f(fields, "entry_offset"), 0.0),
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


def build_indicator_stack(data: dict) -> IndicatorStackConfig:
    fields = data.get("fields") or {}
    inds = data.get("indicators") or {}
    st = inds.get("supertrend") or {}
    vw = inds.get("vwap") or {}
    added = set(data.get("indicators_added") or [])
    combine = str(
        _f(fields, "indicators_combine_mode", inds.get("combine_mode", "ALL")) or "ALL"
    )
    return IndicatorStackConfig(
        combine_mode=IndicatorCombineMode(combine),
        supertrend=SuperTrendConfig(
            enabled="supertrend" in added or _bool(st.get("enabled"), False),
            atr_period=_int(
                _f(fields, "indicators_st_atr_period", st.get("atr_period")), 10
            ),
            multiplier=_float(
                _f(fields, "indicators_st_multiplier", st.get("multiplier")), 3.0
            ),
            apply_trade_filter=_bool(
                _f(fields, "indicators_st_apply_filter", st.get("apply_trade_filter")),
                True,
            ),
        ),
        vwap=VWAPConfig(
            enabled="vwap" in added or _bool(vw.get("enabled"), False),
            apply_trade_filter=_bool(
                _f(fields, "indicators_vwap_apply_filter", vw.get("apply_trade_filter")),
                True,
            ),
        ),
    )


def build_backtest_config(data: dict, strategy) -> backtest.BacktestConfig:
    bt = data.get("backtest") or {}
    fields = data.get("fields") or {}
    pattern = data.get("pattern", "Hammer")
    if pattern == "Hammer with candles":
        pattern_type = "hammer_with_candles"
    elif pattern == "Doji":
        pattern_type = "doji"
    else:
        pattern_type = "hammer"

    market_raw = str(bt.get("market_data") or fields.get("market_data") or "spot").lower()
    if market_raw in ("auto", "legacy", ""):
        market_raw = "spot"
    market = backtest.MarketDataSource(market_raw)

    enabled_folders = []
    folder_map = {
        "1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min",
        "15m": "15min", "30m": "30min", "1h": "1hour",
    }
    for tf, cfg in (data.get("timeframes") or {}).items():
        if _bool(cfg.get("enabled"), False):
            enabled_folders.append(folder_map.get(tf, tf))

    sess = data.get("sessions") or {}
    sessions_enabled = {
        name: _bool(sess.get(name), True) for name in ("Asian", "London", "US")
    }

    return backtest.BacktestConfig(
        pattern_type=pattern_type,
        strategy_config=strategy,
        indicator_stack=build_indicator_stack(data),
        symbol=str(bt.get("symbol") or fields.get("symbol") or "XAUUSD"),
        data_root=str(bt.get("data_root") or fields.get("data_root") or "data"),
        market_data=market,
        timeframes_to_test=enabled_folders or ["3min"],
        max_forward_candles=_int(
            bt.get("max_forward_candles") or fields.get("max_forward_candles"), 5000
        ),
        position_sizing_mode=backtest.PositionSizingMode(
            str(bt.get("position_sizing_mode") or "FIXED_RISK_USD")
        ),
        position_size=_float(bt.get("position_size"), 1.0),
        fixed_risk_usd=_float(bt.get("fixed_risk_usd"), 100.0),
        risk_pct_of_equity=_float(bt.get("risk_pct_of_equity"), 1.0),
        starting_capital=_float(bt.get("starting_capital"), 10000.0),
        equity_floor_usd=_float(bt.get("equity_floor_usd"), 0.0),
        max_equity_multiple=_float(bt.get("max_equity_multiple"), 50.0),
        allow_overlapping_trades=_bool(bt.get("allow_overlapping_trades"), False),
        commission_per_trade=_float(bt.get("commission_per_trade"), 0.0),
        slippage_usd=_float(bt.get("slippage_usd"), 0.0),
        sessions_enabled=sessions_enabled,
        session_clock=str(sess.get("session_clock") or "broker"),
        ist_time_filter_enabled=_bool(sess.get("ist_time_filter_enabled"), False),
        ist_time_start=str(sess.get("ist_time_start") or "09:15"),
        ist_time_end=str(sess.get("ist_time_end") or "15:30"),
    )


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("path", [p for p in PRESET_PATHS if p.is_file()], ids=lambda p: p.name + "@" + p.parent.name)
def test_preset_json_builds_strict_algo_configs(path: Path):
    data = _load(path)
    assert data.get("version") == 2
    assert data.get("pattern") in ("Hammer", "Hammer with candles", "Doji")
    assert data.get("fields"), "fields required for algo params"
    assert data.get("timeframes"), "timeframes required"
    assert data.get("backtest"), "backtest block required"
    assert data.get("sessions"), "sessions required for strict live/backtest parity"
    assert (data["backtest"].get("market_data") or data["fields"].get("market_data")) in (
        "spot", "futures", "both",
    )

    enabled = [
        tf for tf, cfg in data["timeframes"].items() if _bool(cfg.get("enabled"), False)
    ]
    assert enabled, "at least one timeframe must be enabled"

    pattern = data["pattern"]
    if pattern == "Hammer":
        strategy = build_hammer_strategy(data)
        assert strategy.classic_green == logic.TradeAction.BUY
        assert strategy.classic_red == logic.TradeAction.SELL
        assert strategy.entry_rule == logic.EntryRule.NEXT_CANDLE_OPEN
        assert strategy.sl_mode == logic.StopLossMode.CANDLE_EXTREME
        # Smoke: describe rules (used by live summary / presets)
        assert "BUY" in logic.describe_hammer_direction_matrix(strategy)
        stack = build_indicator_stack(data)
        assert stack.supertrend.enabled and stack.supertrend.apply_trade_filter
        assert stack.vwap.enabled and stack.vwap.apply_trade_filter
    elif pattern == "Hammer with candles":
        strategy = build_hammer_context_strategy(data)
        assert strategy.lookback_candles == 5
        assert strategy.enable_buy and strategy.enable_sell
        assert strategy.buy_require_wick and strategy.sell_require_wick
        assert strategy.entry_rule == logic.EntryRule.NEXT_CANDLE_OPEN
        assert strategy.sl_mode == logic.StopLossMode.CANDLE_EXTREME
        assert "BUY" in hc.describe_hammer_context_rules(strategy)
    else:
        pytest.skip("Doji example not in this suite")

    cfg = build_backtest_config(data, strategy)
    err = backtest.validate_position_sizing_config(cfg)
    assert err is None, err
    assert cfg.market_data == backtest.MarketDataSource.SPOT
    assert cfg.allow_overlapping_trades is False
    assert any(cfg.sessions_enabled.values())


def test_presets_folder_has_examples():
    presets = ROOT / "presets"
    assert (presets / "preset_example_hammer_green_buy_red_sell.json").is_file()
    assert (presets / "preset_example_hammer_with_candles.json").is_file()
