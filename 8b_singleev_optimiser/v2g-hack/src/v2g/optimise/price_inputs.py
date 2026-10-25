"""Price inputs for one issue: the S3 forecast (or the actuals, for perfect foresight) -> p_buy / p_sell.

Honours price_forecast_contract.md: rows are selected by (model, series, issue_time_utc) and laid
on the issue's target index from ``timing.target_times`` (74/76/78 periods); never by target time
alone, because consecutive issues overlap. Tariffs are applied here, in £/MWh:
    p_buy = wholesale + import_adder,  p_sell = wholesale + export_adder.
Actual prices come from data/processed/prices.parquet only and may be NaN (missing periods).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from v2g.forecast.timing import issue_times, target_times


class MissingForecastIssue(LookupError):
    """A model has no (complete) forecast for an issue: stop and report, never skip silently."""


def issue_targets(issue_utc, fcfg) -> pd.DatetimeIndex:
    """The issue's target periods (contract: issue -> end of the next local day)."""
    return target_times(issue_utc, fcfg)


def next_issue(issue_utc, fcfg) -> pd.Timestamp:
    """The next day's issue time, i.e. the end of this issue's execution window."""
    local_date = pd.Timestamp(issue_utc).tz_convert(fcfg.timezone).date()
    return issue_times(local_date + pd.Timedelta(days=1), local_date + pd.Timedelta(days=1), fcfg)[0]


def execution_length(issue_utc, targets: pd.DatetimeIndex, fcfg) -> int:
    """Number of targets executed before the next issue (48; 46/50 across the clock changes)."""
    return int((targets < next_issue(issue_utc, fcfg)).sum())


def with_tariff(wholesale: np.ndarray, tariff) -> tuple[np.ndarray, np.ndarray]:
    w = np.asarray(wholesale, dtype="float64")
    return w + tariff.import_adder_per_mwh, w + tariff.export_adder_per_mwh


def forecast_prices(forecasts: pd.DataFrame, model: str, series: str, issue_utc, targets: pd.DatetimeIndex, tariff):
    """(p_buy, p_sell) in £/MWh for exactly this issue's rows of ``model``, aligned to ``targets``."""
    rows = forecasts[(forecasts["model"] == model) & (forecasts["series"] == series) & (forecasts["issue_time_utc"] == issue_utc)]
    if rows.empty:
        raise MissingForecastIssue(f"{model}: no forecast rows for issue {issue_utc}")
    if rows["target_time_utc"].duplicated().any():
        raise ValueError(f"{model}: duplicate targets for issue {issue_utc}")
    p50 = rows.set_index("target_time_utc")["p50"].reindex(targets)
    if p50.isna().any():
        missing = p50.index[p50.isna()][:3].tolist()
        raise MissingForecastIssue(f"{model}: issue {issue_utc} lacks {int(p50.isna().sum())} target periods, e.g. {missing}")
    return with_tariff(p50.to_numpy(), tariff)


def actual_prices(prices: pd.Series, targets: pd.DatetimeIndex, tariff):
    """(p_buy, p_sell) from the actual wholesale series; NaN where the actual is missing."""
    wholesale = prices.reindex(targets).to_numpy(dtype="float64")
    return with_tariff(wholesale, tariff)


def perfect_foresight_prices(prices: pd.Series, targets: pd.DatetimeIndex, tariff):
    """Actuals as the plan's 'forecast'. The rare missing periods are linearly interpolated for
    PLANNING only; they stay excluded from everyone's revenue accounting."""
    wholesale = prices.reindex(targets).interpolate(limit_direction="both")
    if wholesale.isna().any():
        raise ValueError("perfect foresight: no actual price anywhere in the planning window")
    return with_tariff(wholesale.to_numpy(), tariff)
