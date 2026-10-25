"""Plot helpers for the forecast notebooks (EDA and visuals only, no pipeline logic).

Loads the contract parquet and the actual prices through the existing loaders,
attaches actuals, lead times and blocks with :mod:`v2g.forecast.evaluate`, and
draws the figures used in ``notebooks/01_model_performance.ipynb`` and
``notebooks/02_forecast_horizons.ipynb``. Every function returns a matplotlib
Figure; :func:`save` writes it to ``artefacts/figures/``.

Conventions: one fixed colour per model (:data:`MODEL_COLORS`), models always in
:data:`MODEL_ORDER`, prices in £/MWh, times shown in Europe/London local time
(converted from UTC for display only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.patches import Patch

from shared.schema import ForecastConfig
from v2g.forecast import evaluate as ev
from v2g.forecast.config import load_forecast_config, window_dates
from v2g.forecast.io import (
    DEFAULT_FORECAST_PATH,
    DEFAULT_PRICES_PATH,
    load_forecasts,
    load_prices,
)
from v2g.forecast.timing import tzinfo

MODEL_ORDER = ["naive_weekly", "naive_daily", "lear", "lear_x"]
# Validated with the dataviz palette checker: adjacent-pair CVD and normal-vision separation pass;
# the grey deliberately fails the chroma floor because the weekly baseline is meant to recede.
MODEL_COLORS = {
    "naive_weekly": "#5B6168",
    "naive_daily": "#6DB6E8",
    "lear": "#1F4E99",
    "lear_x": "#D9731A",
}
MODEL_LABELS = {
    "naive_weekly": "naive weekly",
    "naive_daily": "naive daily",
    "lear": "LEAR",
    "lear_x": "LEAR + exog (lear_x)",
}
INK, MUTED, GRID, ACTUAL = "#1F2328", "#6B7280", "#E5E7EB", "#111111"
HANDOFF_PATH = Path("artefacts/forecasts/handoff_price_forecasts.parquet")
FIGURES_DIR = Path("artefacts/figures")
PRICE_UNIT = "£/MWh"
REGIME_EDGES = [-np.inf, 0.0, 150.0, np.inf]
REGIME_LABELS = ["< 0", "0 to 150", "≥ 150"]
BLOCK_BOUNDARY_H = 14.0  # 10:00 issue -> local midnight


def apply_style() -> None:
    """Quiet, readable defaults: hairline grid, no top/right spines, neutral ink."""
    plt.rcParams.update(
        {
            "figure.dpi": 110,
            "savefig.dpi": 150,
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "axes.labelsize": 11,
            "axes.edgecolor": GRID,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "legend.frameon": False,
            "legend.fontsize": 10,
            "lines.linewidth": 2.0,
            "text.color": INK,
        }
    )


# ------------------------------------------------------------------------ data


@dataclass
class BacktestData:
    frame: (
        pd.DataFrame
    )  # one row per (model, issue, target) with actual, lead_h, block, slot, local times
    models: list[str]
    cfg: ForecastConfig
    tz: ZoneInfo
    source: Path | None = None
    missing_models: list[str] = field(default_factory=list)

    def of(self, model: str) -> pd.DataFrame:
        return self.frame[self.frame["model"] == model]


def from_frames(
    forecasts: pd.DataFrame,
    prices: pd.Series,
    cfg: ForecastConfig,
    start=None,
    end=None,
) -> BacktestData:
    """Attach actuals/leads/blocks to contract rows and add display columns (local time, slot, month, regime)."""
    fc = forecasts[forecasts["series"] == cfg.series]
    if start is not None and end is not None:
        fc = ev.filter_window(fc, start, end, cfg)
    present = [m for m in MODEL_ORDER if m in set(fc["model"])]
    missing = [m for m in MODEL_ORDER if m not in present]
    fc = fc[fc["model"].isin(present)]
    df = ev.prepare(fc, prices, cfg)  # drops targets whose actual is missing
    tz = tzinfo(cfg)
    local = df["target_time_utc"].dt.tz_convert(tz)
    df["target_local"] = local
    df["issue_local"] = df["issue_time_utc"].dt.tz_convert(tz)
    df["hour_of_day"] = local.dt.hour + local.dt.minute / 60.0
    df["month"] = local.dt.strftime("%Y-%m")
    df["lead_hour"] = np.floor(df["lead_h"]).astype(int)
    df["regime"] = pd.cut(
        df["actual"], bins=REGIME_EDGES, labels=REGIME_LABELS, right=False
    ).astype(str)
    df["model"] = pd.Categorical(df["model"], categories=present, ordered=True)
    return BacktestData(
        frame=df.reset_index(drop=True),
        models=present,
        cfg=cfg,
        tz=tz,
        missing_models=missing,
    )


def load_backtest(
    forecasts_path: Path | str | None = None,
    prices_path: Path | str = DEFAULT_PRICES_PATH,
    cfg: ForecastConfig | None = None,
    window: str = "backtest",
) -> BacktestData:
    """Backtest rows for the four models from the handoff parquet (if present) or the contract parquet."""
    cfg = cfg or load_forecast_config()
    path = (
        Path(forecasts_path)
        if forecasts_path
        else (HANDOFF_PATH if HANDOFF_PATH.exists() else DEFAULT_FORECAST_PATH)
    )
    start, end = window_dates(cfg, window)
    data = from_frames(load_forecasts(path), load_prices(prices_path), cfg, start, end)
    data.source = path
    return data


def describe(data: BacktestData) -> str:
    start, end = window_dates(data.cfg, "backtest")
    issues = data.frame["issue_time_utc"].nunique()
    note = (
        f" Missing from the file and skipped: {', '.join(data.missing_models)}."
        if data.missing_models
        else ""
    )
    return (
        f"{len(data.frame):,} scored rows for {len(data.models)} models over {issues} daily issues "
        f"({start} to {end}, targets with a known actual price).{note}"
    )


# -------------------------------------------------------------- shared helpers


def _mae(g: pd.DataFrame) -> float:
    return float((g["actual"] - g["p50"]).abs().mean())


def _grouped_mae(df: pd.DataFrame, by: str, models: list[str]) -> pd.DataFrame:
    """Rows: groups of ``by``; columns: models; values: MAE."""
    out = (
        df.groupby([by, "model"], observed=True)
        .apply(_mae, include_groups=False)
        .unstack("model")
    )
    return out.reindex(columns=models)


def _legend(ax, models: list[str], **kw) -> None:
    handles = [
        plt.Line2D([], [], color=MODEL_COLORS[m], lw=2.5, label=MODEL_LABELS[m])
        for m in models
    ]
    ax.legend(handles=handles, **kw)


def _label_bars(ax, bars, fmt="{:.1f}", dy=0.5) -> None:
    for b in bars:
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + dy,
            fmt.format(b.get_height()),
            ha="center",
            va="bottom",
            fontsize=10,
            color=INK,
        )


def save(fig: Figure, name: str, directory: Path | str = FIGURES_DIR) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    fig.savefig(path, bbox_inches="tight")
    return path


# -------------------------------------------------------- notebook 01: accuracy


def summary_table(data: BacktestData) -> pd.DataFrame:
    """MAE, RMSE, rMAE vs naive_weekly, pinball loss and P10-P90 coverage per model."""
    scores = ev.score_prepared(data.frame.assign(model=data.frame["model"].astype(str)))
    overall = (
        scores[scores["slice_type"] == "overall"]
        .set_index("model")
        .reindex(data.models)
    )
    table = pd.DataFrame(
        {
            "MAE (£/MWh)": overall["mae"].round(2),
            "RMSE (£/MWh)": overall["rmse"].round(2),
            "rMAE vs naive weekly": overall["rmae"].round(3),
            "Pinball loss": overall["pinball"].round(2),
            "P10–P90 coverage (%)": (100 * overall["coverage"]).round(1),
            "rows": overall["n"].astype(int),
        }
    )
    table.index = [MODEL_LABELS[m] for m in table.index]
    table.index.name = "model"
    return table


def plot_mae_bars(data: BacktestData) -> Figure:
    """Headline MAE per model with values and the improvement relative to naive_daily."""
    mae = (
        data.frame.groupby("model", observed=True)
        .apply(_mae, include_groups=False)
        .reindex(data.models)
    )
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    bars = ax.bar(
        [MODEL_LABELS[m] for m in data.models],
        mae.values,
        color=[MODEL_COLORS[m] for m in data.models],
        width=0.62,
    )
    _label_bars(ax, bars, fmt="{:.2f}")
    if "naive_daily" in data.models:
        ref = mae["naive_daily"]
        for b, m in zip(bars, data.models):
            if m != "naive_daily":
                ax.text(
                    b.get_x() + b.get_width() / 2,
                    b.get_height() / 2,
                    f"{100 * (mae[m] / ref - 1):+.0f}% vs\nnaive daily",
                    ha="center",
                    va="center",
                    fontsize=9.5,
                    color="white",
                )
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("Mean absolute error over the backtest")
    ax.set_ylim(0, mae.max() * 1.15)
    return fig


def plot_block_mae(data: BacktestData) -> Figure:
    """MAE per model for Block A (rest of today) and Block B (tomorrow)."""
    table = _grouped_mae(data.frame, "block", data.models)
    x = np.arange(len(data.models))
    width = 0.38
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, (block, alpha, label) in enumerate(
        [("A", 1.0, "Block A: rest of today"), ("B", 0.5, "Block B: tomorrow")]
    ):
        bars = ax.bar(
            x + (i - 0.5) * width,
            table.loc[block].values,
            width - 0.04,
            color=[MODEL_COLORS[m] for m in data.models],
            alpha=alpha,
            label=label,
        )
        _label_bars(ax, bars, dy=0.3)
    ax.set_xticks(x, [MODEL_LABELS[m] for m in data.models])
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("Same-day versus next-day accuracy")
    ax.legend(
        handles=[
            Patch(facecolor=INK, alpha=1.0, label="Block A: rest of today"),
            Patch(facecolor=INK, alpha=0.4, label="Block B: tomorrow"),
        ],
        loc="upper right",
    )
    ax.set_ylim(0, table.values.max() * 1.18)
    return fig


def plot_mae_by_time_of_day(data: BacktestData) -> Figure:
    """MAE by the target's local half-hour."""
    table = _grouped_mae(data.frame, "hour_of_day", data.models)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for m in data.models:
        ax.plot(table.index, table[m], color=MODEL_COLORS[m], label=MODEL_LABELS[m])
    ax.set_xticks(range(0, 25, 3), [f"{h:02d}:00" for h in range(0, 25, 3)])
    ax.set_xlim(0, 23.5)
    ax.set_xlabel("Target time of day (Europe/London)")
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("Where in the day the errors sit")
    _legend(ax, data.models, loc="upper left", ncol=2)
    ax.set_ylim(0, None)
    return fig


