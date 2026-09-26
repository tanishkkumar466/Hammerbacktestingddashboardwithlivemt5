"""Optional min-wick filter for Hammer with candles and Hammer with candle 35%."""

from logic import Candle, HammerRatioConfig, TimeframeSetting, TradeDirection
import hammer_context_logic as hc
from strategy import hammer_with_candles_35_logic as hwc35
from strategy import hammer_with_candles_logic as hwc

TF = {"3m": TimeframeSetting(2.0, 500.0)}


def _pad(ts, close):
    return Candle(ts, close - 0.3, close + 0.5, close - 0.5, close)


def _pad_red(ts, close):
    return Candle(ts, close + 0.4, close + 0.5, close - 0.5, close)


# Classic green: range 13.5, lower wick 10 → 74.07%
BUY_BARS = [_pad("p0", 92.0), Candle("s", 100.0, 103.5, 90.0, 102.0), Candle("n", 102.0, 103.0, 101.0, 102.5)]
# Inverted red: range 8.4, upper wick 6 → 71.43%
SELL_BARS = [_pad("p0", 107.0), Candle("s", 102.0, 108.0, 99.6, 100.0), Candle("n", 100.0, 101.0, 99.0, 100.5)]


def _cfg(**kw):
    base = dict(lookback_candles=1, enable_risk_limit=False, timeframe_settings=TF)
    base.update(kw)
    return hc.HammerContextConfig(**base)


def test_filter_off_by_default_changes_nothing():
    cfg = _cfg()
    assert not cfg.buy_min_lower_wick_enabled and not cfg.sell_min_upper_wick_enabled
    assert cfg.buy_min_lower_wick_pct == 35.0 and cfg.sell_min_upper_wick_pct == 35.0
    assert hc.detect_buy_setup(BUY_BARS, 1, cfg) is None
    assert hc.detect_sell_setup(SELL_BARS, 1, cfg) is None


def test_buy_lower_wick_threshold():
    assert hc.detect_buy_setup(BUY_BARS, 1, _cfg(buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=74.0)) is None
    fail = hc.detect_buy_setup(BUY_BARS, 1, _cfg(buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=75.0))
    assert fail == "Lower wick 74.1% below min 75%"
    # Disabled switch ignores the % even when it would fail
    assert hc.detect_buy_setup(BUY_BARS, 1, _cfg(buy_min_lower_wick_pct=99.0)) is None


def test_sell_upper_wick_threshold():
    assert hc.detect_sell_setup(SELL_BARS, 1, _cfg(sell_min_upper_wick_enabled=True, sell_min_upper_wick_pct=71.0)) is None
    fail = hc.detect_sell_setup(SELL_BARS, 1, _cfg(sell_min_upper_wick_enabled=True, sell_min_upper_wick_pct=72.0))
    assert fail == "Upper wick 71.4% below min 72%"
    # BUY filter does not touch SELL
    assert hc.detect_sell_setup(SELL_BARS, 1, _cfg(buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=99.0)) is None


def test_filter_applies_in_wick_off_body_only_mode():
    ratios = HammerRatioConfig(body_pct=10.0, body_tol=0.0)
    # Small body, lower wick 0.5 of range 10.5 → 4.8%
    bars = [_pad_red("p0", 100.0), Candle("s", 100.0, 110.0, 99.5, 101.0), Candle("n", 101.0, 102.0, 100.5, 101.5)]
    off = _cfg(buy_hammer_ratios=ratios, buy_require_wick=False, enable_sell=False)
    assert hc.detect_buy_setup(bars, 1, off) is None
    on = _cfg(buy_hammer_ratios=ratios, buy_require_wick=False, enable_sell=False,
              buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=35.0)
    assert "Lower wick" in hc.detect_buy_setup(bars, 1, on)


def test_both_products_respect_filter_and_35_pullback_unchanged():
    strict = dict(buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=80.0)
    for product in (hwc, hwc35):
        taken = [s for s in product.run_strategy(BUY_BARS, "3m", _cfg()) if not s.ignored]
        assert len(taken) == 1 and taken[0].direction == TradeDirection.BUY
        assert product.run_strategy(BUY_BARS, "3m", _cfg(**strict)) == []

    loose = _cfg(buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=40.0, entry_pullback_pct=35.0)
    plain = hwc.run_strategy(BUY_BARS, "3m", loose)[0]
    pulled = hwc35.run_strategy(BUY_BARS, "3m", loose)[0]
    assert not plain.await_limit_fill and plain.entry_pullback_pct is None
    assert pulled.await_limit_fill and pulled.entry_pullback_pct == 35.0


def test_pct_is_clamped_and_bad_input_falls_back():
    cfg = _cfg(buy_min_lower_wick_pct="abc", sell_min_upper_wick_pct=150)
    assert cfg.buy_min_lower_wick_pct == 35.0
    assert cfg.sell_min_upper_wick_pct == 100.0


def test_rules_text_mentions_filter_only_when_on():
    assert "wick ≥" not in hc.describe_hammer_context_rules(_cfg())
    text = hc.describe_hammer_context_rules(_cfg(
        buy_min_lower_wick_enabled=True, buy_min_lower_wick_pct=40,
        sell_min_upper_wick_enabled=True, sell_min_upper_wick_pct=35,
    ))
    assert "lower wick ≥ 40% of range" in text
    assert "upper wick ≥ 35% of range" in text


def test_api_preset_reads_filter_and_old_presets_stay_off():
    from API.runtime import build_hammer_context_strategy

    old = build_hammer_context_strategy({"pattern": "Hammer with candles", "fields": {}})
    assert not old.buy_min_lower_wick_enabled and not old.sell_min_upper_wick_enabled

    new = build_hammer_context_strategy({
        "pattern": "Hammer with candle 35%",
        "fields": {
            "buy_min_lower_wick_enabled": True, "buy_min_lower_wick_pct": "40",
            "sell_min_upper_wick_enabled": True, "sell_min_upper_wick_pct": 38,
        },
    })
    assert new.buy_min_lower_wick_enabled and new.buy_min_lower_wick_pct == 40.0
    assert new.sell_min_upper_wick_enabled and new.sell_min_upper_wick_pct == 38.0
