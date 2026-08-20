"""
plotting.py
===========
Visualization + Yearly Summary Engine for the Hammer-Candle Backtest.

WHAT THIS FILE DOES
----------------------
Reads the CSV files produced by backtest.py (trade_ledger.csv,
summary_by_year.csv, summary_by_month.csv, summary_by_timeframe.csv,
summary_by_direction.csv, summary_by_session.csv) and produces:

    1. A full set of COLORFUL, LIGHT-THEME charts (never dark mode) --
       equity curves, drawdown charts, win-rate/profit-factor bars,
       monthly heatmaps, R-multiple distributions, a linear regression
       trendline over the equity curve, and more. Every chart is saved
       as a standalone PNG.

    2. A YEARLY SUMMARY TABLE (CSV + printed) with a full battery of
       risk/return ratios per year, per exit model: Sharpe, Sortino,
       Calmar, Treynor (only if a benchmark is supplied -- see note
       below), win rate, profit factor, expectancy, max drawdown, and
       more -- so you can drop it straight into your Excel/Numbers
       record book.

ON THE TREYNOR RATIO (being upfront about this)
----------------------------------------------------
Treynor ratio = (portfolio return - risk-free rate) / beta, where beta
measures the strategy's sensitivity to a MARKET BENCHMARK's returns.
This requires an external benchmark return series (e.g. gold spot index,
or whatever "the market" means for your comparison) that this pipeline
does not otherwise have. Rather than inventing a fake benchmark or
silently outputting a meaningless number, this file:
    - computes Treynor ratio ONLY if you supply a benchmark return
      series (see BENCHMARK_RETURNS_CSV in the config section, or pass
      one directly to compute_yearly_summary())
    - reports "N/A (no benchmark supplied)" in every output otherwise,
      clearly labeled, rather than a silently wrong number.
If you have a gold index/spot price series you want used as the
benchmark, point BENCHMARK_RETURNS_CSV at a CSV with columns
[date, return_pct] (one row per year, or per trade date -- both are
supported, see load_benchmark_returns()) and Treynor will populate
automatically.

THEME
-------
Every chart uses a light background with a vivid, colorful qualitative
palette (never grayscale, never dark mode) -- purely so differences in
strategy quality (win rate, drawdown, profit factor) are easy to read
and compare visually.

HOW TO RUN
------------
    python plotting.py

Everything you'd want to change (which run's output to plot, output
folder, color palette, which exit model to highlight) is in the CONFIG
section below.
"""

import os
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import numpy as np
import polars as pl
import matplotlib
matplotlib.use("Agg")  # no GUI backend needed, just save PNGs
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

import sessions


# ============================================================================
# SECTION 1: CONFIG -- everything you'd want to change is here
# ============================================================================

@dataclass
class PlottingConfig:
    # ---- input: which backtest run to read ----
    backtest_output_dir: str = "output/run_no_overlap"   # matches backtest.py's run_name folder

    # ---- which exit model to highlight in single-model charts ----
    # (worst_case / best_case / candle_bias -- multi-model charts show all 3 regardless)
    primary_exit_model: str = "worst_case"

    # ---- output ----
    output_dir: str = "plots"

    # ---- optional benchmark for Treynor ratio (see module docstring) ----
    # Path to a CSV with columns [date, return_pct]. Leave as None to skip
    # Treynor entirely (reported as N/A everywhere).
    benchmark_returns_csv: Optional[str] = None

    # ---- risk-free rate (annualized %, used in Sharpe/Sortino/Treynor) ----
    risk_free_rate_pct: float = 0.0

    # ---- chart appearance ----
    figure_dpi: int = 150
    figure_width: float = 11.0
    figure_height: float = 6.0


# ---- COLOR PALETTE: vivid, light-background, never dark mode ----
COLORS = {
    "background": "#FFFFFF",
    "grid": "#E8E8E8",
    "text": "#2B2B2B",
    "win": "#2ECC71",        # vivid green
    "loss": "#E74C3C",       # vivid red
    "neutral": "#3498DB",    # vivid blue
    "accent1": "#9B59B6",    # purple
    "accent2": "#F39C12",    # orange
    "accent3": "#1ABC9C",    # teal
    "accent4": "#E91E63",    # pink
    "drawdown": "#E74C3C",
    "equity": "#2980B9",
    "trend": "#F39C12",
}

EXIT_MODEL_COLORS = {
    "worst_case": "#E74C3C",
    "best_case": "#2ECC71",
    "candle_bias": "#9B59B6",
}

QUALITATIVE_PALETTE = ["#3498DB", "#E74C3C", "#2ECC71", "#F39C12", "#9B59B6",
                        "#1ABC9C", "#E91E63", "#34495E", "#F1C40F", "#16A085"]


def _apply_light_theme():
    """
    Forces a clean, light, colorful matplotlib theme -- explicitly NOT
    dark mode, applied fresh before every figure so no residual style
    from a previous plot leaks in.
    """
    plt.rcdefaults()
    plt.rcParams.update({
        "figure.facecolor": COLORS["background"],
        "axes.facecolor": COLORS["background"],
        "savefig.facecolor": COLORS["background"],
        "axes.edgecolor": "#CCCCCC",
        "axes.labelcolor": COLORS["text"],
        "text.color": COLORS["text"],
        "xtick.color": COLORS["text"],
        "ytick.color": COLORS["text"],
        "axes.grid": True,
        "grid.color": COLORS["grid"],
        "grid.linewidth": 0.7,
        "axes.axisbelow": True,
        "font.size": 10,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "figure.titlesize": 15,
        "figure.titleweight": "bold",
    })


# ============================================================================
# SECTION 2: DATA LOADING (reads backtest.py's CSV outputs)
# ============================================================================

def load_trade_ledger(config: PlottingConfig) -> pl.DataFrame:
    path = os.path.join(config.backtest_output_dir, "trade_ledger.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Could not find {path}. Run backtest.py first, and make sure "
            f"PlottingConfig.backtest_output_dir points at the right run folder."
        )
    df = pl.read_csv(path, try_parse_dates=True)
    return df


def load_summary(config: PlottingConfig, name: str) -> pl.DataFrame:
    """name is one of: overall, by_year, by_month, by_timeframe, by_direction"""
    path = os.path.join(config.backtest_output_dir, f"summary_{name}.csv")
    if not os.path.exists(path):
        return pl.DataFrame()
    return pl.read_csv(path, try_parse_dates=True)


def load_benchmark_returns(path: Optional[str]) -> Optional[pl.DataFrame]:
    """
    Loads an optional benchmark return series for Treynor ratio.
    Expects columns [date, return_pct]. Returns None if not provided or
    not found (Treynor is then reported as N/A everywhere, not silently
    computed wrong).
    """
    if path is None or not os.path.exists(path):
        return None
    df = pl.read_csv(path, try_parse_dates=True)
    if "date" not in df.columns or "return_pct" not in df.columns:
        print(f"[WARN] Benchmark CSV {path} must have columns [date, return_pct]. "
              f"Ignoring -- Treynor will show as N/A.")
        return None
    return df


# ============================================================================
# SECTION 3: METRICS HELPERS (ratios not already in backtest.py's summary)
# ============================================================================

def _annualization_factor(entry_times: List) -> float:
    """
    Rough trades-per-year estimate from the actual date range in the
    data, used to annualize Sharpe/Sortino for the yearly summary.
    Falls back to 252 (a standard trading-days assumption) if the date
    range can't be determined.
    """
    if len(entry_times) < 2:
        return 252.0
    span_days = (max(entry_times) - min(entry_times)).days
    if span_days <= 0:
        return 252.0
    trades_per_day = len(entry_times) / span_days
    return trades_per_day * 365.0


