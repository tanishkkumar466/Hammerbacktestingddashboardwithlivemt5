"""
strategy/
========
All pattern strategy engines live here (one module per pattern).

  logic.py                         — Classic / inverted Hammer
  doji_logic.py                    — Doji
  hammer_with_candles_logic.py     — Hammer with candles (market entry)
  hammer_with_candles_35_logic.py  — Hammer with candle 35% (pullback entry)
  hammer_context_core.py           — shared detection for the HWC family
  hammer_context_logic.py          — compatibility façade (old imports)

Root-level ``logic``, ``doji_logic``, and ``hammer_context_logic`` are thin
compatibility shims so existing imports keep working unchanged.

New patterns: add a new module in this package (see .cursor/rules).
"""

__all__ = [
    "logic",
    "doji_logic",
    "hammer_with_candles_logic",
    "hammer_with_candles_35_logic",
    "hammer_context_core",
    "hammer_context_logic",
]