MIN_MONTH_ROWS = 7 * 48  # a month needs at least a week of targets to be shown
MIN_LEAD_BIN_FRACTION = (
    0.25  # a lead bin is shown if it has at least this share of the median bin's rows
)


def mae_by_month(data: BacktestData) -> pd.DataFrame:
    counts = data.of(data.models[0])["month"].value_counts()
    table = _grouped_mae(data.frame, "month", data.models).sort_index()
    return table.loc[[m for m in table.index if counts.get(m, 0) >= MIN_MONTH_ROWS]]


def plot_mae_by_month(data: BacktestData) -> Figure:
    """MAE per calendar month of the target (months with less than a week of targets are dropped)."""
    table = mae_by_month(data)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = np.arange(len(table.index))
    for m in data.models:
        ax.plot(
            x, table[m], marker="o", ms=4, color=MODEL_COLORS[m], label=MODEL_LABELS[m]
        )
    ax.set_xticks(x, [pd.Period(p).strftime("%b %y") for p in table.index], rotation=0)
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("Accuracy month by month")
    _legend(ax, data.models, loc="upper left", ncol=2)
    ax.set_ylim(0, None)
    return fig


def plot_mae_by_price_regime(data: BacktestData) -> Figure:
    """MAE per model when the actual price is negative, ordinary or above 150 £/MWh."""
    table = _grouped_mae(data.frame, "regime", data.models).reindex(REGIME_LABELS)
    counts = (
        data.frame[data.frame["model"] == data.models[0]]["regime"]
        .value_counts()
        .reindex(REGIME_LABELS)
        .fillna(0)
        .astype(int)
    )
    x = np.arange(len(REGIME_LABELS))
    width = 0.8 / len(data.models)
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for i, m in enumerate(data.models):
        bars = ax.bar(
            x + (i - (len(data.models) - 1) / 2) * width,
            table[m].values,
            width - 0.03,
            color=MODEL_COLORS[m],
            label=MODEL_LABELS[m],
        )
        _label_bars(ax, bars, fmt="{:.0f}", dy=0.6)
    ax.set_xticks(
        x,
        [f"{lab} {PRICE_UNIT}\n(n = {counts[lab]:,} targets)" for lab in REGIME_LABELS],
    )
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("Error by actual price regime")
    _legend(ax, data.models, loc="upper right")
    ax.set_ylim(0, np.nanmax(table.values) * 1.15)
    return fig


