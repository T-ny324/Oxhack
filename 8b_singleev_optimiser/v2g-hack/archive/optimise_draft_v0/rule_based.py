"""Reference strategies that need no optimiser (§8.B ladder step 1).

unmanaged  : plug in -> charge at full power until soc_max. What a normal charger does.
rule_based : "cheapest-hours timer" with a simple V2G rule. On arrival it looks at the
             session's prices (same price information as the learned strategy) and
               * discharges in the k most expensive slots, if the spread beats losses + wear,
               * charges in the cheapest slots, enough to meet the departure SoC.
             A must-charge override guarantees the driver's departure SoC.
"""
from __future__ import annotations

import numpy as np

from .problem import VehicleParams, VehicleTrace


def _clip(params: VehicleParams, soc: float, p_ch: float, p_dis: float) -> tuple[float, float]:
    E, dt = params.e_cap_kwh, params.dt_h
    p_ch = min(p_ch, max(0.0, (params.soc_max - soc) * E / (params.eta_ch * dt)))
    p_dis = min(p_dis, max(0.0, (soc - params.soc_min) * E * params.eta_dis / dt))
    return p_ch, p_dis


def _step_soc(params: VehicleParams, soc, p_ch, p_dis, drive):
    return soc + ((params.eta_ch * p_ch - p_dis / params.eta_dis) * params.dt_h - drive) / params.e_cap_kwh


def _session_end(plugged: np.ndarray, t: int) -> int:
    T = len(plugged)
    rest = np.flatnonzero(~plugged[t:])
    return T if len(rest) == 0 else t + int(rest[0])


def simulate_unmanaged(params: VehicleParams, trace: VehicleTrace):
    T = trace.T
    p_ch, p_dis = np.zeros(T), np.zeros(T)
    soc = trace.soc0
    for t in range(T):
        if trace.plugged[t]:
            p_ch[t], _ = _clip(params, soc, params.p_ch_max_kw, 0.0)
        soc = _step_soc(params, soc, p_ch[t], 0.0, trace.drive_kwh[t])
    return p_ch, p_dis


def simulate_rule(params: VehicleParams, trace: VehicleTrace, buy_fc, sell_fc, k_discharge: int):
    T, E, dt = trace.T, params.e_cap_kwh, params.dt_h
    buy_fc, sell_fc = np.asarray(buy_fc), np.asarray(sell_fc)
    p_ch, p_dis = np.zeros(T), np.zeros(T)
    plan = np.zeros(T)              # +1 charge, -1 discharge, 0 idle
    soc = trace.soc0
    rt = params.eta_ch * params.eta_dis
    for t in range(T):
        if trace.plugged[t] and (t == 0 or not trace.plugged[t - 1]):
            d = _session_end(trace.plugged, t)
            req = trace.soc_req[d] if d < T else max(params.soc_min, trace.soc0)
            slots = np.arange(t, d)
            plan[slots] = 0
            disc = []
            if k_discharge and len(slots) > 1 and params.p_dis_max_kw > 0:
                cheapest = buy_fc[slots].min()
                for s in slots[np.argsort(-sell_fc[slots])][:k_discharge]:
                    margin = sell_fc[s] * rt - cheapest - params.c_deg_per_mwh * (1 + rt)
                    if margin > 0:
                        disc.append(s)
            plan[disc] = -1
            need_kwh = max(0.0, req - soc) * E + len(disc) * params.p_dis_max_kw * dt / params.eta_dis
            n_ch = int(np.ceil(need_kwh / (params.p_ch_max_kw * params.eta_ch * dt) - 1e-9))
            free = np.setdiff1d(slots, disc)
            plan[free[np.argsort(buy_fc[free])][:n_ch]] = 1

        if trace.plugged[t]:
            d = _session_end(trace.plugged, t)
            req = trace.soc_req[d] if d < T else max(params.soc_min, trace.soc0)
            cap_after = params.p_ch_max_kw * params.eta_ch * dt * (d - t - 1)   # kWh still chargeable
            want_ch = params.p_ch_max_kw if plan[t] > 0 else 0.0
            want_dis = params.p_dis_max_kw if plan[t] < 0 else 0.0
            if want_dis:
                after = _step_soc(params, soc, 0.0, want_dis, 0.0)
                if (req - after) * E > cap_after:        # discharging now would strand the driver
                    want_dis = 0.0
            if not want_dis and (req - soc) * E > cap_after - 1e-9:   # must charge now
                want_ch = params.p_ch_max_kw
            p_ch[t], p_dis[t] = _clip(params, soc, want_ch, want_dis)
        soc = _step_soc(params, soc, p_ch[t], p_dis[t], trace.drive_kwh[t])
    return p_ch, p_dis
