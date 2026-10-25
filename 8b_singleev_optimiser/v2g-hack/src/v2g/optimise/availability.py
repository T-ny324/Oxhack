"""Availability for the forecast backtest: a documented commuter profile -> sessions table.

No real 2025-26 sessions exist, so the plug-in pattern comes from config
(``forecast_backtest.profile``): weekdays away between depart and return hours, a Saturday trip,
Sundays at home. The table has the schema ``daily.trace_from_sessions`` expects
(arrival_utc, departure_utc, trip_kwh_next), so the optimiser's own availability and
departure-floor logic is reused unchanged. Local hours are converted with the DST-safe helper.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from v2g.forecast.timing import local_to_utc_tz


def _trips_for_day(day: date, profile) -> list[tuple[float, float, float]]:
    weekday = day.weekday()
    if weekday < 5:
        return [(profile.weekday_depart_h, profile.weekday_return_h, profile.weekday_trip_kwh)]
    if weekday == 5:
        return [(profile.saturday_depart_h, profile.saturday_return_h, profile.saturday_trip_kwh)]
    if profile.sunday_trip_kwh > 0:
        return [(profile.weekday_depart_h, profile.weekday_return_h, profile.sunday_trip_kwh)]
    return []


def sessions_from_profile(profile, first_day: date, last_day: date, tz: str, vehicle_id: str = "profile_commuter") -> pd.DataFrame:
    """Plug-in sessions between the profile's trips over [first_day, last_day] (local dates)."""
    trips = []
    day = first_day
    while day <= last_day:
        for dep_h, ret_h, kwh in _trips_for_day(day, profile):
            local = pd.DatetimeIndex([pd.Timestamp(day) + pd.Timedelta(hours=dep_h), pd.Timestamp(day) + pd.Timedelta(hours=ret_h)])
            dep_utc, ret_utc = local_to_utc_tz(local, tz)
            trips.append((dep_utc, ret_utc, float(kwh)))
        day += timedelta(days=1)
    start = local_to_utc_tz(pd.DatetimeIndex([pd.Timestamp(first_day)]), tz)[0]
    rows, arrival = [], start
    for dep, ret, kwh in trips:
        rows.append((vehicle_id, arrival, dep, kwh))
        arrival = ret
    rows.append((vehicle_id, arrival, pd.NaT, 0.0))  # plugged in until the end
    out = pd.DataFrame(rows, columns=["vehicle_id", "arrival_utc", "departure_utc", "trip_kwh_next"])
    out["departure_utc"] = pd.to_datetime(out["departure_utc"], utc=True)
    out["arrival_utc"] = pd.to_datetime(out["arrival_utc"], utc=True)
    return out
