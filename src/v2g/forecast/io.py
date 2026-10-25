"""Read/write/validate the S3 -> S5 forecast contract, plus actual-price loading.

Contract: one long parquet at ``artefacts/forecasts/price_quantiles.parquet``.

==================  ===================  ==========================================
column              dtype                meaning
==================  ===================  ==========================================
model               str                  e.g. "naive_weekly"
series              str                  "elexon_mid"
issue_time_utc      datetime64[ns, UTC]  when the forecast was issued
target_time_utc     datetime64[ns, UTC]  START of the forecast half-hour
p50                 float64              median forecast, £/MWh (required)
q10, q90            float64              10th/90th percentile, £/MWh (optional)
==================  ===================  ==========================================

Unique key: (model, series, issue_time_utc, target_time_utc). Each issue holds
exactly the periods from :func:`v2g.forecast.timing.target_times`. Quantile
columns are optional, but a model must provide them for all of its rows or none
of them; where present q10 <= p50 <= q90. £/MWh always; UTC always.

Writing is an upsert by model: rows for the models being written replace any
existing rows for those models, other models are kept, and the file is sorted
by key.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_float_dtype, is_string_dtype

from shared.schema import ForecastConfig
from v2g.forecast.timing import UTC_DTYPE, target_times, tzinfo

log = logging.getLogger(__name__)

KEY_COLUMNS = ["model", "series", "issue_time_utc", "target_time_utc"]
REQUIRED_COLUMNS = [*KEY_COLUMNS, "p50"]
QUANTILE_COLUMNS = ["q10", "q90"]
CONTRACT_COLUMNS = [*REQUIRED_COLUMNS, *QUANTILE_COLUMNS]
TIME_COLUMNS = ["issue_time_utc", "target_time_utc"]

DEFAULT_FORECAST_PATH = Path("artefacts/forecasts/price_quantiles.parquet")
DEFAULT_PRICES_PATH = Path("data/processed/prices.parquet")


# ----------------------------------------------------------------------- actuals


def load_prices(path: Path | str = DEFAULT_PRICES_PATH) -> pd.Series:
    """Actual prices as a UTC-indexed Series (£/MWh) with NaN where ``is_missing``."""
    df = pd.read_parquet(
        path, columns=["ts_utc", "price_wholesale_per_mwh", "is_missing"]
    )
    ts = df["ts_utc"]
    if not is_datetime64_any_dtype(ts) or ts.dt.tz is None or str(ts.dt.tz) != "UTC":
        raise ValueError(f"{path}: ts_utc must be tz-aware UTC, got {ts.dtype}")
    if not ts.is_unique:
        raise ValueError(f"{path}: ts_utc has duplicates")
    prices = pd.Series(
        df["price_wholesale_per_mwh"].astype("float64").to_numpy(),
        index=pd.DatetimeIndex(ts).as_unit("ns"),
        name="actual",
    )
    prices[df["is_missing"].to_numpy(dtype=bool)] = np.nan
    return prices.sort_index()


def lookup_prices(prices: pd.Series, when) -> np.ndarray:
    """Price at each timestamp in ``when`` (NaN where unknown or missing)."""
    idx = pd.DatetimeIndex(when).tz_convert("UTC").as_unit("ns")
    return prices.reindex(idx).to_numpy(dtype="float64")


# -------------------------------------------------------------------- validation


def _is_utc_ns(series: pd.Series) -> bool:
    return str(series.dtype) == UTC_DTYPE


def validate_forecasts(df: pd.DataFrame, cfg: ForecastConfig) -> None:
    """Raise ValueError listing every contract violation found in ``df``."""
    problems: list[str] = []

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Forecast contract violation: missing required columns {missing}"
        )
    extra = [c for c in df.columns if c not in CONTRACT_COLUMNS]
    if extra:
        problems.append(
            f"unexpected columns {extra} (drop internal columns before writing)"
        )

    for col in TIME_COLUMNS:
        if not _is_utc_ns(df[col]):
            problems.append(f"{col} must be {UTC_DTYPE}, got {df[col].dtype}")
    for col in ("model", "series"):
        if (
            not is_string_dtype(df[col])
            or df[col].isna().any()
            or (df[col] == "").any()
        ):
            problems.append(f"{col} must be non-empty strings")
    if not is_float_dtype(df["p50"]):
        problems.append(f"p50 must be float, got {df['p50'].dtype}")
    elif not np.isfinite(df["p50"].to_numpy()).all():
        problems.append("p50 contains NaN or inf")
    present_q = [c for c in QUANTILE_COLUMNS if c in df.columns]
    if present_q and len(present_q) != len(QUANTILE_COLUMNS):
        problems.append(f"quantile columns must come together, found only {present_q}")
    for col in present_q:
        if not is_float_dtype(df[col]):
            problems.append(f"{col} must be float, got {df[col].dtype}")
    if problems:
        raise ValueError("Forecast contract violations:\n - " + "\n - ".join(problems))

    if len(df) == 0:
        raise ValueError("Forecast contract violation: no rows")

    if (df["target_time_utc"] < df["issue_time_utc"]).any():
        n = int((df["target_time_utc"] < df["issue_time_utc"]).sum())
        problems.append(f"{n} rows have target_time_utc before issue_time_utc")

    dup = df.duplicated(subset=KEY_COLUMNS)
    if dup.any():
        problems.append(
            f"{int(dup.sum())} duplicate (model, series, issue_time_utc, target_time_utc) keys"
        )

    if len(present_q) == len(QUANTILE_COLUMNS):
        q10, p50, q90 = (df[c].to_numpy() for c in ("q10", "p50", "q90"))
        has = ~np.isnan(q10) & ~np.isnan(q90)
        half = np.isnan(q10) != np.isnan(q90)
        if half.any():
            problems.append(f"{int(half.sum())} rows have only one of q10/q90")
        per_model = df.assign(_has=has).groupby("model")["_has"].agg(["any", "all"])
        partial = per_model[per_model["any"] & ~per_model["all"]].index.tolist()
        if partial:
            problems.append(f"models with quantiles on only some rows: {partial}")
        bad = has & ((q10 > p50) | (p50 > q90))
        if bad.any():
            problems.append(f"{int(bad.sum())} rows violate q10 <= p50 <= q90")
        if has.any() and not (
            np.isfinite(q10[has]).all() and np.isfinite(q90[has]).all()
        ):
            problems.append("q10/q90 contain inf")

    tz = tzinfo(cfg)
    local_hhmm = df["issue_time_utc"].dt.tz_convert(tz).dt.strftime("%H:%M")
    wrong = local_hhmm != cfg.issue_local_time
    if wrong.any():
        examples = sorted(df.loc[wrong, "issue_time_utc"].unique())[:3]
        problems.append(
            f"{int(wrong.sum())} rows whose issue is not at {cfg.issue_local_time} {cfg.timezone}, e.g. {examples}"
        )

    for (model, series, issue), group in df.groupby(
        ["model", "series", "issue_time_utc"], sort=False
    ):
        expected = target_times(issue, cfg)
        got = pd.DatetimeIndex(group["target_time_utc"]).sort_values()
        if not got.equals(expected):
            n_missing = len(expected.difference(got))
            n_extra = len(got.difference(expected))
            problems.append(
                f"{model}/{series} issue {issue}: {n_missing} target periods missing, {n_extra} unexpected "
                f"(got {len(got)}, expected {len(expected)})"
            )
            if len(problems) > 20:
                problems.append("... further issues not listed")
                break

    if problems:
        raise ValueError("Forecast contract violations:\n - " + "\n - ".join(problems))


# ------------------------------------------------------------------- read/write


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce tz-aware time columns to ns and keep column order; never adds tz to naive times."""
    out = df.copy()
    for col in TIME_COLUMNS:
        if (
            col in out
            and is_datetime64_any_dtype(out[col])
            and out[col].dt.tz is not None
        ):
            out[col] = out[col].dt.tz_convert("UTC").astype(UTC_DTYPE)
    for col in ("model", "series"):
        if col in out:
            out[col] = out[col].astype(str)
    for col in ("p50", *QUANTILE_COLUMNS):
        if col in out:
            out[col] = out[col].astype("float64")
    ordered = [c for c in CONTRACT_COLUMNS if c in out.columns] + [
        c for c in out.columns if c not in CONTRACT_COLUMNS
    ]
    return out[ordered]


