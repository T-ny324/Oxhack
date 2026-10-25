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

Quantiles
    Shared with every S3 model: see :mod:`v2g.forecast.quantiles` (empirical
    residuals by lead-time bucket, warm-up before the window, only in-window
    issues written).
"""

from __future__ import annotations

import argparse
import logging
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
from v2g.forecast.quantiles import (  # noqa: F401
    MIN_BUCKET_ERRORS,
    add_quantiles,
    finalise_window,
    warmup_start,
)
from v2g.forecast.timing import (
    UTC_DTYPE,
    issue_times,
    known_cutoff,
    local_to_utc,
    target_times,
    to_date,
    tzinfo,
)

log = logging.getLogger(__name__)

MODELS = ("naive_weekly", "naive_daily")
MAX_WEEKLY_STEPS = 4  # fall back up to 4 weeks, 7 days at a time
TUE_TO_FRI = (1, 2, 3, 4)  # Monday == 0
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
    first_issue = warmup_start(start, cfg)
    issues = issue_times(first_issue, end, cfg)
    first_in_window = issue_times(start, start, cfg)[0]
    log.info(
        "Forecasting %s for issues %s..%s (%d issues incl. %d-day warm-up from %s)",
        list(models),
        start,
        end,
        len(issues),
        cfg.quantile_history_days + 7,
        first_issue,
    )

    frames = []
    for model in models:
        points = forecast_points(model, issues, prices, cfg)
        frames.append(
            finalise_window(
                add_quantiles(points, prices, cfg), first_in_window, model, cfg
            )
        )

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
