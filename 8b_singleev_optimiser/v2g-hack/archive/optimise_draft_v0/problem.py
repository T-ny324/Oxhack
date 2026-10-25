"""Problem data for the single-vehicle optimiser (§8.B).

Conventions
-----------
* Step t covers [ts[t], ts[t] + dt). Power is constant within a step.
* soc[t] is the SoC at the *start* of step t, so soc has T + 1 entries.
* A departure happens at step t when plugged[t-1] and not plugged[t]; the driver's
  requirement is soc[t] >= soc_req[t] (NaN everywhere that is not a departure).
* drive_kwh[t] is the energy the car uses while away during step t (taken from the battery).
* Prices are currency/MWh. Power is kW. Energy is kWh.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from shared.schema import Assumptions, ConverterSpec


@dataclass(frozen=True)
class VehicleParams:
    e_cap_kwh: float
    soc_min: float
    soc_max: float
    eta_ch: float          # grid -> battery, converter x sqrt(battery round trip)
    eta_dis: float         # battery -> grid
    p_ch_max_kw: float
    p_dis_max_kw: float
    c_deg_per_mwh: float   # cost per MWh of battery throughput (charge + discharge)
    standby_kw: float
    dt_h: float

    @classmethod
    def from_config(cls, spec: ConverterSpec, a: Assumptions, c_deg_per_mwh: float | None = None):
        """Efficiency is read from the spec curve at ONE operating point (optimiser.eta_load_fraction).

        Stated simplification (§8.B): the curve is non-convex in P, so the MILP uses a fixed eta.
        Price arbitrage is bang-bang, so schedules run at rated power almost all the time and the
        error is small; `accounting.efficiency_check` reports it for any schedule.
        """
        eta_c, eta_d = spec.efficiency_curve.eta_at(a.optimiser.eta_load_fraction)
        batt = np.sqrt(a.battery.eta_batt_rt)
        return cls(
            e_cap_kwh=a.battery.e_cap_kwh,
            soc_min=a.battery.soc_min,
            soc_max=a.battery.soc_max,
            eta_ch=eta_c * batt,
            eta_dis=eta_d * batt,
            p_ch_max_kw=spec.power.p_charge_max_kw,
            p_dis_max_kw=spec.power.p_discharge_max_kw,
            c_deg_per_mwh=a.battery.c_deg_linear_per_mwh if c_deg_per_mwh is None else c_deg_per_mwh,
            standby_kw=spec.standby_w / 1000.0,
            dt_h=a.dt_h,
        )


@dataclass
class VehicleTrace:
    vehicle_id: str
    index: pd.DatetimeIndex
    plugged: np.ndarray            # bool (T,)
    drive_kwh: np.ndarray          # (T,)
    soc_req: np.ndarray            # (T,) NaN except at departure steps
    soc0: float
    archetype: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def T(self) -> int:
        return len(self.index)

    def departures(self) -> np.ndarray:
        return np.flatnonzero(~np.isnan(self.soc_req))

    def window(self, start: int, stop: int) -> "VehicleTrace":
        return VehicleTrace(self.vehicle_id, self.index[start:stop], self.plugged[start:stop],
                            self.drive_kwh[start:stop], self.soc_req[start:stop], self.soc0,
                            self.archetype, dict(self.meta))


def sessions_to_trace(sessions: pd.DataFrame, index: pd.DatetimeIndex, params: VehicleParams,
                      soc0: float) -> VehicleTrace:
    """Turn one vehicle's session table into a per-step trace on `index`.

    Snapping is conservative: arrivals round UP to the next step, departures round DOWN,
    so the optimiser never assumes the car is plugged in when it might not be.
    The departure requirement is max(declared, soc_min + trip energy / E_cap): the driver
    must always be able to finish the next trip without dipping under the V2G floor.
    """
    assert sessions["vehicle_id"].nunique() == 1
    T = len(index)
    dt = pd.Timedelta(hours=params.dt_h)
    t0 = index[0]
    plugged = np.zeros(T, bool)
    drive = np.zeros(T)
    req = np.full(T, np.nan)

    def step_ceil(ts):
        return int(np.clip(np.ceil((ts - t0) / dt), 0, T))

    def step_floor(ts):
        return int(np.clip(np.floor((ts - t0) / dt), 0, T))

    s = sessions.sort_values("arrival_utc").reset_index(drop=True)
    spans = []
    for _, r in s.iterrows():
        a = step_ceil(r.arrival_utc)
        d = T if pd.isna(r.departure_utc) else step_floor(r.departure_utc)
        if d > a:
            plugged[a:d] = True
            spans.append((a, d, float(r.soc_required), float(r.trip_kwh_next)))
    for i, (a, d, soc_r, trip) in enumerate(spans):
        if d >= T:
            continue
        nxt = spans[i + 1][0] if i + 1 < len(spans) else T
        away = np.arange(d, nxt)
        away = away[~plugged[away]]
        if len(away):
            drive[away] += trip / len(away)
        need = params.soc_min + trip / params.e_cap_kwh
        r = max(soc_r, need)
        if r > params.soc_max + 1e-9:
            raise ValueError(f"trip of {trip:.1f} kWh needs SoC {r:.2f} > soc_max")
        req[d] = r
    vid = str(s["vehicle_id"].iloc[0])
    arch = str(s["archetype"].iloc[0]) if "archetype" in s else ""
    return VehicleTrace(vid, index, plugged, drive, req, soc0, arch)


def integrate_soc(params: VehicleParams, soc0: float, p_ch, p_dis, drive_kwh) -> np.ndarray:
    """Forward-integrate SoC from powers. This is the single source of truth for reported SoC."""
    dE = (params.eta_ch * np.asarray(p_ch) - np.asarray(p_dis) / params.eta_dis) * params.dt_h \
        - np.asarray(drive_kwh)
    return soc0 + np.concatenate([[0.0], np.cumsum(dE)]) / params.e_cap_kwh
