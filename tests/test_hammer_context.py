"""Hammer with candles: N prior closes vs hammer low/high."""

from logic import Candle, EntryRule, HammerRatioConfig, TimeframeSetting, TradeDirection
import hammer_context_logic as hc


def _classic_green(ts="sig"):
    return Candle(ts, 100.0, 103.5, 90.0, 102.0)


def _inverted_red(ts="sig"):
    return Candle(ts, 102.0, 108.0, 99.6, 100.0)


def _pad(ts, close):
    """Bullish pad candle (close > open) with room around the close."""
    return Candle(ts, close - 0.3, close + 0.5, close - 0.5, close)


def _pad_red(ts, close):
    """Bearish pad candle (open > close)."""
    return Candle(ts, close + 0.4, close + 0.5, close - 0.5, close)


def test_buy_requires_prior_closes_not_below_hammer_low():
    cfg = hc.HammerContextConfig(
        lookback_candles=3,
        enable_sell=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    signal = _classic_green("s")
    bad_prev = _pad("p2", 89.0)
    good = [
        _pad("p0", 95.0),
        _pad("p1", 94.0),
        _pad("p2", 91.0),
        signal,
        Candle("n", 102.0, 103.0, 101.0, 102.5),
    ]
    assert hc.detect_buy_setup(good, 3, cfg) is None

    bad = [
        _pad("p0", 95.0),
        _pad("p1", 94.0),
        bad_prev,
        signal,
        Candle("n", 102.0, 103.0, 101.0, 102.5),
    ]
    assert hc.detect_buy_setup(bad, 3, cfg) is not None

    sigs = hc.run_strategy(good, "3m", cfg)
    taken = [s for s in sigs if not s.ignored]
    assert len(taken) == 1
    assert taken[0].direction == TradeDirection.BUY


def test_sell_requires_prior_closes_not_above_hammer_high():
    cfg = hc.HammerContextConfig(
        lookback_candles=2,
        enable_buy=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    signal = _inverted_red("s")
    good = [
        _pad("p0", 105.0),
        _pad("p1", 106.0),
        signal,
        Candle("n", 100.0, 101.0, 99.0, 100.5),
    ]
    assert hc.detect_sell_setup(good, 2, cfg) is None

    bad = [
        _pad("p0", 105.0),
        _pad("p1", 109.0),
        signal,
        Candle("n", 100.0, 101.0, 99.0, 100.5),
    ]
    assert hc.detect_sell_setup(bad, 2, cfg) is not None

    sigs = hc.run_strategy(good, "3m", cfg)
    taken = [s for s in sigs if not s.ignored]
    assert len(taken) == 1
    assert taken[0].direction == TradeDirection.SELL


def test_lookback_count_configurable():
    cfg = hc.HammerContextConfig(lookback_candles=1, enable_sell=False, enable_risk_limit=False)
    signal = _classic_green("s")
    candles = [_pad("p0", 91.0), signal, Candle("n", 102.0, 103.0, 101.0, 102.5)]
    assert hc.detect_buy_setup(candles, 1, cfg) is None

    candles[0] = _pad("p0", 91.5)
    assert hc.detect_buy_setup(candles, 1, cfg) is None


def test_separate_entry_rules():
    cfg = hc.HammerContextConfig(
        entry_rule=EntryRule.HAMMER_HIGH,
        inverted_entry_rule=EntryRule.HAMMER_LOW,
        lookback_candles=1,
        enable_buy=True,
        enable_sell=True,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    buy_sig = _classic_green("s")
    buy_candles = [_pad("p0", 92.0), buy_sig, Candle("n", 102.0, 103.0, 101.0, 102.5)]
    buy = [s for s in hc.run_strategy(buy_candles, "3m", cfg) if not s.ignored]
    assert buy and abs(buy[0].entry_price - 103.5) < 1e-9

    sell_sig = _inverted_red("s2")
    sell_candles = [_pad("p0", 107.0), sell_sig, Candle("n", 100.0, 101.0, 99.0, 100.5)]
    sell = [s for s in hc.run_strategy(sell_candles, "3m", cfg) if not s.ignored]
    assert sell and abs(sell[0].entry_price - 99.6) < 1e-9


def test_asymmetric_buy_body_bounds():
    buy = HammerRatioConfig(
        body_pct=18.0,
        body_tol=25.0,
        body_tol_is_symmetric=False,
        body_tol_lower=2.0,
        body_tol_upper=20.0,
    )
    lo, hi = buy.body_bounds()
    assert lo == 16.0
    assert hi == 38.0


def test_body_only_ignores_wick_shape():
    """Wick off: body ≤ cap + previous RED → BUY (signal color ignored)."""
    body_only = HammerRatioConfig(
        body_pct=10.0,
        body_tol=0.0,
        body_tol_is_symmetric=True,
    )
    cfg = hc.HammerContextConfig(
        buy_hammer_ratios=body_only,
        buy_require_wick=False,
        enable_sell=False,
        lookback_candles=1,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    # Signal: green, small body (~9.5%), LONG upper wick — not a classic hammer.
    signal = Candle("s", 100.0, 110.0, 99.5, 101.0)
    candles = [_pad_red("p0", 100.0), signal, Candle("n", 101.0, 102.0, 100.5, 101.5)]
    assert hc.detect_buy_setup(candles, 1, cfg) is None

    # Same signal but previous GREEN → not BUY under wick-off.
    candles_green_prev = [_pad("p0", 100.0), signal, Candle("n", 101.0, 102.0, 100.5, 101.5)]
    assert hc.detect_buy_setup(candles_green_prev, 1, cfg) is not None

    cfg_wick_on = hc.HammerContextConfig(
        buy_require_wick=True,
        enable_sell=False,
        lookback_candles=1,
        enable_risk_limit=False,
    )
    assert hc.detect_buy_setup(candles, 1, cfg_wick_on) is not None

    fat = Candle("s", 100.0, 110.0, 90.0, 108.0)  # body well above 10%
    fat_bars = [_pad_red("p0", 100.0), fat, Candle("n", 108.0, 109.0, 107.0, 108.5)]
    fail = hc.detect_buy_setup(fat_bars, 1, cfg)
    assert fail is not None
    assert "Body" in fail


def test_body_only_sell_red_ignores_lower_wick():
    """Wick off SELL: previous GREEN + body ≤ cap (signal may be any color)."""
    body_only = HammerRatioConfig(body_pct=10.0, body_tol=0.0)
    cfg = hc.HammerContextConfig(
        sell_hammer_ratios=body_only,
        sell_require_wick=False,
        enable_buy=False,
        lookback_candles=1,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    # Signal red with long LOWER wick — not inverted; prev green → SELL.
    signal = Candle("s", 101.0, 101.5, 90.0, 100.0)
    candles = [_pad("p0", 99.0), signal, Candle("n", 100.0, 100.5, 99.0, 99.5)]
    assert hc.detect_sell_setup(candles, 1, cfg) is None

    candles_red_prev = [_pad_red("p0", 99.0), signal, Candle("n", 100.0, 100.5, 99.0, 99.5)]
    assert hc.detect_sell_setup(candles_red_prev, 1, cfg) is not None


def test_body_only_live_pipeline_matches_backtest():
    """Live find_bar_signal_outcome must use the same body-only rules as run_strategy."""
    from indicators.config import IndicatorStackConfig
    from live import find_bar_signal_outcome

    ratios = HammerRatioConfig(body_pct=10.0, body_tol=25.0, body_tol_is_symmetric=True)
    cfg = hc.HammerContextConfig(
        buy_hammer_ratios=ratios,
        sell_hammer_ratios=ratios,
        buy_require_wick=False,
        sell_require_wick=False,
        lookback_candles=2,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    # Immediate previous must be RED for BUY; signal color ignored.
    signal = Candle("s", 100.0, 110.0, 99.5, 101.0)
    closed = [
        _pad("p0", 100.0),
        _pad_red("p1", 100.5),
        signal,
    ]
    forming = Candle("n", 101.0, 102.0, 100.5, 101.5)
    stack = IndicatorStackConfig()

    backtest_sigs = [s for s in hc.run_strategy(closed + [forming], "3m", cfg) if not s.ignored]
    assert len(backtest_sigs) == 1
    assert backtest_sigs[0].direction == TradeDirection.BUY
    assert backtest_sigs[0].pattern_variant == "BODY_ONLY"

    live_sig, ignored = find_bar_signal_outcome(
        closed, forming, "3m", "hammer_with_candles", cfg, stack,
    )
    assert ignored is None
    assert live_sig is not None
    assert live_sig.direction == TradeDirection.BUY
    assert live_sig.pattern_variant == "BODY_ONLY"
    assert abs(live_sig.entry_price - backtest_sigs[0].entry_price) < 1e-9
    assert abs(live_sig.stop_loss - backtest_sigs[0].stop_loss) < 1e-9

    rules = hc.describe_hammer_context_rules(cfg)
    assert "≤ 10" in rules
    assert "prev red" in rules.lower() or "prev green" in rules.lower()

    # Tolerance must not widen the cap when wick is off (20±25 is not 45%).
    wide = HammerRatioConfig(body_pct=10.0, body_tol=25.0)
    fat = Candle("s", 100.0, 110.0, 90.0, 104.0)  # body 20% of range
    assert hc.body_only_ok(fat, wide)[0] is False

    # Wick-on must reject the same long-upper-wick candle.
    cfg_wick = hc.HammerContextConfig(
        buy_hammer_ratios=ratios,
        buy_require_wick=True,
        enable_sell=False,
        lookback_candles=2,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    live_none, _ = find_bar_signal_outcome(
        closed, forming, "3m", "hammer_with_candles", cfg_wick, stack,
    )
    assert live_none is None


def test_lookback_zero_skips_prior_close_check():
    cfg = hc.HammerContextConfig(
        lookback_candles=0,
        enable_sell=False,
        enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    assert cfg.lookback_candles == 0
    signal = _classic_green("s")
    # First bar can be the signal; a close below the low is irrelevant with N=0.
    candles = [signal, Candle("n", 102.0, 103.0, 101.0, 102.5)]
    assert hc.detect_buy_setup(candles, 0, cfg) is None
    taken = [s for s in hc.run_strategy(candles, "3m", cfg) if not s.ignored]
    assert len(taken) == 1
    rules = hc.describe_hammer_context_rules(cfg)
    assert "no prior-candle context" in rules


def test_lookback_ignores_bars_beyond_n():
    """Only the last N prior closes are checked — older bars can violate the rule."""
    cfg2 = hc.HammerContextConfig(
        lookback_candles=2, enable_sell=False, enable_risk_limit=False,
    )
    cfg3 = hc.HammerContextConfig(
        lookback_candles=3, enable_sell=False, enable_risk_limit=False,
    )
    signal = _classic_green("s")  # low = 90
    candles = [
        _pad("too_old", 80.0),   # below hammer low — only matters if lookback >= 3
        _pad("p1", 95.0),
        _pad("p2", 94.0),
        signal,
        Candle("n", 102.0, 103.0, 101.0, 102.5),
    ]
    assert hc.detect_buy_setup(candles, 3, cfg2) is None
    fail = hc.detect_buy_setup(candles, 3, cfg3)
    assert fail is not None
    assert "below hammer low" in fail


def test_body_only_rejects_doji_prev_and_wrong_prev_color():
    ratios = HammerRatioConfig(body_pct=10.0, body_tol=25.0)
    buy_cfg = hc.HammerContextConfig(
        buy_hammer_ratios=ratios, buy_require_wick=False,
        enable_sell=False, lookback_candles=1, enable_risk_limit=False,
    )
    # Signal may be red with small body — still BUY if previous is red.
    signal_red = Candle("s", 100.5, 101.0, 99.5, 100.0)  # body 0.5 / range 1.5 ≈ 33% — too fat
    # Use body ≤ 10%: range 10, body 0.5 → 5%
    signal_red = Candle("s", 100.5, 105.0, 95.0, 100.0)
    good = [_pad_red("p0", 100.0), signal_red, Candle("n", 100.5, 101.0, 100.0, 100.8)]
    assert hc.detect_buy_setup(good, 1, buy_cfg) is None

    doji_prev = Candle("p0", 100.0, 101.0, 99.0, 100.0)
    bars = [doji_prev, signal_red, Candle("n", 100.5, 101.0, 100.0, 100.8)]
    fail = hc.detect_buy_setup(bars, 1, buy_cfg) or ""
    assert "previous" in fail.lower() or "doji" in fail.lower() or "color" in fail.lower()

    green_prev = [_pad("p0", 100.0), signal_red, Candle("n", 100.5, 101.0, 100.0, 100.8)]
    assert hc.detect_buy_setup(green_prev, 1, buy_cfg) is not None


def test_extreme_lower_wick_fails_hammer_but_passes_body_only():
    """Gold-style 80%+ lower wick is outside default 60±18 dominant band, but wick-off allows it."""
    ratios = HammerRatioConfig(body_pct=10.0, body_tol=25.0)
    # Green, body ~6.4%, lower wick ~82% (same proportions as a real 1h XAUUSD bar).
    signal = Candle("s", 100.00, 100.18, 99.00, 100.06)
    candles = [_pad_red("p0", 100.0), signal, Candle("n", 100.06, 100.20, 100.00, 100.10)]

    wick_on = hc.HammerContextConfig(
        buy_hammer_ratios=ratios, buy_require_wick=True,
        enable_sell=False, lookback_candles=1, enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    wick_off = hc.HammerContextConfig(
        buy_hammer_ratios=ratios, buy_require_wick=False,
        enable_sell=False, lookback_candles=1, enable_risk_limit=False,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
    )
    assert hc.detect_buy_setup(candles, 1, wick_on) is not None
    assert hc.detect_buy_setup(candles, 1, wick_off) is None
    taken = [s for s in hc.run_strategy(candles, "3m", wick_off) if not s.ignored]
    assert taken and taken[0].pattern_variant == "BODY_ONLY"


def test_body_only_zero_pct_cap_is_not_replaced_with_ten():
    ratios = HammerRatioConfig(body_pct=0.0, body_tol=25.0)
    assert hc.body_only_max_pct(ratios) == 0.0
    fat = Candle("s", 100.0, 101.0, 99.0, 100.2)  # body > 0
    assert hc.body_only_ok(fat, ratios)[0] is False


def test_backtest_ledger_keeps_body_only_variant():
    from backtest import ledger_to_polars
    df = ledger_to_polars([])
    assert "pattern_variant" in df.columns


def test_pullback_entry_buy_and_sell():
    buy_e, dist = hc.pullback_entry_price(TradeDirection.BUY, 4010.0, 4000.0, 35.0)
    assert dist == 10.0
    assert abs(buy_e - 4006.5) < 1e-9
    sell_e, dist2 = hc.pullback_entry_price(TradeDirection.SELL, 4000.0, 4010.0, 35.0)
    assert dist2 == 10.0
    assert abs(sell_e - 4003.5) < 1e-9
    same, _ = hc.pullback_entry_price(TradeDirection.BUY, 4010.0, 4000.0, 0.0)
    assert same == 4010.0


def test_signal_applies_pullback_and_awaits_limit():
    import logic
    cfg = hc.HammerContextConfig(
        lookback_candles=1,
        enable_sell=False,
        entry_pullback_pct=35.0,
        buffer_mode=logic.BufferMode.NONE,
        sl_buffer_pct=0.0,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    # classic green: O=100 C=102 H=103.5 L=90 → next open 102 as entry base
    signal = _classic_green("s")
    nxt = Candle("n", 102.0, 103.0, 100.0, 102.5)
    candles = [_pad("p0", 95.0), signal, nxt]
    assert hc.detect_buy_setup(candles, 1, cfg) is None
    sig = hc.build_context_signal(TradeDirection.BUY, signal, nxt, "3m", cfg)
    assert sig is not None and not sig.ignored
    assert sig.await_limit_fill is True
    # base entry 102, SL at low 90 (no buffer), dist 12, 35% → fill 97.8
    # TP from signal entry: 102 + 2*12 = 126 (pullback must not move target)
    assert abs(sig.entry_price - 97.8) < 1e-9
    assert abs(sig.stop_loss - 90.0) < 1e-9
    assert abs(sig.target - 126.0) < 1e-9
    assert abs(sig.risk - (97.8 - 90.0)) < 1e-9


def test_pullback_keeps_signal_rr_target():
    """User rule: entry 4010, SL 4000, RR 2 → TP 4030 even after 35% pullback fill."""
    import logic
    cfg = hc.HammerContextConfig(
        lookback_candles=1,
        enable_sell=False,
        entry_pullback_pct=35.0,
        buffer_mode=logic.BufferMode.NONE,
        sl_buffer_pct=0.0,
        timeframe_settings={"3m": TimeframeSetting(2.0, 500.0)},
        enable_risk_limit=False,
    )
    # Craft signal low=4000, next open=4010
    signal = Candle("s", 4005.0, 4012.0, 4000.0, 4008.0)
    nxt = Candle("n", 4010.0, 4015.0, 4007.0, 4012.0)
    candles = [_pad("p0", 4006.0), signal, nxt]
    # May fail classic hammer shape — force via build_context_signal directly
    sig = hc.build_context_signal(TradeDirection.BUY, signal, nxt, "3m", cfg)
    assert sig is not None and not sig.ignored
    assert abs(sig.stop_loss - 4000.0) < 1e-9
    assert abs(sig.entry_price - 4006.5) < 1e-9  # 4010 - 0.35*10
    assert abs(sig.target - 4030.0) < 1e-9


def test_find_limit_fill_index_buy():
    import numpy as np
    highs = np.array([103.0, 102.0, 101.0])
    lows = np.array([101.0, 100.0, 97.0])
    opens = np.array([102.0, 101.5, 100.5])
    idx = hc.find_limit_fill_index(
        TradeDirection.BUY, 100.0, 90.0, highs, lows, opens, 0, 10,
    )
    assert idx == 1  # low 100 touches limit


if __name__ == "__main__":
    test_buy_requires_prior_closes_not_below_hammer_low()
    test_sell_requires_prior_closes_not_above_hammer_high()
    test_lookback_count_configurable()
    test_lookback_zero_skips_prior_close_check()
    test_separate_entry_rules()
    test_asymmetric_buy_body_bounds()
    test_body_only_ignores_wick_shape()
    test_body_only_sell_red_ignores_lower_wick()
    test_body_only_live_pipeline_matches_backtest()
    test_lookback_ignores_bars_beyond_n()
    test_body_only_rejects_doji_prev_and_wrong_prev_color()
    test_extreme_lower_wick_fails_hammer_but_passes_body_only()
    test_body_only_zero_pct_cap_is_not_replaced_with_ten()
    test_backtest_ledger_keeps_body_only_variant()
    test_pullback_entry_buy_and_sell()
    test_signal_applies_pullback_and_awaits_limit()
    test_pullback_keeps_signal_rr_target()
    test_find_limit_fill_index_buy()
    print("ok  hammer context tests passed")