def write_forecasts(df: pd.DataFrame, path: Path | str, cfg: ForecastConfig) -> Path:
    """Validate ``df`` and upsert it into the contract parquet by model."""
    new = _normalise(df)
    validate_forecasts(new, cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    models = sorted(new["model"].unique())
    if path.exists():
        existing = _normalise(pd.read_parquet(path))
        kept = existing[~existing["model"].isin(models)]
        log.info(
            "Upsert into %s: replacing %d rows of %s, keeping %d rows of %s",
            path,
            int(existing["model"].isin(models).sum()),
            models,
            len(kept),
            sorted(kept["model"].unique()),
        )
        merged = pd.concat([kept, new], ignore_index=True) if len(kept) else new
    else:
        merged = new

    merged = merged.sort_values(KEY_COLUMNS, kind="mergesort").reset_index(drop=True)
    validate_forecasts(merged, cfg)
    merged.to_parquet(path, index=False, engine="pyarrow")
    log.info(
        "Wrote %s (%d rows, models %s)",
        path,
        len(merged),
        sorted(merged["model"].unique()),
    )
    return path


def load_forecasts(
    path: Path | str = DEFAULT_FORECAST_PATH, model: str | list[str] | None = None
) -> pd.DataFrame:
    """Load the contract parquet, optionally filtered to one model or a list of models."""
    df = _normalise(pd.read_parquet(path))
    if model is not None:
        wanted = [model] if isinstance(model, str) else list(model)
        df = df[df["model"].isin(wanted)].reset_index(drop=True)
    return df