def compute_calmar_ratio(total_return_pct: float, max_drawdown_pct: float) -> Optional[float]:
    """Calmar = annualized return / max drawdown. Here using the period
    return directly (not re-annualized) since the yearly breakdown is
    already one year per row."""
    if max_drawdown_pct in (None, 0):
        return None
    return total_return_pct / max_drawdown_pct


def compute_treynor_ratio(
    portfolio_return_pct: float,
    risk_free_rate_pct: float,
    trade_pnls: List[float],
    benchmark_returns: Optional[List[float]],
) -> Optional[float]:
    """
    Treynor = (portfolio return - risk free rate) / beta.
    beta = cov(portfolio returns, benchmark returns) / var(benchmark returns)

    Returns None if no benchmark was supplied (see module docstring for
    why this isn't silently faked with a made-up benchmark).
    """
    if benchmark_returns is None or len(benchmark_returns) < 2 or len(trade_pnls) < 2:
        return None

    n = min(len(trade_pnls), len(benchmark_returns))
    port = np.array(trade_pnls[:n])
    bench = np.array(benchmark_returns[:n])

    if np.std(bench) == 0:
        return None

    cov_matrix = np.cov(port, bench)
    beta = cov_matrix[0, 1] / cov_matrix[1, 1]

    if beta == 0:
        return None

    return (portfolio_return_pct - risk_free_rate_pct) / beta


# ============================================================================
# SECTION 4: YEARLY SUMMARY TABLE (the detailed per-year metrics you asked for)
# ============================================================================

def compute_yearly_summary(
    ledger: pl.DataFrame,
    summary_by_year: pl.DataFrame,
    config: PlottingConfig,
    benchmark_df: Optional[pl.DataFrame] = None,
) -> pl.DataFrame:
    """
    Builds a FULL per-year, per-exit-model metrics table combining:
      - everything already in backtest.py's summary_by_year.csv
        (win rate, profit factor, avg win/loss, max drawdown, etc.)
      - Sharpe / Sortino, RE-COMPUTED here with proper annualization
        (backtest.py's version is per-trade, not annualized)
      - Calmar ratio (return / max drawdown)
      - Treynor ratio (only if a benchmark was supplied, else "N/A")
      - trade count sanity fields (total signals, taken, skipped, still open)

    This is the table meant for your Excel/Numbers "Backtest Summary"
    sheet, broken out year by year.
    """
    if summary_by_year.height == 0:
        print("[WARN] summary_by_year.csv is empty or missing -- yearly summary will be empty.")
        return pl.DataFrame()

    rows = []
    for row in summary_by_year.rows(named=True):
        year_str = row["group"]
        exit_model = row["exit_model"]

        sub = ledger.filter(
            (pl.col("exit_model") == exit_model) &
            (pl.col("year") == int(year_str)) &
            (pl.col("outcome").is_in(["WIN", "LOSS"]))
        ).sort("entry_time")

        pnls = sub["pnl_usd"].to_list()
        entry_times = sub["entry_time"].to_list()

        # ---- annualized Sharpe / Sortino (proper version, not per-trade raw) ----
        sharpe_annualized = None
        sortino_annualized = None
        if len(pnls) >= 2:
            ann_factor = _annualization_factor(entry_times)
            mean_pnl = np.mean(pnls)
            std_pnl = np.std(pnls, ddof=1)
            if std_pnl > 0:
                sharpe_annualized = (mean_pnl / std_pnl) * math.sqrt(ann_factor)

            downside = [p for p in pnls if p < 0]
            if len(downside) >= 2:
                downside_std = np.std(downside, ddof=1)
                # GUARD: if every losing trade is (near-)identical in size
                # -- the normal case for FIXED_RISK_USD or FIXED_UNITS
                # sizing, where every loss = -risk_usd exactly -- the true
                # standard deviation is legitimately 0, but floating-point
                # arithmetic on large trade counts leaves tiny non-zero
                # noise (e.g. 1e-15) instead of an exact 0. Dividing by
                # that noise produces meaningless numbers in the
                # quadrillions. A relative-scale epsilon (tied to the
                # actual magnitude of the P&L values, not an absolute
                # constant) correctly treats "effectively zero variance"
                # as "no meaningful Sortino" and reports None/N/A instead
                # of a nonsense figure. This is the mathematically honest
                # outcome: Sortino is undefined when downside risk truly
                # doesn't vary trade to trade.
                scale = max(abs(mean_pnl), 1.0)
                epsilon = scale * 1e-9
                if downside_std > epsilon:
                    sortino_annualized = (mean_pnl / downside_std) * math.sqrt(ann_factor)

        calmar = compute_calmar_ratio(row.get("total_return_pct"), row.get("max_drawdown_pct"))

        treynor = None
        if benchmark_df is not None and len(pnls) >= 2:
            try:
                year_int = int(year_str)
                bench_sub = benchmark_df.filter(pl.col("date").dt.year() == year_int)
                bench_returns = bench_sub["return_pct"].to_list()
                treynor = compute_treynor_ratio(
                    row.get("total_return_pct", 0.0), config.risk_free_rate_pct,
                    pnls, bench_returns if bench_returns else None,
                )
            except (ValueError, TypeError):
                treynor = None

        rows.append({
            "year": year_str,
            "exit_model": exit_model,
            "total_signals": row.get("total_signals"),
            "total_trades": row.get("total_trades"),
            "skipped_overlap": row.get("skipped_overlap"),
            "still_open": row.get("still_open"),
            "wins": row.get("wins"),
            "losses": row.get("losses"),
            "win_rate_pct": row.get("win_rate_pct"),
            "gross_profit": row.get("gross_profit"),
            "gross_loss": row.get("gross_loss"),
            "net_pnl": row.get("net_pnl"),
            "avg_win_usd": row.get("avg_win_usd"),
            "avg_loss_usd": row.get("avg_loss_usd"),
            "avg_trade_usd": row.get("avg_trade_usd"),
            "largest_win_usd": row.get("largest_win_usd"),
            "largest_loss_usd": row.get("largest_loss_usd"),
            "profit_factor": row.get("profit_factor"),
            "expectancy_usd": row.get("expectancy_usd"),
            "payoff_ratio": row.get("payoff_ratio"),
            "avg_rr_achieved": row.get("avg_rr_achieved"),
            "avg_bars_held_win": row.get("avg_bars_held_win"),
            "avg_bars_held_loss": row.get("avg_bars_held_loss"),
            "max_drawdown_usd": row.get("max_drawdown_usd"),
            "max_drawdown_pct": row.get("max_drawdown_pct"),
            "max_consecutive_wins": row.get("max_consecutive_wins"),
            "max_consecutive_losses": row.get("max_consecutive_losses"),
            "sharpe_ratio_per_trade": row.get("sharpe_ratio"),
            "sharpe_ratio_annualized": round(sharpe_annualized, 4) if sharpe_annualized is not None else None,
            "sortino_ratio_per_trade": row.get("sortino_ratio"),
            "sortino_ratio_annualized": round(sortino_annualized, 4) if sortino_annualized is not None else None,
            "calmar_ratio": round(calmar, 4) if calmar is not None else None,
            "treynor_ratio": round(treynor, 4) if treynor is not None else "N/A (no benchmark supplied)",
            "starting_capital": row.get("starting_capital"),
            "ending_capital": row.get("ending_capital"),
            "total_return_pct": row.get("total_return_pct"),
        })

    result = pl.DataFrame(rows).sort(["exit_model", "year"])
    return result


