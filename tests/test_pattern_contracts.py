"""
Pattern contracts — the same checks run automatically for EVERY pattern in the
dashboard's PATTERN_REGISTRY (Hammer, Doji, HWC, HWC 35%, and any pattern added later).

Adding a new pattern?  Add it to PATTERN_REGISTRY *and* to CONTRACTS below.
`test_every_registered_pattern_has_a_contract` fails until you do, so a new pattern
can never ship without these checks:

  * backtest and Live both call the pattern's OWN engine (no silent fallback)
  * Live finds exactly the same signals as the backtest, bar by bar — also with a
    session filter and an indicator filter on
  * every signal's Entry / SL / TP obey the maths (RR, side of the stop, pullback)
  * Entry Offset moves the entry by exactly the configured row value, SL unchanged
  * Live history explains each signal with the row that built it, and that row's
    settings rebuild the signal's exact SL (candle extreme + buffer, or fixed distance)
  * backtest trades: WIN exits at TP, LOSS at SL (or a worse gap), P&L = size × move,
    and no two trades overlap when overlap is off
  * API accounts load the pattern from a preset; Telegram has a short name for it

Candles are synthetic but deterministic (seeded), so the tests need no data files
and give the same result on every machine and in CI.
"""

from __future__ import annotations

import copy
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import polars as pl
import pytest

import backtest as bt
import dashboard
import doji_logic
import hammer_context_logic as hc
import live
import live_history
import logic
from API.runtime import strategy_from_preset
from backtest import BacktestConfig, ExitModel, PositionSizingMode, TradeOutcome
from indicators.config import IndicatorStackConfig, RSIConfig
from notification.telegram import short_strategy_name

RR = 2.0
TF_FOLDER, TF_LABEL = "1hour", "1h"
N_BARS = 320
BUY, SELL = logic.TradeDirection.BUY, logic.TradeDirection.SELL


def _tf():
    return {TF_LABEL: logic.TimeframeSetting(rr_multiple=RR, max_sl_usd=1e9)}


def _hammer_cfg(o_buy: float, o_sell: float):
    return logic.StrategyConfig(
        hammer_wick_side=logic.WickSide.EITHER,
        entry_offset=o_buy,
        inverted_entry_offset=o_sell,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=0.3,
        inverted_buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        inverted_sl_buffer_flat=0.4,
        enable_risk_limit=False,
        timeframe_settings=_tf(),
    )


def _doji_cfg(o_buy: float, _o_sell: float):
    return doji_logic.DojiStrategyConfig(
        entry_offset=o_buy,
        buffer_mode=logic.BufferMode.FLAT_AMOUNT,
        sl_buffer_flat=0.3,
        enable_risk_limit=False,
        timeframe_settings=_tf(),
    )


def _hwc_cfg(pullback: float):
    def make(o_buy: float, o_sell: float):
        return hc.HammerContextConfig(
            lookback_candles=2,
            entry_offset=o_buy,
            inverted_entry_offset=o_sell,
            entry_pullback_pct=pullback,
            buffer_mode=logic.BufferMode.FLAT_AMOUNT,
            sl_buffer_flat=0.3,
            inverted_buffer_mode=logic.BufferMode.FLAT_AMOUNT,
            inverted_sl_buffer_flat=0.4,
            enable_risk_limit=False,
            timeframe_settings=_tf(),
        )
    return make


def _row_by_variant(sig, o_buy, o_sell):
    return o_sell if str(sig.pattern_variant or "").upper() == "INVERTED" else o_buy


def _row_by_direction(sig, o_buy, o_sell):
    return o_buy if sig.direction == BUY else o_sell


def _single_row(_sig, o_buy, _o_sell):
    return o_buy


