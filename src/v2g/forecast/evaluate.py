"""Score any model in the forecast contract parquet against actual prices.

Run
---
::

    python -m v2g.forecast.evaluate [--window smoke|backtest] [--start --end] \\
        [--models a,b] [--show-issue 2025-11-10]

Metrics (targets whose actual price is missing are excluded):

* MAE, RMSE (£/MWh)
* rMAE: MAE divided by ``naive_weekly``'s MAE on the same (series, issue,
  target) rows, so 1.0 means "as good as weekly naive"
* pinball loss averaged over q10, q50 (=p50) and q90
* coverage: share of actuals inside [q10, q90] (target about 80 %)

reported overall, by lead-time bucket and by block (A = same local day as the
issue, B = next local day). The output CSV is long: one row per
(model, slice_type, slice). Model-agnostic: LEAR or LightGBM rows in the same
parquet are scored the same way.

Diebold-Mariano (multivariate version of Lago et al. 2021): for every
non-benchmark model and each naive benchmark present, the loss differential
per issue is the mean over that issue's common targets of |e_benchmark| -
|e_model|. The statistic is mean(d) / (sd(d) / sqrt(N)) with the sample
standard deviation over issues, and the one-sided p-value tests the
alternative "the model is better than the benchmark" (positive statistic,
small p-value). Written next to the eval CSV as ``<name>_dm.csv``.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from shared.schema import ForecastConfig
from v2g.forecast.config import (
    DEFAULT_ASSUMPTIONS_PATH,
    WINDOWS,
    load_forecast_config,
    window_dates,
)
from v2g.forecast.io import (
    DEFAULT_FORECAST_PATH,
    DEFAULT_PRICES_PATH,
    load_forecasts,
    load_prices,
    lookup_prices,
)
from v2g.forecast.timing import (
    lead_bucket,
    lead_time_hours,
    target_block,
    to_date,
    tzinfo,
)

log = logging.getLogger(__name__)

DEFAULT_EVAL_PATH = Path("artefacts/forecasts/eval_naive.csv")
REFERENCE_MODEL = "naive_weekly"
QUANTILE_LEVELS = {"q10": 0.1, "p50": 0.5, "q90": 0.9}
METRIC_COLUMNS = [
    "model",
    "slice_type",
    "slice",
    "n",
    "mae",
    "rmse",
    "rmae",
    "pinball",
    "coverage",
]

DM_BENCHMARKS = ("naive_daily", "naive_weekly")
DM_COLUMNS = ["model", "benchmark", "n_issues", "mean_loss_diff", "dm_stat", "p_value"]


def pinball_loss(actual: np.ndarray, forecast: np.ndarray, q: float) -> float:
    diff = actual - forecast
    return float(np.mean(np.maximum(q * diff, (q - 1.0) * diff)))


def filter_window(
    forecasts: pd.DataFrame, start_date, end_date, cfg: ForecastConfig
) -> pd.DataFrame:
    """Keep issues whose local date is within [start_date, end_date]."""
    local_dates = forecasts["issue_time_utc"].dt.tz_convert(tzinfo(cfg)).dt.date
    keep = (local_dates >= to_date(start_date)) & (local_dates <= to_date(end_date))
    return forecasts[keep].reset_index(drop=True)


def prepare(
    forecasts: pd.DataFrame, prices: pd.Series, cfg: ForecastConfig
) -> pd.DataFrame:
    """Attach actuals, lead bucket, block and the reference model's p50; drop unknown actuals."""
    df = forecasts.copy()
    df["actual"] = lookup_prices(prices, df["target_time_utc"])
    n_missing = int(df["actual"].isna().sum())
    if n_missing:
        log.info(
            "Excluding %d rows whose actual price is missing or outside the actuals file",
            n_missing,
        )
    df = df[df["actual"].notna()].reset_index(drop=True)
    if df.empty:
        raise ValueError("no forecast rows with a known actual price")
    df["lead_h"] = lead_time_hours(df["issue_time_utc"], df["target_time_utc"])
    df["lead_bucket"] = lead_bucket(df["lead_h"], cfg)
    df["block"] = target_block(df["issue_time_utc"], df["target_time_utc"], cfg)
    reference = df.loc[
        df["model"] == REFERENCE_MODEL,
        ["series", "issue_time_utc", "target_time_utc", "p50"],
    ]
    reference = reference.rename(columns={"p50": "reference_p50"})
    return df.merge(
        reference, on=["series", "issue_time_utc", "target_time_utc"], how="left"
    )


