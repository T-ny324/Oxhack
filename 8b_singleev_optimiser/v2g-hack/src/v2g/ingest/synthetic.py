"""Synthetic placeholder data so S5 runs before real ingest (S2) exists.

Everything produced here is labelled `source = "synthetic"` and must be shown as such
on the dashboard. Shapes are loosely GB-like; numbers are NOT real market data.

Outputs follow the S2 schemas so real loaders can replace this without code changes:
  prices   : ts_utc (UTC, start of period), price_wholesale_per_mwh, source
  sessions : vehicle_id, archetype, arrival_utc, departure_utc (NaT = still plugged at end),
             soc_required (fraction, driver's "ready by" SoC at departure),
             trip_kwh_next (energy used between this departure and the next arrival), source
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from shared.units import utc_index


def synthetic_prices(start, days: int, dt_h: float, tz: str, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = utc_index(start, pd.Timestamp(start) + pd.Timedelta(days=days), dt_h)
    local = idx.tz_convert(tz)
    h = np.asarray(local.hour + local.minute / 60)
    weekend = np.asarray(local.dayofweek >= 5)

    shape = np.full(len(idx), 75.0)
    shape += np.where(h < 6, -28, 0)
    shape += np.where((h >= 7) & (h < 9.5), 18, 0)
    shape += np.where((h >= 11) & (h < 15), -12 - 10 * weekend, 0)        # solar dip
    shape += np.where((h >= 16.5) & (h < 19.5), 60 - 15 * weekend, 0)     # evening peak
    shape += np.where((h >= 19.5) & (h < 22), 15, 0)

    # wind: smooth multi-day AR(1) in [0, ~1.6]; windy periods depress prices
    steps_per_day = int(round(24 / dt_h))
    phi = np.exp(-1 / (1.5 * steps_per_day))
    w = np.zeros(len(idx))
    for t in range(1, len(idx)):
        w[t] = phi * w[t - 1] + np.sqrt(1 - phi**2) * rng.normal()
    wind = np.clip(0.6 + 0.6 * w, 0, 1.6)
    price = shape * (1.15 - 0.45 * wind) - 25 * wind + rng.normal(0, 7, len(idx))
    # windy nights go negative sometimes (exercises the MILP no-simultaneous constraint)
    price = np.where((wind > 1.2) & (h < 5), price - 45, price)
    return pd.DataFrame({"ts_utc": idx, "price_wholesale_per_mwh": price.round(2),
                         "source": "synthetic"})


def _local(day: pd.Timestamp, hour: float, tz: str) -> pd.Timestamp:
    return (day + pd.Timedelta(hours=hour)).tz_localize(tz, nonexistent="shift_forward",
                                                        ambiguous=False).tz_convert("UTC")


def synthetic_sessions(start, days: int, n_vehicles: int, tz: str, soc_required: float,
                       seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 1)
    start_utc = pd.Timestamp(start, tz="UTC")
    end_utc = start_utc + pd.Timedelta(days=days)
    local_days = pd.date_range(start_utc.tz_convert(tz).normalize().tz_localize(None),
                               periods=days + 1, freq="D")
    archetypes = ["commuter", "home_all_day"]
    rows = []
    for v in range(n_vehicles):
        arch = archetypes[v % len(archetypes)]
        req = float(np.clip(soc_required + rng.normal(0, 0.05), 0.45, 0.8))
        trips = []  # (depart_utc, return_utc, kwh)
        for d in local_days:
            wd = d.dayofweek
            day_trips = []
            if arch == "commuter":
                if wd < 5:
                    day_trips.append((rng.normal(7.75, 0.4), rng.normal(18.0, 0.8), rng.normal(9, 2)))
                elif wd == 5 and rng.random() < 0.7:
                    day_trips.append((rng.normal(11, 1), rng.normal(16, 1.2), rng.normal(12, 4)))
                elif wd == 6 and rng.random() < 0.4:
                    day_trips.append((rng.normal(13, 1), rng.normal(17, 1), rng.normal(8, 3)))
            else:
                if rng.random() < 0.6:
                    s = rng.normal(10.5, 1.2)
                    day_trips.append((s, s + max(0.75, rng.normal(2, 0.6)), rng.normal(4, 1.5)))
                if rng.random() < 0.3:
                    s = rng.normal(15, 0.8)
                    day_trips.append((s, s + max(0.75, rng.normal(1.5, 0.4)), rng.normal(3, 1)))
            for a, b, e in day_trips:
                dep, ret = _local(d, a, tz), _local(d, max(b, a + 0.5), tz)
                if trips and dep <= trips[-1][1] + pd.Timedelta(minutes=30):
                    continue  # overlapping trip, drop it
                trips.append((dep, ret, float(max(1.0, e))))
        trips = [t for t in trips if t[0] > start_utc and t[1] < end_utc]
        # sessions are the gaps between trips; vehicle starts plugged in at start
        arrival = start_utc
        for dep, ret, kwh in trips:
            rows.append((f"ev{v:03d}", arch, arrival, dep, req, kwh))
            arrival = ret
        rows.append((f"ev{v:03d}", arch, arrival, pd.NaT, req, 0.0))
    df = pd.DataFrame(rows, columns=["vehicle_id", "archetype", "arrival_utc", "departure_utc",
                                     "soc_required", "trip_kwh_next"])
    df["departure_utc"] = pd.to_datetime(df["departure_utc"], utc=True)
    df["source"] = "synthetic"
    return df
