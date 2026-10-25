"""Score any schedule the same way, whichever strategy produced it.

Costs are positive = owner pays. Every strategy is scored on TRUE prices and the TRUE trace.

net_cost  = import cost - export revenue + degradation + standby
adj_cost  = net_cost - v_term * E_cap * (soc_end - soc_start)
            (credits energy left in the battery at the end, so strategies that end at
             different SoC are compared fairly; v_term is the same one the optimiser used)
value     = adj_cost(unmanaged) - adj_cost(strategy)   -> "what this strategy is worth"
"""
from __future__ import annotations

import numpy as np

from shared.schema import Assumptions, ConverterSpec

from .problem import VehicleParams, VehicleTrace, integrate_soc

TOL = 1e-6


def terminal_value_per_kwh(params: VehicleParams, buy) -> float:
    """Value of 1 kWh sitting in the battery = mean import price / eta_ch (currency/kWh)."""
    return float(np.mean(buy)) / params.eta_ch / 1000.0


def evaluate(params: VehicleParams, trace: VehicleTrace, buy, sell, p_ch, p_dis,
             v_term_per_kwh: float) -> dict:
    p_ch, p_dis = np.asarray(p_ch, float), np.asarray(p_dis, float)
    buy, sell = np.asarray(buy, float), np.asarray(sell, float)
    dt, E = params.dt_h, params.e_cap_kwh
    soc = integrate_soc(params, trace.soc0, p_ch, p_dis, trace.drive_kwh)

    import_cost = float(np.sum(buy * p_ch) * dt / 1000)
    export_rev = float(np.sum(sell * p_dis) * dt / 1000)
    deg = float(params.c_deg_per_mwh * np.sum(p_ch + p_dis) * dt / 1000)
    standby = float(np.sum(trace.plugged * params.standby_kw * buy) * dt / 1000)
    net_cost = import_cost - export_rev + deg + standby
    terminal_credit = float(v_term_per_kwh * E * (soc[-1] - soc[0]))

    deps = trace.departures()
    margins = soc[deps] - trace.soc_req[deps] if len(deps) else np.array([np.inf])
    batt_throughput = float(np.sum(params.eta_ch * p_ch + p_dis / params.eta_dis) * dt)
    lhs = (soc[-1] - soc[0]) * E
    rhs = float(np.sum(params.eta_ch * p_ch - p_dis / params.eta_dis) * dt - np.sum(trace.drive_kwh))

    return {
        "import_kwh": float(np.sum(p_ch) * dt),
        "export_kwh": float(np.sum(p_dis) * dt),
        "import_cost": import_cost,
        "export_revenue": export_rev,
        "degradation_cost": deg,
        "standby_cost": standby,
        "net_cost": net_cost,
        "terminal_credit": terminal_credit,
        "adj_cost": net_cost - terminal_credit,
        "battery_throughput_kwh": batt_throughput,
        "equivalent_full_cycles": batt_throughput / (2 * E),
        "drive_kwh": float(np.sum(trace.drive_kwh)),
        "soc_start": float(soc[0]),
        "soc_end": float(soc[-1]),
        "soc_min_seen": float(soc[1:].min()),
        "soc_max_seen": float(soc[1:].max()),
        "n_departures": int(len(deps)),
        "departures_met": int(np.sum(margins >= -TOL)) if len(deps) else 0,
        "min_departure_margin": float(margins.min()) if len(deps) else None,
        "simultaneous_steps": int(np.sum((p_ch > TOL) & (p_dis > TOL))),
        "energy_balance_error_kwh": float(abs(lhs - rhs)),
    }


def efficiency_check(spec: ConverterSpec, a: Assumptions, params: VehicleParams, p_ch, p_dis) -> dict:
    """How wrong is the fixed-eta assumption for this schedule? Re-prices energy with the
    full spec curve at each step's actual load fraction."""
    p_ch, p_dis = np.asarray(p_ch), np.asarray(p_dis)
    batt = np.sqrt(a.battery.eta_batt_rt)
    cur = spec.efficiency_curve
    lf_c = np.clip(p_ch / params.p_ch_max_kw, cur.load_fraction[0], 1)
    lf_d = np.clip(p_dis / max(params.p_dis_max_kw, 1e-9), cur.load_fraction[0], 1)
    eta_c = np.interp(lf_c, cur.load_fraction, cur.eta_charge) * batt
    eta_d = np.interp(lf_d, cur.load_fraction, cur.eta_discharge) * batt
    err = np.sum((params.eta_ch - eta_c) * p_ch) + np.sum(p_dis * (1 / eta_d - 1 / params.eta_dis))
    err *= params.dt_h
    thr = np.sum(p_ch + p_dis) * params.dt_h
    active = (p_ch > TOL) | (p_dis > TOL)
    full = (p_ch > params.p_ch_max_kw * 0.99) | (p_dis > params.p_dis_max_kw * 0.99)
    return {"energy_error_kwh": float(err),
            "energy_error_pct_of_throughput": float(100 * err / thr) if thr else 0.0,
            "share_active_steps_at_rated": float(full.sum() / active.sum()) if active.any() else 1.0}
