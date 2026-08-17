"""Hammer with candles: N prior closes vs hammer low/high."""

from logic import Candle, EntryRule, HammerRatioConfig, TimeframeSetting, TradeDirection
import hammer_context_logic as hc


def _classic_green(ts="sig"):
    return Candle(ts, 100.0, 103.5, 90.0, 102.0)


def _inverted_red(ts="sig"):
    return Candle(ts, 102.0, 108.0, 99.6, 100.0)


def _pad(ts, close):
    return Candle(ts, close, close + 0.5, close - 0.5, close)


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


if __name__ == "__main__":
    test_buy_requires_prior_closes_not_below_hammer_low()
    test_sell_requires_prior_closes_not_above_hammer_high()
    test_lookback_count_configurable()
    test_separate_entry_rules()
    test_asymmetric_buy_body_bounds()
    print("ok  hammer context tests passed")
