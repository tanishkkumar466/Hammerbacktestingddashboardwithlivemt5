"""Direction-tab matrix: Classic/Inverted × green/red → BUY / SELL / NO."""

from logic import (
    Candle,
    CandleColor,
    HammerVariant,
    StrategyConfig,
    TradeAction,
    TradeDirection,
    check_hammer,
    hammer_trade_action,
    run_hammer_direction_self_test,
)


def test_default_matrix():
    cfg = StrategyConfig()
    assert cfg.classic_green == TradeAction.BUY
    assert cfg.classic_red == TradeAction.SELL
    assert cfg.inverted_green == TradeAction.BUY
    assert cfg.inverted_red == TradeAction.SELL
    lines = run_hammer_direction_self_test(cfg)
    assert not any("FAIL" in ln for ln in lines), lines
    assert any("Classic GREEN: OK" in ln for ln in lines), lines
    assert any("Classic RED: OK" in ln for ln in lines), lines
    assert any("Inverted GREEN: OK" in ln for ln in lines), lines
    assert any("Inverted RED: OK" in ln for ln in lines), lines


def test_classic_green_no_skips_trade():
    cfg = StrategyConfig(classic_green=TradeAction.NO)
    bar = Candle("c-g", 100.0, 103.5, 90.0, 102.0)
    hr = check_hammer(bar, cfg)
    assert hr.direction is None
    assert hammer_trade_action(cfg, HammerVariant.CLASSIC, CandleColor.GREEN) == TradeAction.NO


def test_inverted_red_can_be_buy():
    cfg = StrategyConfig(inverted_red=TradeAction.BUY)
    bar = Candle("i-r", 102.0, 108.0, 99.6, 100.0)
    hr = check_hammer(bar, cfg)
    assert hr.hammer_variant == HammerVariant.INVERTED
    assert hr.direction == TradeDirection.BUY


def test_both_classic_no_disables_classic():
    cfg = StrategyConfig(classic_green=TradeAction.NO, classic_red=TradeAction.NO)
    assert cfg.enable_classic_hammer is False
    assert cfg.enable_inverted_hammer is True


def test_classic_and_inverted_use_separate_entry_rules():
    from logic import EntryRule, calculate_entry_price

    cfg = StrategyConfig(
        entry_rule=EntryRule.HAMMER_HIGH,
        inverted_entry_rule=EntryRule.HAMMER_LOW,
    )
    classic = Candle("c", 100.0, 105.0, 90.0, 101.0)
    inverted = Candle("i", 100.0, 110.0, 99.0, 101.0)
    nxt = Candle("n", 102.0, 103.0, 101.0, 102.5)
    assert calculate_entry_price(classic, nxt, cfg, HammerVariant.CLASSIC) == 105.0
    assert calculate_entry_price(inverted, nxt, cfg, HammerVariant.INVERTED) == 99.0


def test_inverted_stop_uses_inverted_buffer():
    from logic import BufferMode, calculate_stop_loss

    cfg = StrategyConfig(
        buffer_mode=BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=1.0,
        inverted_buffer_mode=BufferMode.FLAT_AMOUNT,
        inverted_sl_buffer_flat=7.0,
    )
    inverted = Candle("i", 102.0, 108.0, 99.6, 100.0)
    sl = calculate_stop_loss(inverted, TradeDirection.BUY, cfg, HammerVariant.INVERTED)
    assert abs(sl - (99.6 - 7.0)) < 1e-9
    classic = Candle("c", 100.0, 100.5, 90.0, 100.4)
    sl_c = calculate_stop_loss(classic, TradeDirection.BUY, cfg, HammerVariant.CLASSIC)
    assert abs(sl_c - (90.0 - 1.0)) < 1e-9


def test_verify_accepts_enum_or_string_variant():
    from logic import TradeSignal, verify_hammer_trade_signal

    cfg = StrategyConfig()
    hammer = Candle("c-g", 100.0, 103.5, 90.0, 102.0)
    nxt = Candle("n", 100.4, 101.0, 100.0, 100.8)
    sig = TradeSignal(
        direction=TradeDirection.BUY,
        hammer_candle=hammer,
        entry_candle=nxt,
        entry_price=100.4,
        stop_loss=89.0,
        risk=11.4,
        rr_multiple=2.0,
        target=123.2,
        timeframe="3m",
        pattern_variant=HammerVariant.CLASSIC,
    )
    ok, msg = verify_hammer_trade_signal(sig, cfg)
    assert ok, msg
    sig.pattern_variant = "CLASSIC"
    ok, msg = verify_hammer_trade_signal(sig, cfg)
    assert ok, msg


def test_live_find_bar_uses_same_matrix_and_entry():
    from logic import EntryRule, run_strategy, TimeframeSetting
    from live import find_bar_signal_outcome
    from indicators.config import IndicatorStackConfig

    cfg = StrategyConfig(
        classic_green=TradeAction.NO,
        inverted_red=TradeAction.BUY,
        inverted_entry_rule=EntryRule.HAMMER_LOW,
        inverted_entry_offset=0.0,
        timeframe_settings={"3m": TimeframeSetting(rr_multiple=2.0, max_sl_usd=500.0)},
        enable_risk_limit=False,
    )
    classic_green = Candle("c-g", 100.0, 100.5, 90.0, 100.4)
    inverted_red = Candle("i-r", 102.0, 108.0, 99.6, 100.0)
    forming = Candle("n", 100.2, 101.0, 99.8, 100.5)
    pad = [Candle("p", 100.0, 101.0, 99.0, 100.5), Candle("p2", 100.5, 101.0, 100.0, 100.6)]

    signals = run_strategy(pad + [classic_green, forming], timeframe="3m", config=cfg)
    taken = [s for s in signals if not s.ignored]
    assert taken == []

    closed = pad + [inverted_red]
    sig, ignored = find_bar_signal_outcome(
        closed, forming, "3m", "hammer", cfg, IndicatorStackConfig(),
    )
    assert ignored is None
    assert sig is not None
    assert sig.direction == TradeDirection.BUY
    assert sig.pattern_variant == "INVERTED"
    assert abs(sig.entry_price - 99.6) < 1e-9


if __name__ == "__main__":
    test_default_matrix()
    test_classic_green_no_skips_trade()
    test_inverted_red_can_be_buy()
    test_both_classic_no_disables_classic()
    test_classic_and_inverted_use_separate_entry_rules()
    test_inverted_stop_uses_inverted_buffer()
    test_verify_accepts_enum_or_string_variant()
    test_live_find_bar_uses_same_matrix_and_entry()
    print("ok  direction matrix tests passed")
