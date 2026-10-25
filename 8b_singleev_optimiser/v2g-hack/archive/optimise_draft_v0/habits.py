"""Learned driver habits: the "learns each driver's usage" half of the software claim.

Fitted on a vehicle's HISTORY only (the days before the evaluation window), never on the
window being scored. Habits are keyed on local-time slot-of-week so a 07:30 departure stays
at 07:30 across a clock change.

Ladder: this is the empirical baseline (Laplace-smoothed slot frequencies). Natural upgrades:
recency weighting, archetype pooling from the fleet clustering (8.D), or a classifier on
calendar features. The interface (`p_plugged`, `drive_rate`, `trip_kwh_p95`, `soc_req`) stays.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from shared.units import slot_of_week

from .problem import VehicleTrace


def _away_runs(plugged: np.ndarray) -> list[tuple[int, int]]:
    runs, T, t = [], len(plugged), 0
    while t < T:
        if not plugged[t]:
            s = t
            while t < T and not plugged[t]:
                t += 1
            runs.append((s, t))
        else:
            t += 1
    return runs


@dataclass
class Habits:
    p_plugged: np.ndarray        # (slots_per_week,) probability plugged in
    drive_rate_kwh: float        # mean kWh used per away step
    trip_kwh_p95: float          # cap on predicted energy for one away spell
    soc_req: float               # typical declared departure SoC
    dt_h: float
    tz: str
    n_history_days: float

    @classmethod
    def fit(cls, hist: VehicleTrace | None, dt_h: float, tz: str, soc_req_default: float,
            fallback_trip_kwh: float = 10.0) -> "Habits":
        slots = int(round(7 * 24 / dt_h))
        if hist is None or hist.T == 0:
            return cls(np.full(slots, 0.5), fallback_trip_kwh / (8 / dt_h), fallback_trip_kwh,
                       soc_req_default, dt_h, tz, 0.0)
        sow = slot_of_week(hist.index, dt_h, tz)
        n = np.bincount(sow, minlength=slots)
        k = np.bincount(sow, weights=hist.plugged.astype(float), minlength=slots)
        p = (k + 0.5) / (n + 1.0)                              # Laplace smoothing
        runs = _away_runs(hist.plugged)
        energies = np.array([hist.drive_kwh[s:e].sum() for s, e in runs])
        lengths = np.array([e - s for s, e in runs])
        if len(runs):
            rate = float(energies.sum() / lengths.sum())
            p95 = float(np.percentile(energies, 95))
        else:
            rate, p95 = fallback_trip_kwh / (8 / dt_h), fallback_trip_kwh
        deps = hist.departures()
        req = float(np.median(hist.soc_req[deps])) if len(deps) else soc_req_default
        return cls(p, rate, max(p95, 1e-6), req, dt_h, tz, hist.T * dt_h / 24)

    def predict_plugged(self, index: pd.DatetimeIndex) -> np.ndarray:
        return self.p_plugged[slot_of_week(index, self.dt_h, self.tz)]


def predicted_window(trace: VehicleTrace, t: int, H: int, habits: Habits, soc_min: float,
                     soc_max: float, e_cap_kwh: float):
    """What the software believes about steps t .. t+H-1, standing at step t (car plugged in).

    Known: the car is plugged in now, and the driver's declared departure time and SoC for
    this session (as entered in the app). Predicted: everything after that departure.
    """
    T = trace.T
    d = t + int(np.argmax(~trace.plugged[t:])) if (~trace.plugged[t:]).any() else T
    end = min(t + H, T)
    H = end - t
    a = np.zeros(H, bool)
    drive = np.zeros(H)
    req = np.full(H, np.nan)
    a[: min(d, end) - t] = True
    if d < end:
        j0 = d - t
        pred = habits.predict_plugged(trace.index[d:end]) >= 0.5
        pred[0] = False                                       # it has just left
        a[j0:] = pred
        for s, e in _away_runs(a[j0:]):
            s, e = s + j0, e + j0
            energy = min(habits.drive_rate_kwh * (e - s), habits.trip_kwh_p95)
            drive[s:e] = energy / (e - s)
            base = trace.soc_req[d] if s == j0 else habits.soc_req
            req[s] = min(soc_max, max(base, soc_min + energy / e_cap_kwh))
    return a, drive, req