def calibration_table(data: BacktestData) -> pd.DataFrame:
    """Per model: share of actuals below q10, below q90, and inside [q10, q90] (all in %)."""
    rows = {}
    for m in data.models:
        g = data.of(m)
        if g["q10"].isna().all():
            continue
        rows[m] = {
            "below q10": 100 * (g["actual"] < g["q10"]).mean(),
            "below q90": 100 * (g["actual"] < g["q90"]).mean(),
            "inside P10–P90": 100
            * ((g["actual"] >= g["q10"]) & (g["actual"] <= g["q90"])).mean(),
        }
    return pd.DataFrame(rows).T.reindex([m for m in data.models if m in rows])


def plot_calibration(data: BacktestData) -> Figure:
    """Empirical versus nominal quantile levels (10%, 90%) and interval coverage (80%)."""
    table = calibration_table(data)
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(9.5, 4.2), gridspec_kw={"width_ratios": [2, 1]}
    )
    offsets = np.linspace(-0.18, 0.18, len(table.index))
    for off, m in zip(offsets, table.index):
        ax1.scatter(
            [0 + off, 1 + off],
            [table.loc[m, "below q10"], table.loc[m, "below q90"]],
            color=MODEL_COLORS[m],
            s=70,
            zorder=3,
            label=MODEL_LABELS[m],
        )
        ax2.scatter(
            [off],
            [table.loc[m, "inside P10–P90"]],
            color=MODEL_COLORS[m],
            s=70,
            zorder=3,
        )
    for ax, nominal, xs in ((ax1, (10, 90), (0, 1)), (ax2, (80,), (0,))):
        for xv, yv in zip(xs, nominal):
            ax.hlines(yv, xv - 0.3, xv + 0.3, color=MUTED, lw=1, zorder=2)
    ax1.set_xticks(
        [0, 1],
        [
            "share of actuals below q10\n(nominal 10%)",
            "share of actuals below q90\n(nominal 90%)",
        ],
    )
    ax1.set_ylabel("% of targets")
    ax1.set_ylim(0, 100)
    ax1.set_title("Quantile calibration")
    ax2.set_xticks([0], ["inside P10–P90\n(nominal 80%)"])
    ax2.set_xlim(-0.5, 0.5)
    ax2.set_ylim(50, 100)
    ax2.set_title("Interval coverage")
    _legend(ax1, list(table.index), loc="center left")
    return fig