# ============================================================================
# SECTION 5: EQUITY CURVE + DRAWDOWN RECONSTRUCTION (for charting)
# ============================================================================

def build_equity_curve(ledger: pl.DataFrame, exit_model: str) -> Tuple[List, List[float], List[float]]:
    """
    Rebuilds the equity curve and running drawdown for one exit model,
    in true chronological order (matching backtest.py's own equity
    settlement logic). Returns (dates, equity_values, drawdown_pct_values).
    """
    sub = ledger.filter(
        (pl.col("exit_model") == exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"]))
    ).sort("entry_time")

    if sub.height == 0:
        return [], [], []

    dates = sub["entry_time"].to_list()
    pnls = sub["pnl_usd"].to_numpy()
    starting_capital = sub["equity_after"][0] - pnls[0] if sub.height > 0 else 10000.0

    equity = starting_capital + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate(([starting_capital], equity)))[1:]
    drawdown_pct = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)

    return dates, equity.tolist(), drawdown_pct.tolist()


# ============================================================================
# SECTION 6: INDIVIDUAL CHART FUNCTIONS
# ============================================================================

def plot_equity_curves_all_models(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Equity curve for all 3 exit models overlaid, colorful, light theme."""
    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    for exit_model, color in EXIT_MODEL_COLORS.items():
        dates, equity, _ = build_equity_curve(ledger, exit_model)
        if dates:
            ax.plot(dates, equity, label=exit_model, color=color, linewidth=2.2, alpha=0.9)

    ax.set_title("Equity Curve by Exit Model")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_equity_with_regression(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    Equity curve for the primary exit model with a LINEAR REGRESSION
    trendline fitted over it -- shows whether the strategy's equity
    growth is trending up/down/flat over the backtest period, and by
    how much per trade on average (the regression slope).
    """
    dates, equity, _ = build_equity_curve(ledger, config.primary_exit_model)
    if not dates:
        print(f"[SKIP] No resolved trades for {config.primary_exit_model} -- skipping regression chart.")
        return

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    x = np.arange(len(equity))
    y = np.array(equity)

    # simple linear regression (least squares, no external dependency needed)
    slope, intercept = np.polyfit(x, y, 1)
    trend = slope * x + intercept

    # R^2 for the fit
    residuals = y - trend
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    ax.plot(dates, equity, color=COLORS["equity"], linewidth=1.8, alpha=0.85, label="Equity")
    ax.plot(dates, trend, color=COLORS["trend"], linewidth=2.5, linestyle="--",
            label=f"Linear trend (slope=${slope:.2f}/trade, R²={r_squared:.3f})")

    ax.set_title(f"Equity Curve with Linear Regression Trend ({config.primary_exit_model})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_drawdown(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Underwater/drawdown chart for the primary exit model, filled area."""
    dates, _, drawdown_pct = build_equity_curve(ledger, config.primary_exit_model)
    if not dates:
        return

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    ax.fill_between(dates, drawdown_pct, 0, color=COLORS["drawdown"], alpha=0.35)
    ax.plot(dates, drawdown_pct, color=COLORS["drawdown"], linewidth=1.5)
    ax.invert_yaxis()  # drawdown shown going down, conventional style

    max_dd = max(drawdown_pct) if drawdown_pct else 0
    ax.set_title(f"Drawdown (%) Over Time -- Max: {max_dd:.2f}% ({config.primary_exit_model})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_win_rate_by_year(summary_by_year: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Grouped bar chart: win rate per year, one bar cluster per exit model."""
    if summary_by_year.height == 0:
        return

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    years = sorted(summary_by_year["group"].unique().to_list())
    n_models = len(EXIT_MODEL_COLORS)
    bar_width = 0.8 / n_models
    x = np.arange(len(years))

    for i, (exit_model, color) in enumerate(EXIT_MODEL_COLORS.items()):
        vals = []
        for y in years:
            row = summary_by_year.filter(
                (pl.col("group") == y) & (pl.col("exit_model") == exit_model)
            )
            vals.append(row["win_rate_pct"][0] if row.height else 0)
        offset = (i - n_models / 2) * bar_width + bar_width / 2
        ax.bar(x + offset, vals, width=bar_width, label=exit_model, color=color, alpha=0.9,
               edgecolor="white", linewidth=0.8)

    ax.set_xticks(x)
    ax.set_xticklabels(years)
    ax.set_title("Win Rate (%) by Year")
    ax.set_xlabel("Year")
    ax.set_ylabel("Win Rate (%)")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_net_pnl_by_year(summary_by_year: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Bar chart: net P&L per year for the primary exit model, green/red by sign."""
    if summary_by_year.height == 0:
        return

    sub = summary_by_year.filter(pl.col("exit_model") == config.primary_exit_model).sort("group")
    if sub.height == 0:
        return

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    years = sub["group"].to_list()
    pnls = sub["net_pnl"].to_list()
    colors = [COLORS["win"] if p >= 0 else COLORS["loss"] for p in pnls]

    bars = ax.bar(years, pnls, color=colors, alpha=0.9, edgecolor="white", linewidth=0.8)
    ax.axhline(0, color="#888888", linewidth=1)

    for bar, val in zip(bars, pnls):
        ax.text(bar.get_x() + bar.get_width() / 2, val, f"${val:,.0f}",
                ha="center", va="bottom" if val >= 0 else "top", fontsize=9, fontweight="bold")

    ax.set_title(f"Net P&L by Year ({config.primary_exit_model})")
    ax.set_xlabel("Year")
    ax.set_ylabel("Net P&L ($)")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_profit_factor_by_timeframe(summary_by_timeframe: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Bar chart: profit factor per timeframe, all exit models grouped."""
    if summary_by_timeframe.height == 0:
        return

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    timeframes = sorted(summary_by_timeframe["group"].unique().to_list())
    n_models = len(EXIT_MODEL_COLORS)
    bar_width = 0.8 / n_models
    x = np.arange(len(timeframes))

    for i, (exit_model, color) in enumerate(EXIT_MODEL_COLORS.items()):
        vals = []
        for tf in timeframes:
            row = summary_by_timeframe.filter(
                (pl.col("group") == tf) & (pl.col("exit_model") == exit_model)
            )
            pf = row["profit_factor"][0] if row.height else None
            vals.append(pf if pf is not None else 0)
        offset = (i - n_models / 2) * bar_width + bar_width / 2
        ax.bar(x + offset, vals, width=bar_width, label=exit_model, color=color, alpha=0.9,
               edgecolor="white", linewidth=0.8)

    ax.axhline(1.0, color="#888888", linewidth=1.2, linestyle="--", label="Breakeven (PF=1.0)")
    ax.set_xticks(x)
    ax.set_xticklabels(timeframes)
    ax.set_title("Profit Factor by Timeframe")
    ax.set_xlabel("Timeframe")
    ax.set_ylabel("Profit Factor")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_direction_comparison(summary_by_direction: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Side-by-side comparison of BUY vs SELL performance (win rate + net P&L)."""
    if summary_by_direction.height == 0:
        return

    sub = summary_by_direction.filter(pl.col("exit_model") == config.primary_exit_model)
    if sub.height == 0:
        return

    _apply_light_theme()
    fig, axes = plt.subplots(1, 2, figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    directions = sub["group"].to_list()
    win_rates = sub["win_rate_pct"].to_list()
    net_pnls = sub["net_pnl"].to_list()
    colors_dir = [COLORS["neutral"] if d == "BUY" else COLORS["accent4"] for d in directions]

    axes[0].bar(directions, win_rates, color=colors_dir, alpha=0.9, edgecolor="white", linewidth=0.8)
    axes[0].set_title("Win Rate by Direction")
    axes[0].set_ylabel("Win Rate (%)")

    pnl_colors = [COLORS["win"] if p >= 0 else COLORS["loss"] for p in net_pnls]
    axes[1].bar(directions, net_pnls, color=pnl_colors, alpha=0.9, edgecolor="white", linewidth=0.8)
    axes[1].axhline(0, color="#888888", linewidth=1)
    axes[1].set_title("Net P&L by Direction")
    axes[1].set_ylabel("Net P&L ($)")

    fig.suptitle(f"BUY vs SELL Performance ({config.primary_exit_model})")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_session_performance(summary_by_session: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Win rate + net P&L by trading session (Asian / London / US, UTC entry bar)."""
    if summary_by_session.height == 0:
        return

    sub = summary_by_session.filter(pl.col("exit_model") == config.primary_exit_model)
    if sub.height == 0:
        return

    order = {name: i for i, name in enumerate(sessions.SESSION_ORDER)}
    sub = sub.with_columns(
        pl.col("group").replace(order).cast(pl.Int32).alias("_ord")
    ).sort("_ord")

    session_names = sub["group"].to_list()
    win_rates = sub["win_rate_pct"].to_list()
    net_pnls = sub["net_pnl"].to_list()
    trade_counts = sub["total_trades"].to_list()
    session_colors = {
        sessions.SESSION_ASIAN: "#F4B942",
        sessions.SESSION_LONDON: "#4A90D9",
        sessions.SESSION_US: "#34A853",
    }
    colors = [session_colors.get(s, COLORS["neutral"]) for s in session_names]

    _apply_light_theme()
    fig, axes = plt.subplots(1, 3, figsize=(config.figure_width * 1.35, config.figure_height),
                              dpi=config.figure_dpi)

    axes[0].bar(session_names, win_rates, color=colors, alpha=0.9, edgecolor="white", linewidth=0.8)
    axes[0].set_title("Win Rate by Session")
    axes[0].set_ylabel("Win Rate (%)")

    pnl_colors = [COLORS["win"] if p >= 0 else COLORS["loss"] for p in net_pnls]
    axes[1].bar(session_names, net_pnls, color=pnl_colors, alpha=0.9, edgecolor="white", linewidth=0.8)
    axes[1].axhline(0, color="#888888", linewidth=1)
    axes[1].set_title("Net P&L by Session")
    axes[1].set_ylabel("Net P&L ($)")

    axes[2].bar(session_names, trade_counts, color=colors, alpha=0.85, edgecolor="white", linewidth=0.8)
    axes[2].set_title("Trade Count by Session")
    axes[2].set_ylabel("Trades (WIN+LOSS)")

    fig.suptitle(
        f"Session Breakdown — {config.primary_exit_model}  "
        f"(entry bar {sessions.BROKER_TIME_LABEL}: Asian 00–07 · London 08–15 · US 16–23)"
    )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_profit_factor_by_session(summary_by_session: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Grouped bar chart: profit factor per session, all exit models."""
    if summary_by_session.height == 0:
        return

    session_names = sessions.SESSION_ORDER
    n_models = len(EXIT_MODEL_COLORS)
    bar_width = 0.8 / n_models
    x = np.arange(len(session_names))

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    for i, (exit_model, color) in enumerate(EXIT_MODEL_COLORS.items()):
        vals = []
        for sess in session_names:
            row = summary_by_session.filter(
                (pl.col("group") == sess) & (pl.col("exit_model") == exit_model)
            )
            pf = row["profit_factor"][0] if row.height else None
            vals.append(pf if pf is not None else 0)
        offset = (i - n_models / 2) * bar_width + bar_width / 2
        ax.bar(x + offset, vals, width=bar_width, label=exit_model, color=color, alpha=0.9,
               edgecolor="white", linewidth=0.8)

    ax.axhline(1.0, color="#888888", linewidth=1.2, linestyle="--", label="Breakeven (PF=1.0)")
    ax.set_xticks(x)
    ax.set_xticklabels(session_names)
    ax.set_title("Profit Factor by Session (IC Markets server time)")
    ax.set_xlabel("Session")
    ax.set_ylabel("Profit Factor")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_monthly_heatmap(summary_by_month: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    Calendar-style heatmap: rows = years, columns = months, color = net
    P&L for that month, using the primary exit model. Vivid diverging
    palette (red=loss, green=profit), light background.
    """
    if summary_by_month.height == 0:
        return

    sub = summary_by_month.filter(pl.col("exit_model") == config.primary_exit_model)
    if sub.height == 0:
        return

    # month_key format is "YYYY-MM"
    parsed = []
    for row in sub.rows(named=True):
        try:
            year_str, month_str = row["group"].split("-")
            parsed.append((int(year_str), int(month_str), row["net_pnl"]))
        except (ValueError, AttributeError):
            continue

    if not parsed:
        return

    years = sorted(set(p[0] for p in parsed))
    grid = np.full((len(years), 12), np.nan)
    year_index = {y: i for i, y in enumerate(years)}

    for year, month, pnl in parsed:
        grid[year_index[year], month - 1] = pnl

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, max(3, len(years) * 0.6)), dpi=config.figure_dpi)

    max_abs = np.nanmax(np.abs(grid)) if not np.all(np.isnan(grid)) else 1
    im = ax.imshow(grid, cmap="RdYlGn", aspect="auto", vmin=-max_abs, vmax=max_abs)

    ax.set_xticks(range(12))
    ax.set_xticklabels(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels(years)

    for i in range(len(years)):
        for j in range(12):
            val = grid[i, j]
            if not np.isnan(val):
                ax.text(j, i, f"${val:,.0f}", ha="center", va="center", fontsize=8,
                        color="#222222", fontweight="bold")

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Net P&L ($)")

    ax.set_title(f"Monthly Net P&L Heatmap ({config.primary_exit_model})")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_r_multiple_distribution(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Histogram of realized R-multiples (pnl / risk_usd) for the primary exit model."""
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"])) &
        (pl.col("risk_usd") > 0)
    )
    if sub.height == 0:
        return

    r_multiples = (sub["pnl_usd"] / sub["risk_usd"]).to_list()

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    colors_hist = [COLORS["win"] if r >= 0 else COLORS["loss"] for r in r_multiples]
    n, bins, patches = ax.hist(r_multiples, bins=40, edgecolor="white", linewidth=0.5)
    for patch, left_edge in zip(patches, bins[:-1]):
        patch.set_facecolor(COLORS["win"] if left_edge >= 0 else COLORS["loss"])
        patch.set_alpha(0.85)

    ax.axvline(0, color="#333333", linewidth=1.5, linestyle="--")
    ax.axvline(np.mean(r_multiples), color=COLORS["accent2"], linewidth=2,
               label=f"Mean R = {np.mean(r_multiples):.2f}")

    ax.set_title(f"R-Multiple Distribution ({config.primary_exit_model})")
    ax.set_xlabel("R-Multiple (P&L / Risk)")
    ax.set_ylabel("Number of Trades")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_hammer_color_breakdown(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Pie/donut-style bar showing trade counts by hammer color (GREEN/RED/DOJI)."""
    sub = ledger.filter(pl.col("exit_model") == config.primary_exit_model)
    if sub.height == 0:
        return

    counts = sub.group_by("hammer_color").agg(pl.len().alias("count")).sort("hammer_color")
    if counts.height == 0:
        return

    colors_map = {"GREEN": COLORS["win"], "RED": COLORS["loss"], "DOJI": COLORS["neutral"]}
    labels = counts["hammer_color"].to_list()
    values = counts["count"].to_list()
    colors_pie = [colors_map.get(l, COLORS["accent1"]) for l in labels]

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width * 0.6, config.figure_height), dpi=config.figure_dpi)

    ax.pie(values, labels=labels, colors=colors_pie, autopct="%1.1f%%",
           startangle=90, wedgeprops={"edgecolor": "white", "linewidth": 1.5},
           textprops={"fontsize": 11, "fontweight": "bold"})
    ax.set_title("Hammer Signals by Color")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_metrics_comparison_table(summary_overall: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    Rendered TABLE (not a chart) comparing all key metrics across the 3
    exit models side by side -- easiest single image to screenshot for
    a quick "how different are worst/best/candle_bias" comparison.
    """
    if summary_overall.height == 0:
        return

    metrics_to_show = [
        ("total_trades", "Total Trades", "{:.0f}"),
        ("win_rate_pct", "Win Rate (%)", "{:.2f}"),
        ("net_pnl", "Net P&L ($)", "${:,.2f}"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("expectancy_usd", "Expectancy ($)", "${:,.2f}"),
        ("payoff_ratio", "Payoff Ratio", "{:.2f}"),
        ("avg_rr_achieved", "Avg R Achieved", "{:.2f}"),
        ("max_drawdown_usd", "Max Drawdown ($)", "${:,.2f}"),
        ("max_drawdown_pct", "Max Drawdown (%)", "{:.2f}"),
        ("max_consecutive_wins", "Max Win Streak", "{:.0f}"),
        ("max_consecutive_losses", "Max Loss Streak", "{:.0f}"),
        ("sharpe_ratio", "Sharpe (per-trade)", "{:.3f}"),
        ("sortino_ratio", "Sortino (per-trade)", "{:.3f}"),
        ("total_return_pct", "Total Return (%)", "{:.2f}"),
    ]

    models = ["worst_case", "best_case", "candle_bias"]
    table_data = []
    for key, label, fmt in metrics_to_show:
        row = [label]
        for m in models:
            sub = summary_overall.filter(pl.col("exit_model") == m)
            if sub.height == 0:
                row.append("N/A")
                continue
            val = sub[key][0]
            if val is None:
                row.append("N/A")
            else:
                try:
                    row.append(fmt.format(val))
                except (ValueError, TypeError):
                    row.append(str(val))
        table_data.append(row)

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height * 1.3), dpi=config.figure_dpi)
    ax.axis("off")

    col_labels = ["Metric"] + [m.replace("_", " ").title() for m in models]
    table = ax.table(cellText=table_data, colLabels=col_labels, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.8)

    for j, m in enumerate(models):
        header_color = EXIT_MODEL_COLORS.get(m, COLORS["neutral"])
        table[0, j + 1].set_facecolor(header_color)
        table[0, j + 1].set_text_props(color="white", fontweight="bold")
    table[0, 0].set_facecolor("#34495E")
    table[0, 0].set_text_props(color="white", fontweight="bold")

    for i in range(1, len(table_data) + 1):
        row_color = "#F8F9FA" if i % 2 == 0 else "#FFFFFF"
        for j in range(len(col_labels)):
            table[i, j].set_facecolor(row_color)

    ax.set_title("Overall Metrics Comparison Across Exit Models", pad=20)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_bars_held_distribution(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Histogram of bars held to resolution, split win vs loss."""
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"])) &
        (pl.col("bars_held").is_not_null())
    )
    if sub.height == 0:
        return

    wins = sub.filter(pl.col("outcome") == "WIN")["bars_held"].to_list()
    losses = sub.filter(pl.col("outcome") == "LOSS")["bars_held"].to_list()

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    bins = np.linspace(0, max(max(wins, default=1), max(losses, default=1)), 30)
    ax.hist(wins, bins=bins, alpha=0.7, color=COLORS["win"], label=f"Wins (n={len(wins)})", edgecolor="white")
    ax.hist(losses, bins=bins, alpha=0.7, color=COLORS["loss"], label=f"Losses (n={len(losses)})", edgecolor="white")

    ax.set_title(f"Bars Held to Resolution ({config.primary_exit_model})")
    ax.set_xlabel("Bars Held")
    ax.set_ylabel("Number of Trades")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


# ============================================================================
# SECTION 6B: ADVANCED CHARTS
# ============================================================================
# Six additions on top of the original 12 charts, all following the same
# light-theme / vivid-palette style. These go deeper than aggregate
# stats -- they're aimed at answering the questions a serious reviewer
# actually asks: is the edge stable over time, when exactly does it hurt,
# which sessions/days drive it, which parameters matter, and how much of
# the smooth equity curve is luck of trade ORDERING rather than a real
# edge (Monte Carlo).

def plot_rolling_performance(ledger: pl.DataFrame, config: PlottingConfig, out_path: str,
                              window_trades: int = 500):
    """
    Rolling win rate and rolling Sharpe over a moving window of trades
    (not calendar time, since trade frequency varies a lot by timeframe
    mix) -- shows whether the edge is STABLE, decaying, or improving
    over the life of the backtest, instead of one static number for the
    whole period.
    """
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"]))
    ).sort("entry_time")

    if sub.height < window_trades * 2:
        print(f"[SKIP] Not enough trades for a {window_trades}-trade rolling window.")
        return

    dates = sub["entry_time"].to_list()
    is_win = (sub["outcome"] == "WIN").cast(pl.Int8).to_numpy()
    pnl = sub["pnl_usd"].to_numpy()

    # rolling win rate: for each index i >= window_trades, the win rate
    # over the PRECEDING window_trades trades (indices i-window_trades..i-1).
    # This produces exactly len(is_win) - window_trades values, matching
    # dates[window_trades:] exactly (one rolling value per trade from
    # window_trades onward).
    n = len(is_win)
    cum_wins = np.concatenate(([0], np.cumsum(is_win)))  # length n+1
    rolling_win_rate = (cum_wins[window_trades:n] - cum_wins[0:n - window_trades]) / window_trades * 100.0

    # rolling Sharpe (mean/std over the same moving window)
    rolling_sharpe = np.full(len(pnl) - window_trades, np.nan)
    for i in range(len(rolling_sharpe)):
        w = pnl[i:i + window_trades]
        std = w.std(ddof=1)
        rolling_sharpe[i] = (w.mean() / std) if std > 0 else np.nan

    plot_dates = dates[window_trades:]

    _apply_light_theme()
    fig, axes = plt.subplots(2, 1, figsize=(config.figure_width, config.figure_height * 1.3),
                              dpi=config.figure_dpi, sharex=True)

    axes[0].plot(plot_dates, rolling_win_rate, color=COLORS["neutral"], linewidth=1.8)
    axes[0].axhline(50, color="#888888", linewidth=1, linestyle="--")
    axes[0].fill_between(plot_dates, rolling_win_rate, 50,
                          where=(rolling_win_rate >= 50), color=COLORS["win"], alpha=0.15)
    axes[0].fill_between(plot_dates, rolling_win_rate, 50,
                          where=(rolling_win_rate < 50), color=COLORS["loss"], alpha=0.15)
    axes[0].set_title(f"Rolling Win Rate (window = {window_trades} trades)")
    axes[0].set_ylabel("Win Rate (%)")

    axes[1].plot(plot_dates, rolling_sharpe, color=COLORS["accent2"], linewidth=1.8)
    axes[1].axhline(0, color="#888888", linewidth=1, linestyle="--")
    axes[1].set_title(f"Rolling Sharpe (window = {window_trades} trades, per-trade)")
    axes[1].set_ylabel("Sharpe")
    axes[1].set_xlabel("Date")
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    fig.autofmt_xdate()
    fig.suptitle(f"Rolling Performance Over Time ({config.primary_exit_model})")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_drawdown_duration(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    For every drawdown period (peak -> trough -> back to new peak),
    measures how many CALENDAR DAYS it took to recover. This matters
    more to a trader living through it than the peak drawdown % alone --
    a 10% drawdown that recovers in a week feels very different from one
    that takes 8 months.
    """
    dates, equity, _ = build_equity_curve(ledger, config.primary_exit_model)
    if not dates:
        return

    equity = np.array(equity)
    dates_arr = np.array(dates)
    running_peak = np.maximum.accumulate(equity)

    is_underwater = equity < running_peak
    durations_days = []
    depths_pct = []

    i = 0
    n = len(equity)
    while i < n:
        if is_underwater[i]:
            start_idx = i
            peak_before = running_peak[i]
            while i < n and equity[i] < peak_before:
                i += 1
            end_idx = min(i, n - 1)
            duration = (dates_arr[end_idx] - dates_arr[start_idx]).days
            depth = (peak_before - equity[start_idx:end_idx + 1].min()) / peak_before * 100.0
            if duration > 0:
                durations_days.append(duration)
                depths_pct.append(depth)
        else:
            i += 1

    if not durations_days:
        print("[SKIP] No completed drawdown periods found for duration analysis.")
        return

    _apply_light_theme()
    fig, axes = plt.subplots(1, 2, figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    axes[0].hist(durations_days, bins=min(25, len(durations_days)), color=COLORS["accent4"],
                 alpha=0.85, edgecolor="white")
    axes[0].axvline(np.median(durations_days), color=COLORS["accent2"], linewidth=2,
                     label=f"Median = {np.median(durations_days):.0f} days")
    axes[0].set_title("Drawdown Recovery Time")
    axes[0].set_xlabel("Days to Recover")
    axes[0].set_ylabel("Number of Drawdown Periods")
    axes[0].legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")

    axes[1].scatter(durations_days, depths_pct, color=COLORS["drawdown"], alpha=0.6, s=40,
                     edgecolor="white")
    axes[1].set_title("Drawdown Depth vs Recovery Time")
    axes[1].set_xlabel("Days to Recover")
    axes[1].set_ylabel("Drawdown Depth (%)")

    fig.suptitle(f"Drawdown Duration Analysis ({config.primary_exit_model}) -- "
                 f"{len(durations_days)} drawdown periods found")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_time_of_day_performance(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    Net P&L and trade count broken down by HOUR OF DAY (UTC, matching
    your fetched data's timestamps) -- reveals whether performance is
    concentrated in specific trading sessions (Asian/London/NY overlap)
    rather than spread evenly, which matters a lot for a short-timeframe
    gold strategy.
    """
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"]))
    )
    if sub.height == 0:
        return

    sub = sub.with_columns(pl.col("entry_time").dt.hour().alias("hour"))
    grouped = sub.group_by("hour").agg([
        pl.col("pnl_usd").sum().alias("net_pnl"),
        pl.len().alias("trade_count"),
    ]).sort("hour")

    hours = grouped["hour"].to_list()
    pnl_by_hour = grouped["net_pnl"].to_list()
    counts_by_hour = grouped["trade_count"].to_list()

    _apply_light_theme()
    fig, axes = plt.subplots(2, 1, figsize=(config.figure_width, config.figure_height * 1.2),
                              dpi=config.figure_dpi, sharex=True)

    colors_pnl = [COLORS["win"] if p >= 0 else COLORS["loss"] for p in pnl_by_hour]
    axes[0].bar(hours, pnl_by_hour, color=colors_pnl, alpha=0.9, edgecolor="white")
    axes[0].axhline(0, color="#888888", linewidth=1)
    axes[0].set_title("Net P&L by Hour of Day (UTC)")
    axes[0].set_ylabel("Net P&L ($)")

    axes[1].bar(hours, counts_by_hour, color=COLORS["neutral"], alpha=0.85, edgecolor="white")
    axes[1].set_title("Trade Count by Hour of Day (UTC)")
    axes[1].set_xlabel("Hour (UTC)")
    axes[1].set_ylabel("Number of Trades")
    axes[1].set_xticks(range(0, 24))

    fig.suptitle(f"Time-of-Day Breakdown ({config.primary_exit_model})")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_day_of_week_performance(ledger: pl.DataFrame, config: PlottingConfig, out_path: str):
    """Net P&L and win rate broken down by day of week."""
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"]))
    )
    if sub.height == 0:
        return

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    sub = sub.with_columns((pl.col("entry_time").dt.weekday() - 1).alias("dow"))
    grouped = sub.group_by("dow").agg([
        pl.col("pnl_usd").sum().alias("net_pnl"),
        (pl.col("outcome") == "WIN").mean().alias("win_rate"),
        pl.len().alias("trade_count"),
    ]).sort("dow")

    dows = grouped["dow"].to_list()
    labels = [day_names[d] for d in dows]
    pnl_vals = grouped["net_pnl"].to_list()
    win_rates = [w * 100 for w in grouped["win_rate"].to_list()]

    _apply_light_theme()
    fig, axes = plt.subplots(1, 2, figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    colors_pnl = [COLORS["win"] if p >= 0 else COLORS["loss"] for p in pnl_vals]
    axes[0].bar(labels, pnl_vals, color=colors_pnl, alpha=0.9, edgecolor="white")
    axes[0].axhline(0, color="#888888", linewidth=1)
    axes[0].set_title("Net P&L by Day of Week")
    axes[0].set_ylabel("Net P&L ($)")

    axes[1].bar(labels, win_rates, color=COLORS["accent3"], alpha=0.9, edgecolor="white")
    axes[1].axhline(50, color="#888888", linewidth=1, linestyle="--")
    axes[1].set_title("Win Rate by Day of Week")
    axes[1].set_ylabel("Win Rate (%)")

    fig.suptitle(f"Day-of-Week Breakdown ({config.primary_exit_model})")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_parameter_sensitivity(summary_by_timeframe: pl.DataFrame, config: PlottingConfig, out_path: str):
    """
    Shows how much each TIMEFRAME choice moves the outcome (profit
    factor and win rate), as a proxy for parameter sensitivity -- since
    timeframe is the parameter that changes the underlying signal
    density the most. Bars sorted by impact, so the client can see at a
    glance which settings matter most to results.
    """
    if summary_by_timeframe.height == 0:
        return

    sub = summary_by_timeframe.filter(pl.col("exit_model") == config.primary_exit_model)
    if sub.height == 0:
        return

    sub = sub.sort("profit_factor", descending=True)
    timeframes = sub["group"].to_list()
    pf_vals = sub["profit_factor"].to_list()
    wr_vals = sub["win_rate_pct"].to_list()

    _apply_light_theme()
    fig, axes = plt.subplots(1, 2, figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    colors_pf = [COLORS["win"] if pf >= 1.0 else COLORS["loss"] for pf in pf_vals]
    axes[0].barh(timeframes, pf_vals, color=colors_pf, alpha=0.9, edgecolor="white")
    axes[0].axvline(1.0, color="#888888", linewidth=1.2, linestyle="--", label="Breakeven")
    axes[0].set_title("Profit Factor -- Sorted by Impact")
    axes[0].set_xlabel("Profit Factor")
    axes[0].legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")

    axes[1].barh(timeframes, wr_vals, color=COLORS["accent1"], alpha=0.9, edgecolor="white")
    axes[1].axvline(50, color="#888888", linewidth=1, linestyle="--")
    axes[1].set_title("Win Rate -- Same Order")
    axes[1].set_xlabel("Win Rate (%)")

    fig.suptitle(f"Timeframe Sensitivity ({config.primary_exit_model}) -- "
                 f"which timeframe choice matters most")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_monte_carlo_simulation(ledger: pl.DataFrame, config: PlottingConfig, out_path: str,
                                 n_simulations: int = 500):
    """
    BONUS ADDITION (not explicitly requested, but the single most
    important addition for trusting an equity curve): reshuffles the
    ACTUAL trade P&L values (not new random numbers -- your real wins
    and losses, just in randomized order) thousands of times, and plots
    the resulting spread of equity curves. If your real historical
    equity curve sits comfortably inside this spread, the smooth curve
    reflects a genuine trade-level edge. If most simulated paths look
    much worse than the real one, the smoothness may partly be luck of
    the specific ORDER trades happened to occur in.
    """
    sub = ledger.filter(
        (pl.col("exit_model") == config.primary_exit_model) &
        (pl.col("outcome").is_in(["WIN", "LOSS"]))
    ).sort("entry_time")

    if sub.height < 30:
        print("[SKIP] Not enough resolved trades for a meaningful Monte Carlo simulation.")
        return

    pnls = sub["pnl_usd"].to_numpy()
    starting_capital = float(sub["equity_after"][0] - pnls[0])
    n_trades = len(pnls)

    rng = np.random.default_rng(42)  # fixed seed -> reproducible report
    simulated_curves = np.empty((n_simulations, n_trades + 1))
    simulated_curves[:, 0] = starting_capital

    for i in range(n_simulations):
        shuffled = rng.permutation(pnls)
        simulated_curves[i, 1:] = starting_capital + np.cumsum(shuffled)

    real_curve = np.concatenate(([starting_capital], starting_capital + np.cumsum(pnls)))

    p5 = np.percentile(simulated_curves, 5, axis=0)
    p25 = np.percentile(simulated_curves, 25, axis=0)
    p50 = np.percentile(simulated_curves, 50, axis=0)
    p75 = np.percentile(simulated_curves, 75, axis=0)
    p95 = np.percentile(simulated_curves, 95, axis=0)

    x = np.arange(n_trades + 1)

    _apply_light_theme()
    fig, ax = plt.subplots(figsize=(config.figure_width, config.figure_height), dpi=config.figure_dpi)

    ax.fill_between(x, p5, p95, color=COLORS["neutral"], alpha=0.15, label="5th-95th percentile")
    ax.fill_between(x, p25, p75, color=COLORS["neutral"], alpha=0.30, label="25th-75th percentile")
    ax.plot(x, p50, color=COLORS["neutral"], linewidth=1.5, linestyle="--", label="Median simulated path")
    ax.plot(x, real_curve, color=COLORS["win"], linewidth=2.2, label="Actual historical path")

    ax.set_title(f"Monte Carlo Simulation -- {n_simulations} Reshuffles of Real Trades "
                 f"({config.primary_exit_model})")
    ax.set_xlabel("Trade Number")
    ax.set_ylabel("Equity ($)")
    ax.legend(frameon=True, facecolor="white", edgecolor="#CCCCCC")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_tear_sheet(
    ledger: pl.DataFrame,
    summary_overall: pl.DataFrame,
    summary_by_timeframe: pl.DataFrame,
    config: PlottingConfig,
    out_path: str,
):
    """
    ONE combined, dense, professional-style summary image -- everything
    a reviewer needs at a glance on a single page, similar to a
    QuantConnect/Backtrader style tear sheet. This is the single file
    to lead with when presenting results; the other 18 charts are for
    anyone who wants to dig into one specific angle afterward.

    Panels: equity curve (top, full width), then a 2x3 grid of
    drawdown / monthly heatmap-lite / win rate by year / profit factor
    by timeframe / R-multiple distribution / key stats table.
    """
    exit_model = config.primary_exit_model
    dates, equity, drawdown_pct = build_equity_curve(ledger, exit_model)
    if not dates:
        print("[SKIP] No resolved trades -- cannot build tear sheet.")
        return

    _apply_light_theme()
    fig = plt.figure(figsize=(config.figure_width * 1.6, config.figure_height * 2.4), dpi=config.figure_dpi)
    gs = fig.add_gridspec(4, 3, height_ratios=[1.3, 1, 1, 1], hspace=0.55, wspace=0.35)

    # ---- Row 1: equity curve, full width ----
    ax_equity = fig.add_subplot(gs[0, :])
    ax_equity.plot(dates, equity, color=COLORS["equity"], linewidth=2)
    ax_equity.set_title("Equity Curve", fontsize=12, fontweight="bold")
    ax_equity.set_ylabel("Equity ($)")
    ax_equity.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    # ---- Row 2: drawdown (full width) ----
    ax_dd = fig.add_subplot(gs[1, :])
    ax_dd.fill_between(dates, drawdown_pct, 0, color=COLORS["drawdown"], alpha=0.35)
    ax_dd.plot(dates, drawdown_pct, color=COLORS["drawdown"], linewidth=1.2)
    ax_dd.invert_yaxis()
    ax_dd.set_title(f"Drawdown (%) -- Max {max(drawdown_pct):.2f}%", fontsize=12, fontweight="bold")
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    # ---- Row 3, col 1: win rate by year ----
    ax_wr = fig.add_subplot(gs[2, 0])
    by_year_sub = summary_overall  # placeholder, replaced below if by_year data passed differently
    ax_wr.axis("off")
    ax_wr.text(0.5, 0.5, "(see win_rate_by_year.png\nfor full yearly detail)",
               ha="center", va="center", fontsize=9, color="#888888", style="italic")
    ax_wr.set_title("Win Rate by Year", fontsize=11, fontweight="bold")

    # ---- Row 3, col 2: profit factor by timeframe ----
    ax_pf = fig.add_subplot(gs[2, 1])
    if summary_by_timeframe.height:
        sub_pf = summary_by_timeframe.filter(pl.col("exit_model") == exit_model).sort("profit_factor")
        tfs = sub_pf["group"].to_list()
        pfs = sub_pf["profit_factor"].to_list()
        colors_pf = [COLORS["win"] if pf >= 1.0 else COLORS["loss"] for pf in pfs]
        ax_pf.barh(tfs, pfs, color=colors_pf, alpha=0.9, edgecolor="white")
        ax_pf.axvline(1.0, color="#888888", linewidth=1, linestyle="--")
    ax_pf.set_title("Profit Factor by Timeframe", fontsize=11, fontweight="bold")

    # ---- Row 3, col 3: R-multiple distribution ----
    ax_r = fig.add_subplot(gs[2, 2])
    r_sub = ledger.filter(
        (pl.col("exit_model") == exit_model) & (pl.col("outcome").is_in(["WIN", "LOSS"])) &
        (pl.col("risk_usd") > 0)
    )
    if r_sub.height:
        r_multiples = (r_sub["pnl_usd"] / r_sub["risk_usd"]).to_list()
        n, bins, patches = ax_r.hist(r_multiples, bins=30, edgecolor="white", linewidth=0.4)
        for patch, left_edge in zip(patches, bins[:-1]):
            patch.set_facecolor(COLORS["win"] if left_edge >= 0 else COLORS["loss"])
            patch.set_alpha(0.85)
    ax_r.set_title("R-Multiple Distribution", fontsize=11, fontweight="bold")

    # ---- Row 4: key stats table, full width ----
    ax_table = fig.add_subplot(gs[3, :])
    ax_table.axis("off")
    if summary_overall.height:
        row = summary_overall.filter(pl.col("exit_model") == exit_model)
        if row.height:
            r = row.row(0, named=True)
            stats_line_1 = (
                f"Trades: {r.get('total_trades')}   |   Win Rate: {r.get('win_rate_pct'):.2f}%   |   "
                f"Net P&L: USD {r.get('net_pnl'):,.2f}   |   Profit Factor: {r.get('profit_factor'):.2f}"
            )
            stats_line_2 = (
                f"Max Drawdown: USD {r.get('max_drawdown_usd'):,.2f} ({r.get('max_drawdown_pct'):.2f}%)   |   "
                f"Expectancy: USD {r.get('expectancy_usd'):.2f}   |   Sharpe: {r.get('sharpe_ratio'):.3f}   |   "
                f"Avg RR Achieved: {r.get('avg_rr_achieved'):.2f}"
            )
            ax_table.text(0.5, 0.65, stats_line_1, ha="center", va="center",
                           fontsize=12, fontweight="bold", color=COLORS["text"])
            ax_table.text(0.5, 0.25, stats_line_2, ha="center", va="center",
                           fontsize=11, color=COLORS["text"])

    fig.suptitle(f"Strategy Tear Sheet -- Exit Model: {exit_model}", fontsize=16, fontweight="bold")
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


# ============================================================================
# SECTION 7: TOP-LEVEL RUNNER
# ============================================================================

def generate_all_plots(config: PlottingConfig) -> None:
    """Generates every chart into config.output_dir."""
    os.makedirs(config.output_dir, exist_ok=True)

    print("=" * 70)
    print(f"PLOTTING: reading from {config.backtest_output_dir}")
    print("=" * 70)

    ledger = load_trade_ledger(config)
    summary_overall = load_summary(config, "overall")
    summary_by_year = load_summary(config, "by_year")
    summary_by_month = load_summary(config, "by_month")
    summary_by_timeframe = load_summary(config, "by_timeframe")
    summary_by_direction = load_summary(config, "by_direction")
    summary_by_session = load_summary(config, "by_session")

    charts = [
        ("equity_curves_all_models.png", lambda p: plot_equity_curves_all_models(ledger, config, p)),
        ("equity_with_regression.png", lambda p: plot_equity_with_regression(ledger, config, p)),
        ("drawdown.png", lambda p: plot_drawdown(ledger, config, p)),
        ("win_rate_by_year.png", lambda p: plot_win_rate_by_year(summary_by_year, config, p)),
        ("net_pnl_by_year.png", lambda p: plot_net_pnl_by_year(summary_by_year, config, p)),
        ("profit_factor_by_timeframe.png", lambda p: plot_profit_factor_by_timeframe(summary_by_timeframe, config, p)),
        ("direction_comparison.png", lambda p: plot_direction_comparison(summary_by_direction, config, p)),
        ("session_performance.png", lambda p: plot_session_performance(summary_by_session, config, p)),
        ("profit_factor_by_session.png", lambda p: plot_profit_factor_by_session(summary_by_session, config, p)),
        ("monthly_heatmap.png", lambda p: plot_monthly_heatmap(summary_by_month, config, p)),
        ("r_multiple_distribution.png", lambda p: plot_r_multiple_distribution(ledger, config, p)),
        ("hammer_color_breakdown.png", lambda p: plot_hammer_color_breakdown(ledger, config, p)),
        ("metrics_comparison_table.png", lambda p: plot_metrics_comparison_table(summary_overall, config, p)),
        ("bars_held_distribution.png", lambda p: plot_bars_held_distribution(ledger, config, p)),
        ("rolling_performance.png", lambda p: plot_rolling_performance(ledger, config, p)),
        ("drawdown_duration.png", lambda p: plot_drawdown_duration(ledger, config, p)),
        ("time_of_day_performance.png", lambda p: plot_time_of_day_performance(ledger, config, p)),
        ("day_of_week_performance.png", lambda p: plot_day_of_week_performance(ledger, config, p)),
        ("parameter_sensitivity.png", lambda p: plot_parameter_sensitivity(summary_by_timeframe, config, p)),
        ("monte_carlo_simulation.png", lambda p: plot_monte_carlo_simulation(ledger, config, p)),
        ("tear_sheet.png", lambda p: plot_tear_sheet(ledger, summary_overall, summary_by_timeframe, config, p)),
    ]

    for filename, plot_fn in charts:
        out_path = os.path.join(config.output_dir, filename)
        try:
            plot_fn(out_path)
            if os.path.exists(out_path):
                print(f"  [OK] {filename}")
            else:
                print(f"  [SKIP] {filename} (no data available)")
        except Exception as e:
            print(f"  [ERROR] {filename}: {e}")

    print(f"\n[OK] Charts saved to: {config.output_dir}/")


def generate_yearly_summary(config: PlottingConfig) -> pl.DataFrame:
    """Generates the detailed yearly metrics table and saves it as CSV."""
    ledger = load_trade_ledger(config)
    summary_by_year = load_summary(config, "by_year")
    benchmark_df = load_benchmark_returns(config.benchmark_returns_csv)

    yearly = compute_yearly_summary(ledger, summary_by_year, config, benchmark_df)

    os.makedirs(config.output_dir, exist_ok=True)
    out_path = os.path.join(config.output_dir, "yearly_detailed_summary.csv")
    yearly.write_csv(out_path)
    print(f"[OK] Yearly detailed summary saved to: {out_path}")

    if yearly.height:
        print("\n--- YEARLY SUMMARY PREVIEW ---")
        preview_cols = ["year", "exit_model", "total_trades", "win_rate_pct", "net_pnl",
                         "profit_factor", "sharpe_ratio_annualized", "sortino_ratio_annualized",
                         "calmar_ratio", "treynor_ratio", "max_drawdown_pct"]
        print(yearly.select(preview_cols))

    return yearly


def run_full_report(config: PlottingConfig) -> None:
    """Runs both plot generation and the yearly summary table in one call."""
    generate_all_plots(config)
    print()
    generate_yearly_summary(config)


# ============================================================================
# SECTION 8: DEMO / EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    my_config = PlottingConfig(
        backtest_output_dir="output/run_no_overlap",
        primary_exit_model="worst_case",
        output_dir="plots",
        benchmark_returns_csv=None,   # set this to enable Treynor ratio
        risk_free_rate_pct=0.0,
    )
    run_full_report(my_config)