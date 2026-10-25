"""Data preparation for LEAR (stage S3, ladder step 2).

Two artefacts are derived from ``data/processed/prices.parquet``:

``prices_imputed``
    The raw UTC series with short gaps (at most ``impute_max_gap_periods``
    consecutive missing half-hours) filled by linear interpolation in UTC
    order. Longer gaps stay NaN; the ``imputed`` flag marks filled cells.
    Imputation feeds features and training targets ONLY. The evaluator scores
    against the raw actuals and skips missing ones.

``day_matrix``
    One row per LOCAL date (Europe/London), 48 columns = local half-hour slots
    0..47 where slot = 2 * hour + minute // 30. Conventions, as in epftoolbox:

    * 50-period days (autumn clock change): the two occurrences of each
      repeated local slot are averaged.
    * 46-period days (spring clock change): the two absent slots are filled by
      linear interpolation between their neighbours, so every row has 48
      values.
    * Partial days at the edges of the data, and genuine gaps longer than the
      imputation limit, stay NaN. Training rows touching them are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast.io import DEFAULT_PRICES_PATH, load_prices
from v2g.forecast.timing import FREQ, local_to_utc, tzinfo

SLOTS = 48


def slot_of(local_index: pd.DatetimeIndex) -> np.ndarray:
    """Local half-hour slot 0..47 of each timestamp (already in local time)."""
    return (local_index.hour * 2 + local_index.minute // 30).to_numpy()


def day_length_hours(d: date, cfg: ForecastConfig) -> int:
    """Real elapsed hours in local date ``d``: 23 (spring), 24, or 25 (autumn)."""
    tz = tzinfo(cfg)
    start = pd.Timestamp(datetime.combine(d, time(0), tzinfo=tz)).tz_convert("UTC")
    end = pd.Timestamp(
        datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)
    ).tz_convert("UTC")
    return round((end - start) / pd.Timedelta(hours=1))


def impute_short_gaps(prices: pd.Series, max_gap: int) -> tuple[pd.Series, pd.Series]:
    """Linearly interpolate runs of at most ``max_gap`` NaNs; longer runs stay NaN.

    Only interior gaps can be filled (there is nothing to interpolate towards at
    the edges). Returns (filled series, boolean 'imputed' flags).
    """
    missing = prices.isna()
    if max_gap <= 0 or not missing.any():
        return prices.copy(), pd.Series(False, index=prices.index)
    run_id = (missing != missing.shift(fill_value=False)).cumsum()
    run_len = missing.groupby(run_id).transform("size")
    fillable = missing & (run_len <= max_gap)
    filled = prices.where(
        ~fillable, prices.interpolate(method="linear", limit_area="inside")
    )
    imputed = fillable & filled.notna()
    return filled, imputed


def local_frame(prices: pd.Series, cfg: ForecastConfig) -> pd.DataFrame:
    """Per-timestamp local date and slot for a UTC-indexed price series."""
    local = prices.index.tz_convert(tzinfo(cfg))
    return pd.DataFrame(
        {
            "local_date": local.date,
            "slot": slot_of(local),
            "price": prices.to_numpy(dtype="float64"),
        },
        index=prices.index,
    )


def build_day_matrix(prices: pd.Series, cfg: ForecastConfig) -> pd.DataFrame:
    """Local-date x slot matrix with the epftoolbox DST conventions (see module docstring)."""
    frame = local_frame(prices, cfg)
    grouped = frame.groupby(["local_date", "slot"])["price"]
    matrix = (
        grouped.mean().unstack("slot").reindex(columns=range(SLOTS))
    )  # averages autumn duplicates
    present = (
        grouped.size().unstack("slot").reindex(columns=range(SLOTS)).notna()
    )  # slot exists in the UTC grid
    for d in matrix.index:
        if day_length_hours(d, cfg) == 23:
            absent = ~present.loc[d].to_numpy()
            interpolated = matrix.loc[d].interpolate(
                method="linear", limit_area="inside"
            )
            matrix.loc[d, absent] = interpolated.to_numpy()[absent]
    matrix.index.name = "local_date"
    matrix.columns.name = "slot"
    return matrix


def prices_from_day_matrix(matrix: pd.DataFrame, cfg: ForecastConfig) -> pd.Series:
    """Inverse of :func:`build_day_matrix` on the UTC grid (used to build synthetic series).

    Repeated autumn slots both take the slot's value; absent spring slots are not emitted.
    """
    first, last = matrix.index.min(), matrix.index.max() + timedelta(days=1)
    start = local_to_utc(pd.DatetimeIndex([pd.Timestamp(first)]), cfg)[0]
    end = local_to_utc(pd.DatetimeIndex([pd.Timestamp(last)]), cfg)[0]
    grid = pd.date_range(start, end, freq=FREQ, inclusive="left", unit="ns")
    local = grid.tz_convert(tzinfo(cfg))
    lookup = matrix.stack()
    keys = pd.MultiIndex.from_arrays([local.date, slot_of(local)])
    return pd.Series(
        lookup.reindex(keys).to_numpy(dtype="float64"), index=grid, name="actual"
    )


@dataclass
class LearData:
    prices_raw: pd.Series  # UTC index, NaN where missing (as load_prices)
    prices_imputed: pd.Series  # short gaps filled
    imputed: pd.Series  # bool flags
    day_matrix: pd.DataFrame  # local date x slot, built from prices_imputed


def prepare(prices_raw: pd.Series, cfg: ForecastConfig) -> LearData:
    filled, imputed = impute_short_gaps(prices_raw, cfg.lear.impute_max_gap_periods)
    return LearData(
        prices_raw=prices_raw,
        prices_imputed=filled,
        imputed=imputed,
        day_matrix=build_day_matrix(filled, cfg),
    )


def load_lear_data(
    path: Path | str = DEFAULT_PRICES_PATH, cfg: ForecastConfig | None = None
) -> LearData:
    if cfg is None:
        raise ValueError("cfg is required")
    return prepare(load_prices(path), cfg)