def dm_matrix(data: BacktestData) -> pd.DataFrame:
    """Pairwise one-sided Diebold-Mariano p-values: row model better than column model."""
    frame = data.frame.assign(model=data.frame["model"].astype(str))
    mat = pd.DataFrame(np.nan, index=data.models, columns=data.models)
    for row in data.models:
        for col in data.models:
            if row != col:
                mat.loc[row, col] = ev.diebold_mariano(frame, row, col)["p_value"]
    return mat


def plot_dm_matrix(data: BacktestData) -> Figure:
    """Heatmap of pairwise DM p-values with significant cells (p < 0.05) outlined."""
    mat = dm_matrix(data)
    fig, ax = plt.subplots(figsize=(6.6, 5.2))
    values = mat.to_numpy(dtype="float64")
    shade = np.where(
        np.isnan(values), np.nan, 1 - np.clip(values, 0, 1)
    )  # darker = smaller p
    ax.imshow(shade, cmap="Blues", vmin=0, vmax=1.3)
    labels = [MODEL_LABELS[m] for m in data.models]
    ax.set_xticks(range(len(labels)), labels, rotation=20, ha="right")
    ax.set_yticks(range(len(labels)), labels)
    ax.grid(False)
    for i in range(len(labels)):
        for j in range(len(labels)):
            if i == j:
                ax.text(j, i, "—", ha="center", va="center", color=MUTED)
                continue
            p = values[i, j]
            txt = "p < 0.001" if p < 1e-3 else f"p = {p:.3f}"
            ax.text(
                j,
                i,
                txt,
                ha="center",
                va="center",
                fontsize=10,
                color="white" if p < 0.5 else INK,
            )
            if p < 0.05:
                ax.add_patch(
                    plt.Rectangle(
                        (j - 0.5, i - 0.5), 1, 1, fill=False, edgecolor=INK, lw=2
                    )
                )
    ax.set_xlabel("... than this model (benchmark)")
    ax.set_ylabel("This model is better ...")
    ax.set_title("Diebold-Mariano, one-sided (outlined: p < 0.05)")
    return fig


