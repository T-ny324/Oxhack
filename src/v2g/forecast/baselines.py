"""Naive price-forecast baselines (stage S3, ladder step 1) and the shared
point-forecast -> quantile plumbing later models reuse.

Run
---
::

    python -m v2g.forecast.baselines --window smoke|backtest [--start --end] \\
        [--models naive_weekly,naive_daily]
    make forecast-naive        # backtest window
    make forecast-naive-smoke  # smoke window, separate output files

Output follows the S3 -> S5 contract (:mod:`v2g.forecast.io`).

Models
------
Both work in LOCAL wall-clock time (Europe/London) for "the same time on another
day", never UTC minus 24 h, which breaks across the clock change. The local
reference time is converted back to UTC with the DST rules in
:func:`v2g.forecast.timing.local_to_utc`: a nonexistent local time (spring
forward) uses the period one hour earlier, an ambiguous one (autumn back) uses
the first occurrence.

``naive_weekly``
    p50(target) = price at the same local time 7 days before the target's
    local date.

``naive_daily`` (the "Naive" benchmark of Lago et al. 2021)
    Reference date = target's local date - 1 if the target's local weekday is
    Tue-Fri, else (Sat, Sun, Mon) target's local date - 7. If that reference
    falls after the known cutoff (tomorrow's targets pointing at today's
    afternoon), fall back to reference date - 1, i.e. the same local time
    yesterday. Only after that does the 7-day step-back below apply.

Price lookup rule (shared)
    A reference price is usable only if its period START is <= the issue's
    known cutoff (issue time minus ``price_known_lag_periods`` half-hours) and
    it is not missing in the actuals. Otherwise step back 7 days at a time at
    the same local time, up to 4 weeks. If still nothing, raise: a NaN p50 is
    never emitted. Each row keeps ``ref_time_utc`` internally so leakage can be
    tested; it is dropped before writing.

Quantiles (empirical residuals by lead-time bucket)
    For each issue, take the model's own past errors (actual - p50) from issues
    in the previous ``quantile_history_days``, using only targets whose actual
    was known at this issue's cutoff. Within each lead bucket
    q10 = p50 + P10(errors), q90 = p50 + P90(errors); buckets with fewer than
    ``MIN_BUCKET_ERRORS`` errors pool all buckets. q10 <= p50 <= q90 is
    enforced. Point forecasts are generated from
    (window start - quantile_history_days - 7 days) as warm-up and only issues
    inside the requested window are written.
"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast.config import (
    DEFAULT_ASSUMPTIONS_PATH,
    WINDOWS,
    load_forecast_config,
    window_dates,
)
from v2g.forecast.io import (
    CONTRACT_COLUMNS,
    DEFAULT_FORECAST_PATH,
    DEFAULT_PRICES_PATH,
    load_prices,
    lookup_prices,
    write_forecasts,
)
from v2g.forecast.timing import (
    UTC_DTYPE,
    issue_times,
    known_cutoff,
    lead_bucket,
    lead_time_hours,
    local_to_utc,
    target_times,
    to_date,
    tzinfo,
)

log = logging.getLogger(__name__)

MODELS = ("naive_weekly", "naive_daily")
MAX_WEEKLY_STEPS = 4  # fall back up to 4 weeks, 7 days at a time
TUE_TO_FRI = (1, 2, 3, 4)  # Monday == 0
MIN_BUCKET_ERRORS = 10
POINT_COLUMNS = [
    "model",
    "series",
    "issue_time_utc",
    "target_time_utc",
    "p50",
    "ref_time_utc",
]


# ---------------------------------------------------------------- point forecasts


def build_issue_target_frame(
    issues: pd.DatetimeIndex, cfg: ForecastConfig
) -> pd.DataFrame:
    """One row per (issue, target) for every issue, in issue then target order."""
    if len(issues) == 0:
        raise ValueError("no issues given")
    parts = [
        pd.DataFrame(
            {"issue_time_utc": issue, "target_time_utc": target_times(issue, cfg)}
        )
        for issue in issues
    ]
    frame = pd.concat(parts, ignore_index=True)
    for col in ("issue_time_utc", "target_time_utc"):
        frame[col] = frame[col].astype(UTC_DTYPE)
    return frame


def _naive_to_utc(local_naive: pd.Series, cfg: ForecastConfig) -> pd.Series:
    return pd.Series(local_to_utc(local_naive.to_numpy(), cfg), index=local_naive.index)


def _initial_reference(model: str, target_local_naive: pd.Series) -> pd.Series:
    """Model-specific first reference wall-clock time for each target."""
    if model == "naive_weekly":
        return target_local_naive - pd.Timedelta(days=7)
    if model == "naive_daily":
        back_days = np.where(target_local_naive.dt.day_of_week.isin(TUE_TO_FRI), 1, 7)
        return target_local_naive - pd.to_timedelta(back_days, unit="D")
    raise ValueError(f"unknown model {model!r}; choose from {MODELS}")


def forecast_points(
    model: str, issues: pd.DatetimeIndex, prices: pd.Series, cfg: ForecastConfig
) -> pd.DataFrame:
    """Point forecasts (p50) for every issue in ``issues`` with their reference times."""
    frame = build_issue_target_frame(issues, cfg)
    cutoff = known_cutoff(frame["issue_time_utc"], cfg)
    target_local = (
        frame["target_time_utc"].dt.tz_convert(tzinfo(cfg)).dt.tz_localize(None)
    )

    ref_local = _initial_reference(model, target_local)
    ref_utc = _naive_to_utc(ref_local, cfg)
    if model == "naive_daily":
        late = ref_utc > cutoff  # e.g. tomorrow's 15:00 pointing at today's 15:00
        ref_local = ref_local.mask(late, ref_local - pd.Timedelta(days=1))
        ref_utc = _naive_to_utc(ref_local, cfg)

    p50 = pd.Series(lookup_prices(prices, ref_utc), index=frame.index)
    usable = (ref_utc <= cutoff) & p50.notna()
    steps = 0
    while not usable.all() and steps < MAX_WEEKLY_STEPS:
        steps += 1
        ref_local = ref_local.mask(~usable, ref_local - pd.Timedelta(days=7))
        ref_utc = _naive_to_utc(ref_local, cfg)
        p50 = pd.Series(lookup_prices(prices, ref_utc), index=frame.index)
        usable = (ref_utc <= cutoff) & p50.notna()
    if not usable.all():
        examples = (
            frame.loc[~usable, ["issue_time_utc", "target_time_utc"]]
            .head(3)
            .to_dict("records")
        )
        raise ValueError(
            f"{model}: no usable reference price within {MAX_WEEKLY_STEPS} weeks for "
            f"{int((~usable).sum())} targets, e.g. {examples}"
        )
    if steps:
        log.info(
            "%s: used the 7-day step-back for some targets (%d round(s))", model, steps
        )

    out = frame.assign(
        model=model, series=cfg.series, p50=p50.astype("float64"), ref_time_utc=ref_utc
    )
    return out[POINT_COLUMNS]


# --------------------------------------------------------------------- quantiles


def add_quantiles(
    points: pd.DataFrame,
    prices: pd.Series,
    cfg: ForecastConfig,
    min_bucket_errors: int = MIN_BUCKET_ERRORS,
) -> pd.DataFrame:
    """Add q10/q90 from the model's own past errors, per issue and lead bucket.

    Rows whose issue has no known history yet (warm-up) get NaN quantiles.
    """
    if points["model"].nunique() != 1:
        raise ValueError("add_quantiles expects rows of a single model")
    df = points.reset_index(drop=True).copy()
    buckets = lead_bucket(
        lead_time_hours(df["issue_time_utc"], df["target_time_utc"]), cfg
    ).to_numpy()
    p50 = df["p50"].to_numpy(dtype="float64")
    err = lookup_prices(prices, df["target_time_utc"]) - p50
    issue = df["issue_time_utc"].dt.tz_convert(None).to_numpy()
    target = df["target_time_utc"].dt.tz_convert(None).to_numpy()
    history = np.timedelta64(cfg.quantile_history_days, "D")
    lag = np.timedelta64(cfg.price_known_lag_periods * 30, "m")

    q10 = np.full(len(df), np.nan)
    q90 = np.full(len(df), np.nan)
    for this_issue in np.unique(issue):
        cutoff = this_issue - lag
        known = (
            (issue >= this_issue - history)
            & (issue < this_issue)
            & (target <= cutoff)
            & ~np.isnan(err)
        )
        if not known.any():
            continue
        pooled = err[known]
        current = issue == this_issue
        for bucket in np.unique(buckets[current]):
            errors = err[known & (buckets == bucket)]
            if len(errors) < min_bucket_errors:
                errors = pooled
            lo, hi = np.percentile(errors, [10, 90])
            rows = current & (buckets == bucket)
            q10[rows] = p50[rows] + lo
            q90[rows] = p50[rows] + hi

    df["q10"] = np.minimum(q10, p50)
    df["q90"] = np.maximum(q90, p50)
    return df


# --------------------------------------------------------------------------- run


def run(
    models: list[str] | tuple[str, ...],
    start_date,
    end_date,
    cfg: ForecastConfig,
    *,
    prices_path: Path | str = DEFAULT_PRICES_PATH,
    out_path: Path | str = DEFAULT_FORECAST_PATH,
) -> pd.DataFrame:
    """Forecast every issue in [start_date, end_date], write the contract parquet.

    Returns the written rows plus the internal ``ref_time_utc`` column.
    """
    start, end = to_date(start_date), to_date(end_date)
    for model in models:
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}; choose from {MODELS}")
    prices = load_prices(prices_path)
    warmup_start = start - timedelta(days=cfg.quantile_history_days + 7)
    issues = issue_times(warmup_start, end, cfg)
    first_in_window = issue_times(start, start, cfg)[0]
    log.info(
        "Forecasting %s for issues %s..%s (%d issues incl. %d-day warm-up from %s)",
        list(models),
        start,
        end,
        len(issues),
        cfg.quantile_history_days + 7,
        warmup_start,
    )

    frames = []
    for model in models:
        points = forecast_points(model, issues, prices, cfg)
        with_q = add_quantiles(points, prices, cfg)
        in_window = with_q[with_q["issue_time_utc"] >= first_in_window]
        if in_window[["q10", "q90"]].isna().any().any():
            raise ValueError(
                f"{model}: quantiles undefined for some issues in the window; "
                f"actual prices are needed over the {cfg.quantile_history_days}-day warm-up"
            )
        log.info(
            "%s: %d rows for %d issues",
            model,
            len(in_window),
            in_window["issue_time_utc"].nunique(),
        )
        frames.append(in_window)

    result = pd.concat(frames, ignore_index=True)
    write_forecasts(result[CONTRACT_COLUMNS], out_path, cfg)
    return result


def summarise(result: pd.DataFrame) -> str:
    counts = result.groupby("model").agg(
        issues=("issue_time_utc", "nunique"),
        rows=("target_time_utc", "size"),
        p50_median=("p50", "median"),
    )
    first = result["issue_time_utc"].min()
    last = result["issue_time_utc"].max()
    return (
        f"Issues {first:%Y-%m-%d %H:%M}Z .. {last:%Y-%m-%d %H:%M}Z\n"
        + counts.to_string(float_format=lambda x: f"{x:.2f}")
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m v2g.forecast.baselines",
        description="Naive price-forecast baselines -> artefacts/forecasts/price_quantiles.parquet",
    )
    p.add_argument(
        "--window",
        choices=WINDOWS,
        default="smoke",
        help="named window from config (default smoke)",
    )
    p.add_argument("--start", help="first local issue date (overrides the window)")
    p.add_argument(
        "--end", help="last local issue date, inclusive (overrides the window)"
    )
    p.add_argument(
        "--models", default=",".join(MODELS), help=f"comma-separated subset of {MODELS}"
    )
    p.add_argument(
        "--prices",
        default=str(DEFAULT_PRICES_PATH),
        help="actuals parquet from the ingest stage",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_FORECAST_PATH),
        help="contract parquet to upsert into",
    )
    p.add_argument(
        "--config", default=str(DEFAULT_ASSUMPTIONS_PATH), help="assumptions yaml"
    )
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
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    result = run(models, start, end, cfg, prices_path=args.prices, out_path=args.out)
    print(summarise(result))
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
