"""
API desk vs dashboard: the same preset must build the same strategy + indicators.

Live slots build from the Parameters panel; API accounts parse the preset JSON in
API/runtime.py. Any drift means API trades differ from Live / backtest.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import random
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit, QMessageBox  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SKIP_RANDOM = {
    "starting_capital", "fixed_risk_usd", "position_size", "risk_pct_of_equity",
    "max_forward_candles", "slippage_usd", "commission_per_trade", "market_data",
}


def _norm(v):
    if isinstance(v, enum.Enum):
        return v.value
    if dataclasses.is_dataclass(v):
        return {f.name: _norm(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float):
        return round(v, 9)
    return v


def _diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")
        return out
    return [] if a == b else [f"{path}: dashboard={a!r} api={b!r}"]


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    import dashboard

    tmp = tmp_path_factory.mktemp("parity")
    ini = str(tmp / "settings.ini")
    mp = pytest.MonkeyPatch()
    mp.setattr(dashboard, "QSettings", lambda *a, **k: QSettings(ini, QSettings.IniFormat))
    mp.setattr(dashboard, "DEFAULT_OUTPUT_DIR", str(tmp / "output"))
    mp.setattr(dashboard.BacktestDashboard, "_restore_session_state", lambda self: None)
    mp.setattr(dashboard.BacktestDashboard, "_save_session_state", lambda self, force=False: None)
    for name in ("information", "warning", "critical", "question"):
        mp.setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
    app = QApplication.instance() or QApplication([])
    w = dashboard.BacktestDashboard()
    app.processEvents()
    yield w
    w.deleteLater()
    app.processEvents()
    mp.undo()


def _compare(w):
    from API.runtime import strategy_from_preset

    snap = json.loads(json.dumps(w._collect_preset_snapshot(), default=str))
    ui_cfg, ui_stack = w._build_strategy_config(), w._build_indicator_stack()
    _l, _t, api_cfg, api_stack = strategy_from_preset(snap)
    return (
        _diff(_norm(ui_cfg), _norm(api_cfg), "strategy")
        + _diff(_norm(ui_stack), _norm(api_stack), "indicators")
    ), _norm(ui_cfg)


@pytest.mark.parametrize("preset", sorted(p.name for p in (ROOT / "template").glob("preset_*.json")))
def test_shipped_presets_match(window, preset):
    data = json.loads((ROOT / "template" / preset).read_text(encoding="utf-8"))
    window._apply_preset_snapshot(data)
    diffs, _ = _compare(window)
    assert not diffs, "\n".join(diffs)


@pytest.mark.parametrize("pattern", ["Hammer", "Doji", "Hammer with candles", "Hammer with candle 35%"])
def test_randomized_parameters_match(window, pattern):
    rng = random.Random(f"parity-{pattern}")
    w = window
    w.pattern_combo.setCurrentText(pattern)
    assert w.pattern_combo.currentText() == pattern
    _, baseline = _compare(w)
    changed_fields = set()
    for trial in range(8):
        for name, wid in w.field_widgets.items():
            if name in SKIP_RANDOM:
                continue
            if isinstance(wid, QCheckBox):
                wid.setChecked(rng.random() < 0.5)
            elif isinstance(wid, QComboBox) and wid.count() > 1:
                wid.setCurrentIndex(rng.randrange(wid.count()))
            elif isinstance(wid, QLineEdit) and wid.isEnabled():
                try:
                    v = float(wid.text())
                except ValueError:
                    continue
                nv = round(rng.uniform(0, max(1.0, v * 2 if v else 50)), 2)
                wid.setText(str(int(nv) if "candles" in name else nv))
        w.pattern_combo.setCurrentText(pattern)
        diffs, built = _compare(w)
        assert not diffs, f"trial {trial}:\n" + "\n".join(diffs)
        changed_fields |= {d.split(":")[0] for d in _diff(baseline, built)}
    # Guard against a vacuous pass: the fuzz really moved strategy fields.
    assert len(changed_fields) >= 8, changed_fields
