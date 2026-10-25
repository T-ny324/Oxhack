"""Time helpers for stage S3 forecasts: issue times, target windows, lead times.

Every function takes the ``forecast`` section of config/assumptions.yaml
(:class:`shared.schema.ForecastConfig`) so no clock number is hard-coded here.

Conventions
-----------
* Timestamps are tz-aware UTC at nanosecond resolution (``datetime64[ns, UTC]``).
* "Local" means the configured IANA zone (Europe/London), resolved with
  :mod:`zoneinfo`, never a fixed offset.
* A settlement period is identified by its START.
* An issue covers every half-hour from the issue time (inclusive) to the end of
  the NEXT local day (exclusive): normally 76 periods, 78 when the next local
  day is the autumn clock change, 74 for the spring one.

DST rules when turning a local wall-clock time into UTC (:func:`local_to_utc`)
----------------------------------------------------------------------------
* A nonexistent local time (spring forward: 01:00-01:59 on the change day) is
  replaced by the time one hour earlier.
* An ambiguous local time (autumn back: 01:00-01:59 occurs twice) resolves to
  the first occurrence, the one still on summer time.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig

FREQ = "30min"
PERIOD = pd.Timedelta(FREQ)
UTC_DTYPE = "datetime64[ns, UTC]"


def tzinfo(cfg: ForecastConfig) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def issue_local_time(cfg: ForecastConfig) -> time:
    hour, minute = (int(part) for part in cfg.issue_local_time.split(":"))
    return time(hour, minute)


def to_date(value: date | str | pd.Timestamp | datetime) -> date:
    """Coerce a calendar-date-like value to :class:`datetime.date`."""
    if isinstance(value, datetime):  # includes pd.Timestamp
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def local_to_utc_tz(local_naive, tz: ZoneInfo | str) -> pd.DatetimeIndex:
    """Localise naive wall-clock times in ``tz`` and return UTC (DST rules in the module docstring)."""
    idx = pd.DatetimeIndex(local_naive)
    if idx.tz is not None:
        raise ValueError("local_to_utc expects naive wall-clock times")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    localised = idx.tz_localize(
        zone,
        ambiguous=np.ones(len(idx), dtype=bool),  # True = DST = first occurrence
        nonexistent=pd.Timedelta(hours=-1),
    )
    return localised.tz_convert("UTC").as_unit("ns")


def local_to_utc(local_naive, cfg: ForecastConfig) -> pd.DatetimeIndex:
    """Localise naive wall-clock times in the configured zone and return UTC.

    Applies the DST rules from the module docstring: nonexistent times shift
    one hour earlier, ambiguous times take the first (summer-time) occurrence.
    """
    return local_to_utc_tz(local_naive, tzinfo(cfg))


def settlement_period_to_utc(
    settlement_date, settlement_period, tz: ZoneInfo | str
) -> pd.DatetimeIndex:
    """UTC start of GB settlement periods: local midnight of the date plus (period - 1) half-hours.

    Periods count elapsed half-hours of the local day (1..46 on the spring
    change day, 1..50 on the autumn one), so the offset is added in absolute
    time after converting midnight.
    """
    dates = pd.DatetimeIndex(pd.to_datetime(settlement_date)).normalize()
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    midnight_utc = local_to_utc_tz(dates, tz)
    periods = np.asarray(settlement_period, dtype="int64")
    return (midnight_utc + pd.to_timedelta((periods - 1) * 30, unit="m")).as_unit("ns")


def issue_times(start_date, end_date, cfg: ForecastConfig) -> pd.DatetimeIndex:
    """One issue per local calendar date in [start_date, end_date], as UTC."""
    start, end = to_date(start_date), to_date(end_date)
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")
    at = issue_local_time(cfg)
    local = pd.date_range(start, end, freq="D") + pd.Timedelta(
        hours=at.hour, minutes=at.minute
    )
    return local_to_utc(local, cfg).rename("issue_time_utc")


def target_times(issue_utc, cfg: ForecastConfig) -> pd.DatetimeIndex:
    """Half-hour period starts from the issue (inclusive) to the end of the next local day."""
    issue = pd.Timestamp(issue_utc)
    if issue.tzinfo is None:
        raise ValueError("issue_utc must be tz-aware")
    issue = issue.tz_convert("UTC").as_unit("ns")
    if issue != issue.floor(FREQ):
        raise ValueError(f"issue time {issue} is not on the {FREQ} grid")
    tz = tzinfo(cfg)
    end_local_date = issue.tz_convert(tz).date() + timedelta(
        days=2
    )  # midnight closing the next day
    end_utc = pd.Timestamp(
        datetime.combine(end_local_date, time(0), tzinfo=tz)
    ).tz_convert("UTC")
    return pd.date_range(
        issue, end_utc, freq=FREQ, inclusive="left", unit="ns", name="target_time_utc"
    )


def known_cutoff(issue_utc, cfg: ForecastConfig):
    """Latest period START whose price is treated as known at the issue.

    Works element-wise on a Series/Index as well as on a single Timestamp.
    """
    return issue_utc - cfg.price_known_lag_periods * PERIOD


def lead_time_hours(issue_utc, target_utc):
    """Hours from issue to the target period start (element-wise for Series)."""
    return (target_utc - issue_utc) / pd.Timedelta(hours=1)


def bucket_labels(cfg: ForecastConfig) -> list[str]:
    edges = cfg.lead_buckets_h
    return [f"{lo:g}-{hi:g}h" for lo, hi in itertools.pairwise(edges)]


def lead_bucket(lead_h, cfg: ForecastConfig) -> pd.Series:
    """Left-closed lead-time bucket label for each lead (hours).

    Raises if any lead falls outside the configured edges.
    """
    lead = pd.Series(lead_h, dtype="float64")
    cats = pd.cut(lead, bins=cfg.lead_buckets_h, right=False, labels=bucket_labels(cfg))
    if cats.isna().any():
        bad = sorted(set(lead[cats.isna()].round(2)))[:5]
        raise ValueError(
            f"lead times outside configured buckets {cfg.lead_buckets_h}: {bad}"
        )
    return cats.astype(str)


def target_block(issue_utc, target_utc, cfg: ForecastConfig) -> pd.Series:
    """'A' when the target is on the issue's local day, 'B' when on the next local day."""
    tz = tzinfo(cfg)
    issue_day = pd.DatetimeIndex(issue_utc).tz_convert(tz).normalize()
    target_day = pd.DatetimeIndex(target_utc).tz_convert(tz).normalize()
    index = issue_utc.index if isinstance(issue_utc, pd.Series) else None
    return pd.Series(
        np.where(target_day == issue_day, "A", "B"), index=index, name="block"
    )