def _metrics(
    group: pd.DataFrame, model: str, slice_type: str, slice_value: str
) -> dict:
    actual = group["actual"].to_numpy(dtype="float64")
    p50 = group["p50"].to_numpy(dtype="float64")
    err = actual - p50
    row = {
        "model": model,
        "slice_type": slice_type,
        "slice": slice_value,
        "n": len(group),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "rmae": np.nan,
        "pinball": np.nan,
        "coverage": np.nan,
    }
    has_ref = group["reference_p50"].notna().to_numpy()
    if has_ref.any():
        ref_mae = np.mean(
            np.abs(
                actual[has_ref]
                - group["reference_p50"].to_numpy(dtype="float64")[has_ref]
            )
        )
        own_mae = np.mean(np.abs(err[has_ref]))
        row["rmae"] = float(own_mae / ref_mae) if ref_mae > 0 else np.nan
    if {"q10", "q90"} <= set(group.columns):
        q10 = group["q10"].to_numpy(dtype="float64")
        q90 = group["q90"].to_numpy(dtype="float64")
        has_q = ~np.isnan(q10) & ~np.isnan(q90)
        if has_q.any():
            a, lo, mid, hi = actual[has_q], q10[has_q], p50[has_q], q90[has_q]
            losses = [
                pinball_loss(a, f, q) for f, q in ((lo, 0.1), (mid, 0.5), (hi, 0.9))
            ]
            row["pinball"] = float(np.mean(losses))
            row["coverage"] = float(np.mean((lo <= a) & (a <= hi)))
    return row


def score(
    forecasts: pd.DataFrame, prices: pd.Series, cfg: ForecastConfig
) -> pd.DataFrame:
    """Long table of metrics per model: overall, per lead bucket, per block."""
    return score_prepared(prepare(forecasts, prices, cfg))


def score_prepared(df: pd.DataFrame) -> pd.DataFrame:
    """As :func:`score`, on the output of :func:`prepare`."""
    rows = []
    for model, group in df.groupby("model", sort=True):
        rows.append(_metrics(group, model, "overall", "all"))
        for block, sub in group.groupby("block", sort=True):
            rows.append(_metrics(sub, model, "block", str(block)))
        for bucket, sub in group.groupby("lead_bucket", sort=False):
            rows.append(_metrics(sub, model, "lead_bucket", str(bucket)))
    out = pd.DataFrame(rows, columns=METRIC_COLUMNS)
    order = {"overall": 0, "block": 1, "lead_bucket": 2}
    out["_o"] = out["slice_type"].map(order)
    out["_b"] = (
        out["slice"]
        .map({b: i for i, b in enumerate(df["lead_bucket"].unique())})
        .fillna(-1)
    )
    return (
        out.sort_values(["model", "_o", "_b", "slice"])
        .drop(columns=["_o", "_b"])
        .reset_index(drop=True)
    )


def diebold_mariano(prepared: pd.DataFrame, model: str, benchmark: str) -> dict:
    """One-sided multivariate DM test that ``model`` beats ``benchmark`` (see module docstring)."""
    keys = ["series", "issue_time_utc", "target_time_utc"]
    a = prepared.loc[prepared["model"] == model, [*keys, "actual", "p50"]]
    b = prepared.loc[prepared["model"] == benchmark, [*keys, "p50"]].rename(
        columns={"p50": "p50_benchmark"}
    )
    both = a.merge(b, on=keys, how="inner")
    if both.empty:
        raise ValueError(f"no common rows between {model} and {benchmark}")
    both["d"] = (both["actual"] - both["p50_benchmark"]).abs() - (
        both["actual"] - both["p50"]
    ).abs()
    per_issue = both.groupby("issue_time_utc")["d"].mean()
    n = len(per_issue)
    mean = float(per_issue.mean())
    sd = float(per_issue.std(ddof=1)) if n > 1 else float("nan")
    stat = mean / (sd / np.sqrt(n)) if sd and sd > 0 else float("nan")
    p_value = float(1.0 - norm.cdf(stat)) if np.isfinite(stat) else float("nan")
    return {
        "model": model,
        "benchmark": benchmark,
        "n_issues": n,
        "mean_loss_diff": mean,
        "dm_stat": stat,
        "p_value": p_value,
    }


def dm_table(
    prepared: pd.DataFrame, benchmarks: tuple[str, ...] = DM_BENCHMARKS
) -> pd.DataFrame:
    """DM results for every non-benchmark model against each benchmark present."""
    present = set(prepared["model"].unique())
    rows = [
        diebold_mariano(prepared, model, benchmark)
        for model in sorted(present - set(benchmarks))
        for benchmark in benchmarks
        if benchmark in present
    ]
    return pd.DataFrame(rows, columns=DM_COLUMNS)


def format_dm_table(dm: pd.DataFrame) -> str:
    if dm.empty:
        return "(no model to compare against the naive benchmarks)"
    show = dm.copy()
    show["mean_loss_diff"] = show["mean_loss_diff"].map(lambda v: f"{v:+.2f} £/MWh")
    show["dm_stat"] = show["dm_stat"].map(lambda v: f"{v:.2f}")
    show["p_value"] = show["p_value"].map(
        lambda v: f"{v:.4f}" if v >= 1e-4 else f"{v:.1e}"
    )
    return show.to_string(index=False)


def dm_output_path(out_path: Path | str) -> Path:
    out_path = Path(out_path)
    return out_path.with_name(out_path.stem + "_dm" + out_path.suffix)