# ------------------------------------------------------ notebook 02: horizons


def per_issue_mae(data: BacktestData, model: str = "lear_x") -> pd.Series:
    g = data.of(model)
    return g.groupby("issue_time_utc").apply(_mae, include_groups=False)


def _complete_issues(data: BacktestData, model: str) -> pd.Series:
    """Issues whose every target has an actual (target count per issue == max observed)."""
    counts = data.of(model).groupby("issue_time_utc").size()
    return counts[counts >= 74]


def typical_issue(data: BacktestData, model: str = "lear_x") -> pd.Timestamp:
    """The complete issue whose MAE is closest to the model's median per-issue MAE."""
    mae = per_issue_mae(data, model).loc[_complete_issues(data, model).index]
    return (mae - mae.median()).abs().idxmin()


def contrasting_issues(
    data: BacktestData, model: str = "lear_x", fixed_date: date = date(2026, 3, 28)
) -> dict[str, pd.Timestamp]:
    """Calm day, negative-price day, highest-peak day and the spring clock-change issue."""
    g = data.of(model)
    g = g[g["issue_time_utc"].isin(_complete_issues(data, model).index)]
    stats = g.groupby("issue_time_utc")["actual"].agg(["std", "min", "max"])
    fixed = g[g["issue_local"].dt.date == fixed_date]["issue_time_utc"]
    return {
        "Calm day (lowest price variance)": stats["std"].idxmin(),
        "Negative prices (lowest actual)": stats["min"].idxmin(),
        "Highest peak price": stats["max"].idxmax(),
        f"{fixed_date:%d %b %Y}: next day is the spring clock change": (
            fixed.iloc[0] if len(fixed) else stats.index[-1]
        ),
    }


