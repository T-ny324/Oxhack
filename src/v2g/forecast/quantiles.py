"""Empirical residual quantiles by lead-time bucket, shared by every S3 model.

For each issue, take the model's own past errors (actual - p50) from issues in
the previous ``quantile_history_days``, using only targets whose actual price
was known at this issue's cutoff. Within each lead bucket
q10 = p50 + P10(errors) and q90 = p50 + P90(errors); a bucket with fewer than
``MIN_BUCKET_ERRORS`` errors pools all buckets. q10 <= p50 <= q90 is enforced.

Models generate point forecasts from :func:`warmup_start` (window start minus
``quantile_history_days`` minus 7 days) and write only the in-window rows
(:func:`finalise_window`).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast.io import lookup_prices
from v2g.forecast.timing import lead_bucket, lead_time_hours, to_date

log = logging.getLogger(__name__)

MIN_BUCKET_ERRORS = 10


def warmup_start(start_date, cfg: ForecastConfig) -> date:
    """First issue date to generate so the first in-window issue has full error history."""
    return to_date(start_date) - timedelta(days=cfg.quantile_history_days + 7)


def add_quantiles(
    points: pd.DataFrame,
    prices: pd.Series,
    cfg: ForecastConfig,
    min_bucket_errors: int = MIN_BUCKET_ERRORS,
) -> pd.DataFrame:
    """Add q10/q90 from the model's own past errors, per issue and lead bucket.

    ``points`` holds one model's rows with issue_time_utc, target_time_utc and p50.
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


def finalise_window(
    with_quantiles: pd.DataFrame,
    first_in_window: pd.Timestamp,
    model: str,
    cfg: ForecastConfig,
) -> pd.DataFrame:
    """Keep in-window issues and refuse NaN quantiles (insufficient warm-up history)."""
    in_window = with_quantiles[with_quantiles["issue_time_utc"] >= first_in_window]
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
    return in_window
