"""Catalog of indicators — extend this dict when adding new ones."""

# Backtest filter behavior is implemented in indicators/filter.py
# (_supertrend_passes, _vwap_passes). Change rules there; keep this
# registry in sync so the dashboard shows the same text to users.

INDICATOR_REGISTRY = {
    "supertrend": {
        "label": "SuperTrend",
        "description": "ATR-based trend line; optional filter after hammer signals are found.",
        "config_key": "supertrend",
        "filter_rules": [
            "Classic hammer + BUY: SuperTrend must be bullish (green) and close must be above the SuperTrend line.",
            "Inverted hammer + SELL: SuperTrend must be bearish (red) and close must be below the SuperTrend line.",
            "Other variant/direction pairs: no SuperTrend filter (trade passes this indicator).",
        ],
        "filter_fn": "_supertrend_passes",
    },
    "vwap": {
        "label": "VWAP",
        "description": "Session VWAP from OHLCV; optional filter by hammer type vs price.",
        "config_key": "vwap",
        "filter_rules": [
            "Classic hammer + BUY: close must be above VWAP; below VWAP → trade rejected.",
            "Classic hammer + SELL: no VWAP block (passes).",
            "Inverted hammer + SELL: close must be below VWAP.",
            "Inverted hammer + BUY: if close is above VWAP → no trade; below/at VWAP passes.",
        ],
        "filter_fn": "_vwap_passes",
    },
}

INDICATOR_FILTER_LOGIC_FILE = "indicators/filter.py"
INDICATOR_COMBINE_HELP = (
    "When two or more indicators are added and “Apply trade filter” is on:\n"
    "• ALL — every enabled filter must pass.\n"
    "• ANY — at least one enabled filter must pass."
)


def list_indicator_ids():
    return list(INDICATOR_REGISTRY.keys())


def format_filter_rules_for_display(ind_id: str) -> str:
    meta = INDICATOR_REGISTRY.get(ind_id, {})
    lines = meta.get("filter_rules") or []
    fn = meta.get("filter_fn", "")
    body = "\n".join(f"• {line}" for line in lines)
    if fn:
        body += f"\n\n(Code: {INDICATOR_FILTER_LOGIC_FILE} → {fn})"
    return body