def plot_issue(
    data: BacktestData,
    issue: pd.Timestamp,
    ax=None,
    title: str | None = None,
    band_model: str = "lear_x",
    legend: bool = True,
) -> Figure:
    """Actual price and every model's p50 over one issue's targets, with the band and the blocks."""
    rows = data.frame[data.frame["issue_time_utc"] == issue]
    if rows.empty:
        raise ValueError(f"no rows for issue {issue}")
    own_fig = ax is None
    if own_fig:
        fig, ax = plt.subplots(figsize=(11, 4.6))
    else:
        fig = ax.figure
    first = rows[rows["model"] == data.models[0]].sort_values("target_time_utc")
    t = first["target_local"]
    ax.plot(t, first["actual"], color=ACTUAL, lw=2.4, label="actual", zorder=4)
    for m in data.models:
        g = rows[rows["model"] == m].sort_values("target_time_utc")
        ax.plot(
            g["target_local"],
            g["p50"],
            color=MODEL_COLORS[m],
            lw=1.8,
            label=MODEL_LABELS[m],
            zorder=3,
        )
        if m == band_model and g["q10"].notna().any():
            ax.fill_between(
                g["target_local"],
                g["q10"],
                g["q90"],
                color=MODEL_COLORS[m],
                alpha=0.18,
                lw=0,
                label=f"{MODEL_LABELS[m]} P10–P90",
                zorder=2,
            )
    issue_local = first["issue_local"].iloc[0]
    midnight = (issue_local + pd.Timedelta(days=1)).normalize()
    end = t.max() + pd.Timedelta(minutes=30)
    ax.axvspan(issue_local, midnight, color="#F3F4F6", zorder=0)
    ax.axvspan(midnight, end, color="#FAFAFA", zorder=0)
    ax.axvline(issue_local, color=MUTED, lw=1, ls="--", zorder=1)
    ymax = ax.get_ylim()[1]
    ax.text(
        issue_local + (midnight - issue_local) / 2,
        ymax,
        "Block A (rest of today)",
        ha="center",
        va="top",
        fontsize=9.5,
        color=MUTED,
    )
    ax.text(
        midnight + (end - midnight) / 2,
        ymax,
        "Block B (tomorrow)",
        ha="center",
        va="top",
        fontsize=9.5,
        color=MUTED,
    )
    ax.text(
        issue_local,
        ax.get_ylim()[0],
        " issue 10:00",
        ha="left",
        va="bottom",
        fontsize=9,
        color=MUTED,
    )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=data.tz))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6), tz=data.tz))
    ax.set_xlim(t.min() - pd.Timedelta(minutes=30), end)
    ax.set_ylabel(f"price ({PRICE_UNIT})")
    ax.set_title(title or f"Forecast issued {issue_local:%A %d %B %Y} at 10:00")
    if legend:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=6, fontsize=9.5)
    if not own_fig:
        ax.tick_params(axis="x", labelsize=9)
    return fig


def plot_issue_grid(data: BacktestData, issues: dict[str, pd.Timestamp]) -> Figure:
    """2x2 small multiples of :func:`plot_issue` for contrasting issues."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.5))
    for ax, (label, issue) in zip(axes.ravel(), issues.items()):
        issue_local = pd.Timestamp(issue).tz_convert(data.tz)
        plot_issue(
            data,
            issue,
            ax=ax,
            title=f"{label}\nissued {issue_local:%a %d %b %Y}",
            legend=False,
        )
    handles = [plt.Line2D([], [], color=ACTUAL, lw=2.4, label="actual")] + [
        plt.Line2D([], [], color=MODEL_COLORS[m], lw=1.8, label=MODEL_LABELS[m])
        for m in data.models
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    return fig


def _full_lead_bins(data: BacktestData) -> list[int]:
    """Lead-hour bins with at least a quarter of the typical bin's rows (drops the 38 h bin of one 78-period issue)."""
    counts = data.of(data.models[0])["lead_hour"].value_counts()
    return sorted(
        int(h)
        for h, n in counts.items()
        if n >= MIN_LEAD_BIN_FRACTION * counts.median()
    )


def mae_by_lead(data: BacktestData) -> pd.DataFrame:
    """MAE per whole hour of lead time (bins with too few rows dropped)."""
    return _grouped_mae(data.frame, "lead_hour", data.models).reindex(
        _full_lead_bins(data)
    )


def plot_mae_by_lead(data: BacktestData) -> Figure:
    """MAE as a function of lead time in hours, with the Block A/B boundary marked."""
    table = mae_by_lead(data)
    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    for m in data.models:
        ax.plot(
            table.index + 0.5, table[m], color=MODEL_COLORS[m], label=MODEL_LABELS[m]
        )
    ax.axvline(BLOCK_BOUNDARY_H, color=MUTED, lw=1, ls="--")
    ax.text(
        BLOCK_BOUNDARY_H + 0.3,
        ax.get_ylim()[1] * 0.97,
        "midnight: Block A → Block B",
        va="top",
        fontsize=9.5,
        color=MUTED,
    )
    ax.set_xlabel("Lead time from the 10:00 issue (hours)")
    ax.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax.set_title("How accuracy decays with lead time")
    ax.set_xlim(0, 39)
    ax.set_ylim(0, None)
    _legend(ax, data.models, loc="lower right", ncol=2)
    return fig


