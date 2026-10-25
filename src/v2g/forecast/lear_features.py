"""Feature construction for LEAR.

For an issue on local date ``d`` (10:00 local), the feature vector is:

* ``today_sNN``: today's known local slots 0..L, where L is the slot of the
  known cutoff (``known_cutoff()``: issue minus ``price_known_lag_periods``
  half-hours, so L = 18 = 09:00 for a 10:00 issue). Built from the RAW prices
  with start time <= cutoff only, then short gaps (and the two absent spring
  slots) interpolated within that block and edge gaps filled forward/backward
  up to the imputation limit. Nothing after the cutoff is ever read.
* ``dmK_sNN`` for K in (1, 2, 3, 6, 7): the full 48-slot rows of days d-K from
  the day matrix. d-6 is tomorrow's weekday one week earlier, d-7 is today's.
* ``dow_*``: one-hot day of week of ``d`` (the block fixes the target weekday).
* ``exog_*``: optional exogenous forecasts (NESO wind/solar/demand) for the
  target days, see the TODO in :func:`build_features`. None contributes nothing.

Models: one lasso per (block, slot). Block A = targets on ``d`` at slots from
the issue slot (20 = 10:00) to 47; Block B = slots 0..47 of ``d + 1``.
On a 50-period target day the two occurrences of a repeated slot share one
model and get the same p50; on a 46-period day the absent slots are simply
not requested. Training targets for a past issue d': Y_A = slots 20..47 of
d', Y_B = slots 0..47 of d'+1 (from the day matrix, so spring-day targets in
slots 2 and 3 are the interpolated values).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast.lear_data import SLOTS, impute_short_gaps, slot_of
from v2g.forecast.timing import issue_local_time, known_cutoff, local_to_utc, tzinfo

PAST_DAYS = (1, 2, 3, 6, 7)
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def issue_slot(cfg: ForecastConfig) -> int:
    at = issue_local_time(cfg)
    return at.hour * 2 + at.minute // 30


def last_known_slot(cfg: ForecastConfig) -> int:
    """Slot of the known cutoff: issue slot minus the configured lag (18 for 10:00 and lag 2)."""
    slot = issue_slot(cfg) - cfg.price_known_lag_periods
    if slot < 0:
        raise ValueError(
            "known cutoff falls on the previous local day; today_* features assume the same day"
        )
    return slot


def model_keys(cfg: ForecastConfig) -> list[tuple[str, int]]:
    """(block, slot) for every model: Block A from the issue slot to 47, Block B 0..47."""
    return [("A", s) for s in range(issue_slot(cfg), SLOTS)] + [
        ("B", s) for s in range(SLOTS)
    ]


def feature_names(
    cfg: ForecastConfig, exog_columns: list[str] | None = None
) -> list[str]:
    names = [f"today_s{s:02d}" for s in range(last_known_slot(cfg) + 1)]
    for k in PAST_DAYS:
        names += [f"dm{k}_s{s:02d}" for s in range(SLOTS)]
    names += [f"dow_{n}" for n in DAY_NAMES]
    names += [f"exog_{c}" for c in (exog_columns or [])]
    return names


def price_feature_mask(names: list[str]) -> np.ndarray:
    """True for features measured in £/MWh (they go through the price transform)."""
    return np.array([n.startswith(("today_", "dm")) for n in names], dtype=bool)


def today_block(
    issue_utc: pd.Timestamp, prices_raw: pd.Series, cfg: ForecastConfig
) -> np.ndarray:
    """Today's known slots 0..L from raw prices with start <= known cutoff."""
    tz = tzinfo(cfg)
    cutoff = known_cutoff(issue_utc, cfg)
    cutoff_slot = int(slot_of(pd.DatetimeIndex([cutoff.tz_convert(tz)]))[0])
    expected = last_known_slot(cfg)
    if (
        cutoff_slot != expected
        or cutoff.tz_convert(tz).date() != issue_utc.tz_convert(tz).date()
    ):
        raise ValueError(
            f"known cutoff {cutoff} is not slot {expected} of the issue day"
        )
    d = issue_utc.tz_convert(tz).date()
    day_start = local_to_utc(pd.DatetimeIndex([pd.Timestamp(d)]), cfg)[0]
    window = prices_raw.loc[
        day_start:cutoff
    ]  # inclusive; everything here starts <= cutoff
    by_slot = (
        pd.Series(window.to_numpy(dtype="float64"))
        .groupby(slot_of(window.index.tz_convert(tz)))
        .mean()
    )
    block = by_slot.reindex(range(expected + 1))
    max_gap = cfg.lear.impute_max_gap_periods
    block, _ = impute_short_gaps(
        block, max_gap
    )  # interior gaps, incl. the absent spring slots
    if max_gap > 0:
        block = block.ffill(limit=max_gap).bfill(limit=max_gap)
    return block.to_numpy(dtype="float64")


def build_features(
    issue_utc,
    day_matrix: pd.DataFrame,
    prices_raw: pd.Series,
    cfg: ForecastConfig,
    exog: pd.DataFrame | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Feature vector and names for one issue. Uses only prices with start <= known_cutoff(issue)."""
    issue_utc = pd.Timestamp(issue_utc).tz_convert("UTC")
    d = issue_utc.tz_convert(tzinfo(cfg)).date()
    parts = [today_block(issue_utc, prices_raw, cfg)]
    for k in PAST_DAYS:
        day = d - timedelta(days=k)
        row = (
            day_matrix.loc[day].to_numpy(dtype="float64")
            if day in day_matrix.index
            else np.full(SLOTS, np.nan)
        )
        parts.append(row)
    dow = np.zeros(len(DAY_NAMES))
    dow[d.weekday()] = 1.0
    parts.append(dow)
    exog_columns: list[str] = []
    if exog is not None:
        # TODO(NESO): pass target-day wind/solar/demand forecasts here, indexed by issue_time_utc and
        # filtered on forecast-made time <= issue time, one column per (variable, target slot).
        exog_columns = list(exog.columns)
        parts.append(exog.loc[issue_utc].to_numpy(dtype="float64"))
    return np.concatenate(parts), feature_names(cfg, exog_columns)


def training_targets(
    day_matrix: pd.DataFrame, d: date, cfg: ForecastConfig
) -> np.ndarray:
    """Targets for an issue on ``d`` in :func:`model_keys` order (NaN where unavailable)."""
    row_a = (
        day_matrix.loc[d].to_numpy(dtype="float64")
        if d in day_matrix.index
        else np.full(SLOTS, np.nan)
    )
    tomorrow = d + timedelta(days=1)
    row_b = (
        day_matrix.loc[tomorrow].to_numpy(dtype="float64")
        if tomorrow in day_matrix.index
        else np.full(SLOTS, np.nan)
    )
    return np.concatenate([row_a[issue_slot(cfg) :], row_b])