def format_table(scores: pd.DataFrame) -> str:
    show = scores.copy()
    show["slice"] = (
        show["slice_type"]
        .str.replace("lead_bucket", "lead")
        .str.cat(show["slice"], sep=" ")
    )
    show = show.drop(columns="slice_type")
    show["coverage"] = show["coverage"].map(
        lambda v: f"{100 * v:.1f}%" if pd.notna(v) else ""
    )
    for col in ("mae", "rmse", "pinball"):
        show[col] = show[col].map(lambda v: f"{v:.2f}" if pd.notna(v) else "")
    show["rmae"] = show["rmae"].map(lambda v: f"{v:.3f}" if pd.notna(v) else "")
    return show.to_string(index=False)


def show_issue(
    forecasts: pd.DataFrame, prices: pd.Series, issue_date, cfg: ForecastConfig
) -> pd.DataFrame:
    """Wide spot-check table for one issue: local target time, block, actual, each model's p50/q10/q90."""
    sub = filter_window(forecasts, issue_date, issue_date, cfg)
    if sub.empty:
        raise ValueError(f"no forecasts issued on {issue_date}")
    sub = sub.copy()
    sub["actual"] = lookup_prices(prices, sub["target_time_utc"])
    sub["block"] = target_block(sub["issue_time_utc"], sub["target_time_utc"], cfg)
    sub["target_local"] = (
        sub["target_time_utc"].dt.tz_convert(tzinfo(cfg)).dt.strftime("%a %d %H:%M")
    )
    values = [c for c in ("p50", "q10", "q90") if c in sub.columns]
    wide = sub.pivot_table(
        index=["target_time_utc", "target_local", "block", "actual"],
        columns="model",
        values=values,
    )
    wide.columns = [f"{model}.{value}" for value, model in wide.columns]
    wide = (
        wide.reset_index()
        .sort_values("target_time_utc")
        .drop(columns="target_time_utc")
    )
    return wide[
        [
            "target_local",
            "block",
            "actual",
            *sorted(wide.columns.difference(["target_local", "block", "actual"])),
        ]
    ]


def run(
    start_date,
    end_date,
    cfg: ForecastConfig,
    *,
    forecasts_path: Path | str = DEFAULT_FORECAST_PATH,
    prices_path: Path | str = DEFAULT_PRICES_PATH,
    out_path: Path | str | None = DEFAULT_EVAL_PATH,
    models: list[str] | None = None,
) -> pd.DataFrame:
    forecasts = load_forecasts(forecasts_path, model=models)
    forecasts = filter_window(forecasts, start_date, end_date, cfg)
    if forecasts.empty:
        raise ValueError(
            f"no forecasts issued between {start_date} and {end_date} in {forecasts_path}"
        )
    prices = load_prices(prices_path)
    prepared = prepare(forecasts, prices, cfg)
    scores = score_prepared(prepared)
    dm = dm_table(prepared)
    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        scores.to_csv(out_path, index=False)
        log.info("Wrote %s", out_path)
        if not dm.empty:
            dm.to_csv(dm_output_path(out_path), index=False)
            log.info("Wrote %s", dm_output_path(out_path))
    scores.attrs["dm"] = dm
    return scores


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m v2g.forecast.evaluate",
        description="Score forecast models against actuals.",
    )
    p.add_argument("--window", choices=WINDOWS, default="smoke")
    p.add_argument("--start", help="first local issue date (overrides the window)")
    p.add_argument(
        "--end", help="last local issue date, inclusive (overrides the window)"
    )
    p.add_argument(
        "--models", help="comma-separated models to score (default: all in the file)"
    )
    p.add_argument("--forecasts", default=str(DEFAULT_FORECAST_PATH))
    p.add_argument("--prices", default=str(DEFAULT_PRICES_PATH))
    p.add_argument("--out", default=str(DEFAULT_EVAL_PATH), help="CSV to write")
    p.add_argument(
        "--show-issue",
        help="also print one issue's rows next to the actuals (local date)",
    )
    p.add_argument("--config", default=str(DEFAULT_ASSUMPTIONS_PATH))
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = load_forecast_config(args.config)
    start, end = window_dates(cfg, args.window)
    start = to_date(args.start) if args.start else start
    end = to_date(args.end) if args.end else end
    models = [m.strip() for m in args.models.split(",")] if args.models else None
    scores = run(
        start,
        end,
        cfg,
        forecasts_path=args.forecasts,
        prices_path=args.prices,
        out_path=args.out,
        models=models,
    )
    print(
        f"Evaluation of issues {start}..{end} (£/MWh; rMAE vs {REFERENCE_MODEL}; coverage target 80%)"
    )
    print(format_table(scores))
    print(f"written to {args.out}")
    dm = scores.attrs.get("dm")
    if dm is not None and not dm.empty:
        print(
            "\nDiebold-Mariano (one-sided, H1: model beats benchmark; d = |e_benchmark| - |e_model| per issue):"
        )
        print(format_dm_table(dm))
        print(f"written to {dm_output_path(args.out)}")
    if args.show_issue:
        table = show_issue(
            load_forecasts(args.forecasts, model=models),
            load_prices(args.prices),
            args.show_issue,
            cfg,
        )
        print(f"\nIssue {args.show_issue} ({len(table)} targets):")
        print(table.to_string(index=False, float_format=lambda v: f"{v:.2f}"))


if __name__ == "__main__":
    main()