def width_by_lead(data: BacktestData, model: str = "lear_x") -> pd.DataFrame:
    """Mean P10-P90 width and empirical coverage per whole hour of lead time."""
    g = data.of(model)
    g = g[g["lead_hour"].isin(_full_lead_bins(data))]
    out = g.groupby("lead_hour").apply(
        lambda x: pd.Series(
            {
                "width": float((x["q90"] - x["q10"]).mean()),
                "coverage": float(
                    ((x["actual"] >= x["q10"]) & (x["actual"] <= x["q90"])).mean() * 100
                ),
            }
        ),
        include_groups=False,
    )
    return out.sort_index()


def plot_width_by_lead(data: BacktestData, model: str = "lear_x") -> Figure:
    """Mean P10-P90 width and empirical coverage versus lead time (two panels, one axis each)."""
    table = width_by_lead(data, model)
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9.5, 6.2), sharex=True, gridspec_kw={"height_ratios": [2, 1.2]}
    )
    ax1.plot(table.index + 0.5, table["width"], color=MODEL_COLORS[model])
    ax1.set_ylabel(f"mean P10–P90 width ({PRICE_UNIT})")
    ax1.set_title(f"{MODEL_LABELS[model]}: uncertainty band versus lead time")
    ax1.set_ylim(0, None)
    ax2.plot(table.index + 0.5, table["coverage"], color=MODEL_COLORS[model])
    ax2.axhline(80, color=MUTED, lw=1, ls="--")
    ax2.text(
        38.8, 80.8, "nominal 80%", ha="right", va="bottom", fontsize=9, color=MUTED
    )
    ax2.set_ylabel("coverage (%)")
    ax2.set_ylim(50, 100)
    ax2.set_xlabel("Lead time from the 10:00 issue (hours)")
    for ax in (ax1, ax2):
        ax.axvline(BLOCK_BOUNDARY_H, color=MUTED, lw=1, ls="--")
    ax1.set_xlim(0, 39)
    fig.tight_layout()
    return fig


def plot_pred_vs_actual(
    data: BacktestData,
    models: tuple[str, str] = ("naive_daily", "lear_x"),
    leads: tuple[tuple[float, float], ...] = ((0, 12), (24, 38.5)),
) -> Figure:
    """Hexbin of p50 against actual for two models (rows) and two lead ranges (columns)."""
    models = [m for m in models if m in data.models]
    lo, hi = data.frame["actual"].quantile([0.005, 0.995]).tolist()
    lo = min(lo, -20.0)
    fig, axes = plt.subplots(
        len(models),
        len(leads),
        figsize=(5.2 * len(leads), 4.6 * len(models)),
        sharex=True,
        sharey=True,
    )
    axes = np.atleast_2d(axes)
    for i, m in enumerate(models):
        for j, (a, b) in enumerate(leads):
            ax = axes[i, j]
            g = data.of(m)
            g = g[(g["lead_h"] >= a) & (g["lead_h"] <= b)]
            ax.hexbin(
                g["actual"],
                g["p50"],
                gridsize=45,
                cmap="Blues",
                mincnt=1,
                extent=(lo, hi, lo, hi),
                linewidths=0.2,
            )
            ax.plot([lo, hi], [lo, hi], color=MUTED, lw=1, ls="--")
            ax.text(
                0.03,
                0.95,
                f"MAE {_mae(g):.1f} {PRICE_UNIT}\nn = {len(g):,}",
                transform=ax.transAxes,
                va="top",
                fontsize=10,
                color=INK,
            )
            ax.set_title(
                f"{MODEL_LABELS[m]}, lead {a:g}–{b:g} h", color=MODEL_COLORS[m]
            )
            ax.grid(False)
            ax.set_xlim(lo, hi)
            ax.set_ylim(lo, hi)
            if i == len(models) - 1:
                ax.set_xlabel(f"actual price ({PRICE_UNIT})")
            if j == 0:
                ax.set_ylabel(f"forecast p50 ({PRICE_UNIT})")
    fig.suptitle("Forecast against actual (darker = more targets)", y=1.0)
    fig.tight_layout()
    return fig