@dataclass(frozen=True)
class Contract:
    pattern_type: str
    engine: str                     # module attribute in backtest.py / live.py that owns run_strategy
    short_name: str                 # Telegram
    make_config: Callable           # (buy/classic row offset, sell/inverted row offset) -> config
    offset_for: Callable            # (signal, o_buy, o_sell) -> offset that built this signal
    pullback_pct: float = 0.0


CONTRACTS: Dict[str, Contract] = {
    "Hammer": Contract("hammer", "logic", "Hammer", _hammer_cfg, _row_by_variant),
    "Doji": Contract("doji", "doji_logic", "Doji", _doji_cfg, _single_row),
    "Hammer with candles": Contract(
        hc.PATTERN_TYPE, "hwc_logic", "HWC", _hwc_cfg(0.0), _row_by_direction,
    ),
    "Hammer with candle 35%": Contract(
        hc.PATTERN_TYPE_35, "hwc35_logic", "HWC 35%", _hwc_cfg(35.0), _row_by_direction, 35.0,
    ),
}
ENGINES = ("logic", "doji_logic", "hwc_logic", "hwc35_logic")
PATTERNS = list(dashboard.PATTERN_REGISTRY)


# ---------------------------------------------------------------- synthetic market

def _synthetic_df(n: int = N_BARS, seed: int = 11) -> pl.DataFrame:
    """Random walk with a mix of hammer, inverted-hammer, doji and trend candles."""
    rng = random.Random(seed)
    t0 = datetime(2024, 1, 2, 0, 0)
    price = 2000.0
    rows = []
    for i in range(n):
        rng_size = rng.uniform(3.0, 12.0)
        kind = rng.random()
        if kind < 0.22:          # classic hammer: long lower wick
            body, upper = rng.uniform(0.08, 0.3), rng.uniform(0.0, 0.15)
        elif kind < 0.44:        # inverted hammer: long upper wick
            body, upper = rng.uniform(0.08, 0.3), None
        elif kind < 0.56:        # doji
            body, upper = rng.uniform(0.0, 0.05), rng.uniform(0.2, 0.75)
        else:                    # ordinary candle
            body, upper = rng.uniform(0.35, 0.9), None
        if upper is None:
            if kind < 0.44:
                lower = rng.uniform(0.0, 0.15)
                upper = 1.0 - body - lower
            else:
                upper = rng.uniform(0.0, 1.0 - body)
        lower = max(0.0, 1.0 - body - upper)
        green = rng.random() < 0.5
        o = price + rng.gauss(0.0, 0.4)
        c = o + body * rng_size if green else o - body * rng_size
        hi = max(o, c) + upper * rng_size
        lo = min(o, c) - lower * rng_size
        rows.append((t0 + timedelta(hours=i), round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
        price = c + rng.gauss(0.0, 1.5)
    return pl.DataFrame({
        "datetime": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
    })


DF = _synthetic_df()
CANDLES = bt.df_to_candles(DF)


def _bt_config(ptype: str, strategy, *, stack=None, sessions=None, overlap=True) -> BacktestConfig:
    return BacktestConfig(
        pattern_type=ptype,
        strategy_config=strategy,
        indicator_stack=stack or IndicatorStackConfig(),
        timeframes_to_test=[TF_FOLDER],
        max_forward_candles=400,
        position_sizing_mode=PositionSizingMode.FIXED_RISK_USD,
        fixed_risk_usd=100.0,
        starting_capital=10000.0,
        allow_overlapping_trades=overlap,
        sessions_enabled=list(sessions or ["Asian", "London", "US"]),
    )


def _snap(sig) -> dict:
    return dict(
        signal_ts=sig.hammer_candle.timestamp,
        entry_ts=sig.entry_candle.timestamp,
        direction=sig.direction,
        entry=float(sig.entry_price),
        sl=float(sig.stop_loss),
        tp=float(sig.target),
        ignored=bool(sig.ignored),
        reason=str(sig.ignore_reason or ""),
        variant=str(getattr(sig, "pattern_variant", "") or ""),
        signal_entry=getattr(sig, "signal_entry_price", None),
        await_fill=bool(getattr(sig, "await_limit_fill", False)),
        sig=sig,
    )


def backtest_signals(ptype: str, strategy, *, stack=None, sessions=None) -> List[dict]:
    """Signals exactly as the backtest sees them after pattern + indicator + session filters."""
    captured: List[dict] = []
    real = bt.sessions.apply_session_and_time_filters

    def capture(signals, **kw):
        out = real(signals, **kw)
        captured.extend(_snap(s) for s in signals)
        return out

    cfg = _bt_config(ptype, strategy, stack=stack, sessions=sessions)
    with patch.object(bt, "load_candles_df", return_value=DF), \
         patch.object(bt.sessions, "apply_session_and_time_filters", side_effect=capture):
        bt.simulate_timeframe_outcomes(TF_FOLDER, cfg)
    return captured


def live_bar(ptype: str, strategy, i: int, *, stack=None, sessions=None):
    return live.find_bar_signal_outcome(
        CANDLES[:i], CANDLES[i], TF_LABEL, ptype, strategy,
        stack or IndicatorStackConfig(),
        sessions_enabled=list(sessions or ["Asian", "London", "US"]),
    )


def _same(a: dict, sig) -> bool:
    b = _snap(sig)
    return (
        a["direction"] == b["direction"]
        and a["ignored"] == b["ignored"]
        and abs(a["entry"] - b["entry"]) < 1e-9
        and abs(a["sl"] - b["sl"]) < 1e-9
        and abs(a["tp"] - b["tp"]) < 1e-9
    )


def _describe(d: Optional[dict]) -> str:
    if d is None:
        return "no signal"
    return (f"{d['direction'].value} entry={d['entry']:.2f} sl={d['sl']:.2f} tp={d['tp']:.2f} "
            f"{'IGNORED: ' + d['reason'] if d['ignored'] else 'actionable'}")


# ---------------------------------------------------------------- registry completeness

def test_every_registered_pattern_has_a_contract():
    missing = [p for p in PATTERNS if p not in CONTRACTS]
    assert not missing, (
        f"New pattern(s) {missing} in dashboard.PATTERN_REGISTRY have no contract. "
        "Add them to CONTRACTS in tests/test_pattern_contracts.py so backtest/Live/API "
        "parity and the signal maths are tested for them."
    )
    stale = [p for p in CONTRACTS if p not in PATTERNS]
    assert not stale, f"CONTRACTS lists patterns that are no longer registered: {stale}"
    for label in PATTERNS:
        assert dashboard.PATTERN_REGISTRY[label]["pattern_type"] == CONTRACTS[label].pattern_type


@pytest.mark.parametrize("label", PATTERNS)
def test_api_preset_loads_the_same_pattern(label):
    got_label, ptype, cfg, _stack = strategy_from_preset({"pattern": label, "fields": {}})
    assert got_label == label
    assert ptype == CONTRACTS[label].pattern_type


def test_telegram_short_names_are_set_and_distinct():
    names = {label: short_strategy_name(label) for label in PATTERNS}
    for label, name in names.items():
        assert name == CONTRACTS[label].short_name
    assert len(set(names.values())) == len(names), f"two patterns share a Telegram name: {names}"


# ---------------------------------------------------------------- dispatch

def _spies(module) -> Tuple[list, list]:
    called: list = []
    patches = []
    for name in ENGINES:
        eng = getattr(module, name)

        def spy(*_a, _n=name, **_k):
            called.append(_n)
            return []
        patches.append(patch.object(eng, "run_strategy", side_effect=spy))
    return called, patches


@pytest.mark.parametrize("label", PATTERNS)
def test_backtest_and_live_call_only_the_patterns_own_engine(label):
    c = CONTRACTS[label]
    cfg = c.make_config(0.0, 0.0)
    for module, run in (
        (bt, lambda: bt.simulate_timeframe_outcomes(TF_FOLDER, _bt_config(c.pattern_type, cfg))),
        (live, lambda: live_bar(c.pattern_type, cfg, 20)),
    ):
        called, patches = _spies(module)
        with patch.object(bt, "load_candles_df", return_value=DF):
            for p in patches:
                p.start()
            try:
                run()
            finally:
                for p in patches:
                    p.stop()
        assert called == [c.engine], f"{module.__name__}: {label} ran {called}, expected [{c.engine!r}]"


# ---------------------------------------------------------------- Live == backtest, bar by bar

SCENARIOS = {
    "plain": dict(),
    "london_session_only": dict(sessions=["London"]),
    "rsi_filter": dict(stack=IndicatorStackConfig(
        rsi=RSIConfig(enabled=True, period=14, buy_above=50.0, sell_below=50.0, apply_trade_filter=True),
    )),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("label", PATTERNS)
def test_live_finds_exactly_the_backtest_signals(label, scenario):
    c = CONTRACTS[label]
    cfg = c.make_config(0.0, 0.0)
    opts = SCENARIOS[scenario]
    by_bar: Dict = {}
    for s in backtest_signals(c.pattern_type, cfg, **opts):
        by_bar.setdefault(s["signal_ts"], []).append(s)

    warmup = max(3, int(getattr(cfg, "lookback_candles", 0) or 0) + 1)
    compared = actionable = filtered = 0
    mismatches = []
    for i in range(warmup, len(CANDLES)):
        signal_bar = CANDLES[i - 1].timestamp
        expected = by_bar.get(signal_bar, [])
        want_act = next((s for s in expected if not s["ignored"]), None)
        want_ign = next((s for s in reversed(expected) if s["ignored"]), None)
        act, ign = live_bar(c.pattern_type, cfg, i, **opts)
        compared += 1
        if want_act is not None:
            actionable += 1
            if act is None or not _same(want_act, act):
                mismatches.append(f"bar {signal_bar}: backtest {_describe(want_act)} | live "
                                  f"{_describe(_snap(act)) if act else _describe(_snap(ign)) if ign else 'none'}")
            continue
        if act is not None:
            mismatches.append(f"bar {signal_bar}: backtest {_describe(want_ign)} | live {_describe(_snap(act))}")
            continue
        if want_ign is not None:
            if scenario != "plain" and ("Session" in want_ign["reason"] or "RSI" in want_ign["reason"]):
                filtered += 1
            if ign is None or not _same(want_ign, ign):
                mismatches.append(f"bar {signal_bar}: backtest {_describe(want_ign)} | live "
                                  f"{_describe(_snap(ign)) if ign else 'none'}")
        elif ign is not None:
            mismatches.append(f"bar {signal_bar}: backtest none | live {_describe(_snap(ign))}")

    assert not mismatches, f"{label}/{scenario}: Live differs from backtest:\n" + "\n".join(mismatches[:10])
    assert actionable >= 5, f"{label}/{scenario}: only {actionable} tradable signals — fixture too weak"
    if scenario != "plain":
        assert filtered >= 1, f"{label}/{scenario}: filter never rejected anything — test would be vacuous"


# ---------------------------------------------------------------- signal maths

@pytest.mark.parametrize("label", PATTERNS)
def test_signal_levels_obey_the_maths(label):
    c = CONTRACTS[label]
    sigs = [s for s in backtest_signals(c.pattern_type, c.make_config(0.0, 0.0)) if not s["ignored"]]
    assert len({s["direction"] for s in sigs}) == 2, f"{label}: fixture must produce BUY and SELL"
    for s in sigs:
        base = s["signal_entry"] if s["await_fill"] else s["entry"]
        dist = abs(base - s["sl"])
        assert dist > 0
        if s["direction"] == BUY:
            assert s["sl"] < s["entry"] <= base + 1e-9 < s["tp"]
        else:
            assert s["sl"] > s["entry"] >= base - 1e-9 > s["tp"]
        assert abs(abs(s["tp"] - base) - RR * dist) < 1e-6, f"{label}: TP is not signal entry ± RR × risk"
        if c.pullback_pct:
            assert s["await_fill"]
            assert abs(abs(base - s["entry"]) - dist * c.pullback_pct / 100.0) < 1e-6
        else:
            assert not s["await_fill"], f"{label}: plain pattern must not wait for a limit fill"


@pytest.mark.parametrize("label", PATTERNS)
def test_entry_offset_moves_entry_by_exactly_the_row_value(label):
    c = CONTRACTS[label]
    o_buy, o_sell = 1.25, -0.75
    base = {(s["signal_ts"], s["direction"]): s
            for s in backtest_signals(c.pattern_type, c.make_config(0.0, 0.0))}
    shifted = backtest_signals(c.pattern_type, c.make_config(o_buy, o_sell))
    matched = {BUY: 0, SELL: 0}
    for s in shifted:
        b = base.get((s["signal_ts"], s["direction"]))
        if b is None:
            continue
        want = c.offset_for(s["sig"], o_buy, o_sell)
        e0 = b["signal_entry"] if b["await_fill"] else b["entry"]
        e1 = s["signal_entry"] if s["await_fill"] else s["entry"]
        assert abs((e1 - e0) - want) < 1e-9, (
            f"{label} {s['direction'].value} ({s['variant']}): entry moved {e1 - e0:+.4f}, "
            f"expected the row offset {want:+g}"
        )
        assert abs(s["sl"] - b["sl"]) < 1e-9, f"{label}: candle-extreme SL must not move with the offset"
        matched[s["direction"]] += 1
    assert matched[BUY] >= 3 and matched[SELL] >= 3, f"{label}: too few matched signals {matched}"


@pytest.mark.parametrize("label", PATTERNS)
def test_live_history_explains_each_signal_with_its_own_row(label):
    c = CONTRACTS[label]
    o_buy, o_sell = 1.25, -0.75
    cfg = c.make_config(o_buy, o_sell)
    sigs = backtest_signals(c.pattern_type, cfg)
    assert sigs
    for s in sigs:
        p = live_history.side_params(cfg, c.pattern_type, s["sig"])
        assert p is not None
        assert abs(p.entry_offset - c.offset_for(s["sig"], o_buy, o_sell)) < 1e-12, (
            f"{label} {s['direction'].value}: history shows offset {p.entry_offset}"
        )


FIXED_SL = 6.0


def _sell_row_fixed_stop(cfg):
    """SELL / inverted row uses a fixed stop, BUY / classic row keeps the candle extreme."""
    cfg = copy.deepcopy(cfg)
    if hasattr(cfg, "inverted_sl_mode"):
        cfg.inverted_sl_mode = logic.StopLossMode.FIXED_FROM_ENTRY
        cfg.inverted_sl_fixed_distance = FIXED_SL
    else:
        cfg.sl_mode = logic.StopLossMode.FIXED_FROM_ENTRY
        cfg.sl_fixed_distance = FIXED_SL
    return cfg


STOP_SETUPS = {
    "candle_extreme": lambda cfg: cfg,
    "sell_row_fixed": _sell_row_fixed_stop,
}


@pytest.mark.parametrize("setup", list(STOP_SETUPS))
@pytest.mark.parametrize("label", PATTERNS)
def test_stop_loss_is_rebuilt_from_the_history_row(label, setup):
    """
    SL = signal candle LOW (BUY) / HIGH (SELL) ∓ buffer, or signal entry ∓ fixed distance,
    using exactly the settings row Live history shows for that signal.
    """
    c = CONTRACTS[label]
    cfg = STOP_SETUPS[setup](c.make_config(0.0, 0.0))
    sigs = [s for s in backtest_signals(c.pattern_type, cfg) if not s["ignored"]]
    seen = {BUY: 0, SELL: 0}
    fixed_used = 0
    for s in sigs:
        sig = s["sig"]
        p = live_history.side_params(cfg, c.pattern_type, sig)
        is_buy = s["direction"] == BUY
        if p.sl_mode == logic.StopLossMode.FIXED_FROM_ENTRY:
            fixed_used += 1
            base = s["signal_entry"] if s["await_fill"] else s["entry"]
            want = base - p.sl_fixed_distance if is_buy else base + p.sl_fixed_distance
        else:
            assert p.buffer_mode == logic.BufferMode.FLAT_AMOUNT
            candle = sig.hammer_candle
            want = candle.low - p.sl_buffer_flat if is_buy else candle.high + p.sl_buffer_flat
        assert abs(s["sl"] - want) < 1e-9, (
            f"{label}/{setup} {s['direction'].value} ({s['variant'] or 'n/a'}) at {s['signal_ts']}: "
            f"SL {s['sl']:.4f}, history row gives {want:.4f} ({p.sl_mode.value})"
        )
        seen[s["direction"]] += 1
    assert seen[BUY] >= 3 and seen[SELL] >= 3, f"{label}/{setup}: too few signals {seen}"
    if setup == "sell_row_fixed":
        assert fixed_used >= 3, f"{label}: fixed stop never exercised"


# ---------------------------------------------------------------- backtest trade accounting

@pytest.mark.parametrize("label", PATTERNS)
def test_backtest_trades_exit_at_their_levels_and_pnl_adds_up(label):
    c = CONTRACTS[label]
    cfg = _bt_config(c.pattern_type, c.make_config(0.0, 0.0), overlap=False)
    with patch.object(bt, "load_candles_df", return_value=DF):
        ledger, _ignored = bt.run_full_backtest(cfg)
    counts = {"WIN": 0, "LOSS": 0, "SKIPPED_OVERLAP": 0}
    for model in bt.ALL_EXIT_MODELS:
        last_exit = None
        for t in sorted(ledger, key=lambda t: t.entry_time):
            outcome, exit_px, exit_time, _bars = t.outcomes[model]
            sig = t.signal
            size, risk_usd, pnl = t.position_size[model], t.risk_usd[model], t.pnl[model]
            is_buy = sig.direction == BUY
            if outcome == TradeOutcome.SKIPPED_OVERLAP:
                assert size == 0 and pnl == 0
                counts["SKIPPED_OVERLAP"] += 1
                continue
            # Overlap off: a taken trade never starts before the previous one closed
            if last_exit is not None:
                assert t.entry_time >= last_exit, f"{label}/{model.value}: overlapping trades"
            if outcome == TradeOutcome.STILL_OPEN:
                last_exit = DF["datetime"][-1]
                continue
            last_exit = exit_time
            counts[outcome.value] += 1
            assert abs(risk_usd - 100.0) < 1e-9
            assert abs(size - 100.0 / sig.risk) < 1e-9
            move = (exit_px - sig.entry_price) if is_buy else (sig.entry_price - exit_px)
            assert abs(pnl - size * move) < 1e-6, f"{label}: P&L is not size × price move"
            if outcome == TradeOutcome.WIN:
                assert abs(exit_px - sig.target) < 1e-9, f"{label}: WIN must exit at TP"
                assert pnl > 0
            else:
                worse_or_at_sl = exit_px <= sig.stop_loss + 1e-9 if is_buy else exit_px >= sig.stop_loss - 1e-9
                assert worse_or_at_sl, f"{label}: LOSS must exit at SL or a worse gap"
                assert pnl < 0
    assert counts["WIN"] >= 3 and counts["LOSS"] >= 3, f"{label}: fixture too weak {counts}"
    assert counts["SKIPPED_OVERLAP"] >= 1, f"{label}: overlap filter never exercised {counts}"
