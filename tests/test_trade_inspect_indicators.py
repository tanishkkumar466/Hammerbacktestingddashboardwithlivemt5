"""Trade inspect indicator side-panel helpers."""

from dashboard import TradeInspectDialog


def test_fmt_ind_num():
    assert TradeInspectDialog._fmt_ind_num(81290.72) == "81290.72"
    assert TradeInspectDialog._fmt_ind_num(None) == "—"
    assert TradeInspectDialog._fmt_ind_num(float("nan")) == "—"


def test_indicator_panel_includes_signal_and_entry_values():
    class _Cfg:
        enabled = True
        apply_trade_filter = True
        atr_period = 10
        multiplier = 3.0
        period = 20
        buy_above = 60.0
        sell_below = 40.0

    class _Stack:
        supertrend = _Cfg()
        vwap = _Cfg()
        rolling_vwap = _Cfg()
        rsi = _Cfg()

        def enabled_indicator_ids(self):
            return ["supertrend", "vwap", "rolling_vwap", "rsi"]

    dlg = TradeInspectDialog.__new__(TradeInspectDialog)
    html = TradeInspectDialog._build_indicator_panel_html(
        dlg,
        {"direction": "BUY"},
        _Stack(),
        close_sig=100.0,
        close_ent=101.0,
        signal_ts=None,
        entry_ts=None,
        st_sig=99.0,
        st_ent=98.5,
        dir_sig=1,
        dir_ent=1,
        vwap_sig=99.5,
        vwap_ent=100.5,
        rolling_sig=99.2,
        rolling_ent=100.2,
        rsi_sig=62.0,
        rsi_ent=55.0,
    )
    assert "SuperTrend" in html
    assert "VWAP" in html
    assert "Rolling VWAP" in html
    assert "RSI" in html
    assert "Signal" in html and "Entry" in html
    assert "PASS" in html  # ST bullish + RSI 62 > 60 on signal
    assert "62" in html


def test_floating_price_tag_helper_exists():
    assert callable(TradeInspectDialog._draw_floating_price_tags)


def test_pullback_levels_and_summary_show_signal_vs_fill():
    trade = {
        "direction": "BUY",
        "timeframe": "1hour",
        "session": "London",
        "exit_model": "worst_case",
        "outcome": "WIN",
        "entry_time": "2025-01-02 09:00:00",
        "entry_price": 4006.5,
        "signal_entry_price": 4010.0,
        "await_limit_fill": True,
        "entry_pullback_pct": 35.0,
        "stop_loss": 4000.0,
        "target": 4030.0,
        "exit_time": "2025-01-02 11:00:00",
        "exit_price": 4030.0,
        "pnl_usd": 100.0,
        "bars_held": 2,
        "pattern_variant": "CLASSIC",
    }
    signal_e, fill_e, sl_e, tp_e, pct, is_pb = TradeInspectDialog._pullback_levels(trade)
    assert is_pb
    assert abs(signal_e - 4010.0) < 1e-9
    assert abs(fill_e - 4006.5) < 1e-9
    assert abs(sl_e - 4000.0) < 1e-9
    assert abs(tp_e - 4030.0) < 1e-9
    assert abs(pct - 35.0) < 1e-9

    html = TradeInspectDialog._summary_html(trade)
    assert "4,010.00" in html
    assert "4,006.50" in html
    assert "pullback" in html.lower()
    assert "signal entry" in html.lower()
    assert "4,000.00" in html and "4,030.00" in html