def next_local_day(data: BacktestData, issue: pd.Timestamp) -> date:
    """The local calendar day after an issue (its Block B day)."""
    return (pd.Timestamp(issue).tz_convert(data.tz) + pd.Timedelta(days=1)).date()


def convergence_pair(
    data: BacktestData, target_day: date, model: str = "lear_x"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rows for target_day from the issue the day before and from the same-day issue."""
    g = data.of(model)
    on_day = g[g["target_local"].dt.date == target_day]
    earlier = on_day[
        on_day["issue_local"].dt.date == target_day - timedelta(days=1)
    ].sort_values("target_time_utc")
    later = on_day[on_day["issue_local"].dt.date == target_day].sort_values(
        "target_time_utc"
    )
    return earlier, later


def convergence_summary(data: BacktestData, model: str = "lear_x") -> pd.Series:
    """MAE over all targets covered by both the day-before issue and the same-day issue."""
    g = data.of(model)
    both = g[
        g["target_local"].dt.hour >= 10
    ].copy()  # periods from 10:00 local are in both issues
    both["which"] = np.where(
        both["block"] == "B",
        "issued the day before (Block B)",
        "issued the same day (Block A)",
    )
    keys = ["target_time_utc"]
    covered = both.groupby(keys)["which"].nunique()
    both = both[both["target_time_utc"].isin(covered[covered == 2].index)]
    return (
        both.groupby("which")
        .apply(_mae, include_groups=False)
        .reindex(["issued the day before (Block B)", "issued the same day (Block A)"])
    )


def plot_convergence(
    data: BacktestData, target_day: date, model: str = "lear_x"
) -> Figure:
    """One target day seen from two issues, plus the backtest-wide MAE of the two views."""
    earlier, later = convergence_pair(data, target_day, model)
    summary = convergence_summary(data, model)
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(13, 4.6), gridspec_kw={"width_ratios": [3, 1.1]}
    )
    color = MODEL_COLORS[model]
    ax1.plot(
        earlier["target_local"],
        earlier["actual"],
        color=ACTUAL,
        lw=2.4,
        label="actual",
        zorder=4,
    )
    ax1.plot(
        earlier["target_local"],
        earlier["p50"],
        color=color,
        lw=1.8,
        ls="--",
        label="forecast issued the day before (lead 14–38 h)",
        zorder=3,
    )
    ax1.plot(
        later["target_local"],
        later["p50"],
        color=color,
        lw=2.2,
        label="forecast issued the same day at 10:00 (lead 0–14 h)",
        zorder=3,
    )
    ax1.axvline(
        later["issue_local"].iloc[0] if len(later) else earlier["target_local"].iloc[0],
        color=MUTED,
        lw=1,
        ls="--",
    )
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=data.tz))
    ax1.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=data.tz))
    ax1.set_ylabel(f"price ({PRICE_UNIT})")
    ax1.set_title(
        f"{MODEL_LABELS[model]} forecasts for {target_day:%A %d %B %Y} from two issues"
    )
    ax1.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, fontsize=9.5)
    bars = ax2.bar(
        ["day before\n(Block B)", "same day\n(Block A)"],
        summary.values,
        color=[color, color],
        alpha=0.9,
        width=0.6,
    )
    bars[0].set_alpha(0.5)
    _label_bars(ax2, bars, fmt="{:.2f}", dy=0.2)
    ax2.set_ylabel(f"MAE ({PRICE_UNIT})")
    ax2.set_title("Same targets, whole backtest")
    ax2.set_ylim(0, summary.max() * 1.2)
    fig.tight_layout()
    return fig


__all__ = [name for name in dir() if not name.startswith("_")]
matplotlib.rcParams["figure.max_open_warning"] = 0
