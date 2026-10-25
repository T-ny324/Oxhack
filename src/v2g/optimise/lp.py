"""S5 Single-EV optimise — the app's one-night schedule (CONTEXT.md §8.B, §8.B.1).

Inputs : prices for the plugged-in window (£/MWh, buy and sell), converter ratings and
         efficiencies (config/converter_spec.yaml), battery and owner assumptions
         (config/assumptions.yaml), the app mode and target.
Outputs: a schedule (charge / sell kW per half-hour, SoC) and £/day, £/yr, payback.
         TODO(S5): write artefacts/schedules/*.parquet and revenue_per_vehicle.json.

This is the small, fast LP the Streamlit app solves live as the owner drags a slider. It is
separate from the tested rolling-horizon optimiser in 8b_singleev_optimiser/v2g-hack/, which
plans a whole week against learned habits. Moved here from dashboard/app.py unchanged so it can
be imported and tested without Streamlit.

Steady-state ("periodic") day: the car arrives, the optimiser schedules until departure, and
arrival SoC = departure SoC − tomorrow's trip. So every strategy replaces exactly one day's
driving energy, which makes savings vs an unmanaged charger a fair comparison.

App modes = same objective, different constraint blocks (the §8.B.1 registry idea):
  max_savings          departure SoC ≥ floor + trip + reserve
  guaranteed, by dep.  departure SoC ≥ target
  guaranteed, always   "we never sell energy below your target": if the car arrives above
                       target it never drops below it; if it arrives below, it never drops
                       below its arrival level and is at target once the trip has been
                       recharged. Needs one binary (arrived above target?), so MILP.

Units: prices £/MWh in, converted to £/kWh here; power kW; energy kWh; SoC fraction.
c_deg is £/kWh of battery throughput. Sign convention: p_net = P_ch − P_dis, + = charging.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

DT_H = 0.5                      # 30-minute settlement periods (CONTEXT.md §6.3)


def solve_schedule(buy: tuple, sell: tuple, p_ch: float, p_dis: float, eta_ch: float, eta_dis: float,
                   e_cap: float, trip_kwh: float, reserve_kwh: float, soc_min: float, soc_max: float,
                   c_deg: float, mode: str, target: float, always_ready: bool, allow_v2g: bool,
                   dt_h: float = DT_H) -> dict:
    """One plugged-in window as an LP (MILP when a yes/no variable is needed: 'always ready',
    or a slot where charging and selling at once could pay; see `loop_slots`)."""
    buy = np.array(buy) / 1000.0      # £/MWh → £/kWh
    sell = np.array(sell) / 1000.0
    n = len(buy)
    use_z = mode == "guaranteed_charge" and always_ready
    loop = loop_slots(buy, sell, eta_ch, eta_dis, c_deg) if allow_v2g else np.array([], int)
    m = len(loop)
    nv = 3 * n + 2 + m                 # [P_ch(n), P_dis(n), SoC(n+1), z, w(m)]
    ich, idis, isoc, iz, iw = 0, n, 2 * n, 3 * n + 1, 3 * n + 2

    c = np.zeros(nv)
    c[ich:ich + n] = (buy + c_deg) * dt_h
    c[idis:idis + n] = (-sell + c_deg) * dt_h

    rows, lo, hi = [], [], []

    def add(coeffs: dict, lower: float, upper: float) -> None:
        r = np.zeros(nv)
        for k, v in coeffs.items():
            r[k] += v
        rows.append(r); lo.append(lower); hi.append(upper)

    for t in range(n):                 # SoC_{t+1} − SoC_t − (η P_ch − P_dis/η) Δt / E = 0
        add({isoc + t + 1: 1, isoc + t: -1, ich + t: -eta_ch * dt_h / e_cap,
             idis + t: dt_h / (eta_dis * e_cap)}, 0, 0)
    add({isoc: 1, isoc + n: -1}, -trip_kwh / e_cap, -trip_kwh / e_cap)   # periodic day

    lb = np.r_[np.zeros(2 * n), np.full(n + 1, soc_min), 0.0, np.zeros(m)]
    ub = np.r_[np.full(n, p_ch), np.full(n, p_dis if allow_v2g else 0.0), np.full(n + 1, soc_max),
               1.0 if use_z else 0.0, np.ones(m)]

    # Charge OR sell, never both (§12), only in the slots where both at once could pay.
    # w = 1 → charging allowed, w = 0 → selling allowed.
    for k, t in enumerate(loop):
        add({ich + t: 1, iw + k: -p_ch}, -np.inf, 0)            # P_ch  ≤ P_ch_max · w
        add({idis + t: 1, iw + k: p_dis}, -np.inf, p_dis)       # P_dis ≤ P_dis_max · (1 − w)

    ready_slot = None
    if mode == "max_savings":
        lb[isoc + n] = min(soc_max, soc_min + (trip_kwh + reserve_kwh) / e_cap)
    else:
        lb[isoc + n] = target
        if use_z:
            ready_slot = min(n, math.ceil(trip_kwh / (eta_ch * p_ch * dt_h)))
            lb[isoc + ready_slot:isoc + n + 1] = np.maximum(lb[isoc + ready_slot:isoc + n + 1], target)
            add({isoc: 1, iz: -1}, target - 1, np.inf)       # z=1 ⇒ arrived ≥ target
            add({isoc: 1, iz: -1}, -np.inf, target)          # z=0 ⇒ arrived ≤ target
            for t in range(1, n + 1):
                add({isoc + t: 1, iz: -1}, target - 1, np.inf)          # z=1 ⇒ SoC ≥ target
                add({isoc + t: 1, isoc: -1, iz: 1}, 0, np.inf)          # z=0 ⇒ SoC ≥ arrival

    integrality = np.zeros(nv)
    integrality[iz] = 1 if use_z else 0
    integrality[iw:] = 1
    res = milp(c, constraints=LinearConstraint(np.array(rows), lo, hi),
               bounds=Bounds(lb, ub), integrality=integrality)
    if res.status != 0 or res.x is None:
        return {"ok": False, "msg": res.message}
    x = res.x
    pch, pdis, soc = x[ich:ich + n], x[idis:idis + n], x[isoc:isoc + n + 1]
    if use_z and x[iz] > 0.5:
        ready_slot = 0
    return {
        "ok": True, "cost": float(res.fun), "p_net": pch - pdis,    # + charging, − exporting
        "p_ch": pch, "p_dis": pdis, "soc": soc, "ready_slot": ready_slot,
        "exported_kwh": float(pdis.sum() * dt_h), "imported_kwh": float(pch.sum() * dt_h),
    }


def loop_slots(buy_per_kwh, sell_per_kwh, eta_ch: float, eta_dis: float, c_deg: float) -> np.ndarray:
    """Slots where charging and selling in the same half-hour could look profitable to an LP.

    Buy x kWh and sell back what keeps SoC unchanged, η_ch·η_dis·x kWh. Net cost per kWh bought
    = (buy + c_deg) + η_ch·η_dis·(c_deg − sell). If that is negative, the LP would "burn" energy
    through the losses to get paid. With sell ≤ buy this only happens when prices go negative.
    These slots (and only these) get a yes/no variable, so normal days stay a fast plain LP.
    """
    buy, sell = np.asarray(buy_per_kwh), np.asarray(sell_per_kwh)
    return np.flatnonzero((buy + c_deg) + eta_ch * eta_dis * (c_deg - sell) < 0)


def unmanaged_cost(buy: np.ndarray, p_ch: float, eta_ch: float, trip_kwh: float, c_deg: float,
                   dt_h: float = DT_H) -> float:
    """Normal charger: plug in and charge at full power until the trip energy is replaced."""
    need, cost = trip_kwh, 0.0
    for price in buy / 1000.0:
        if need <= 1e-9:
            break
        grid_kwh = min(p_ch * dt_h, need / eta_ch)
        cost += grid_kwh * (price + c_deg)
        need -= grid_kwh * eta_ch
    return cost


def evaluate(params: dict, buy_all: np.ndarray, sell_all: np.ndarray,
             solve: Callable[..., dict] = solve_schedule) -> dict:
    """All three strategies (normal charger, smart charging, V2G) → £/day, £/yr and payback.

    buy_all / sell_all: £/MWh for the whole day's slots; params picks the plugged-in window.
    `solve` lets the app pass a cached wrapper of solve_schedule (st.cache_data).
    """
    a, d = params["arrive_slot"], params["depart_slot"]
    buy, sell = tuple(buy_all[a:d]), tuple(sell_all[a:d])
    dt_h = params.get("dt_h", DT_H)
    common = dict(buy=buy, sell=sell, p_ch=params["p_ch"], p_dis=params["p_dis"],
                  eta_ch=params["eta_ch"], eta_dis=params["eta_dis"], e_cap=params["e_cap"],
                  trip_kwh=params["trip_kwh"], reserve_kwh=params["reserve_kwh"],
                  soc_min=params["soc_min"], soc_max=params["soc_max"], c_deg=params["c_deg"],
                  target=params["target"], always_ready=params["always_ready"], dt_h=dt_h)
    base = unmanaged_cost(np.array(buy), params["p_ch"], params["eta_ch"], params["trip_kwh"],
                          params["c_deg"], dt_h)
    smart = solve(**common, mode=params["mode"], allow_v2g=False)
    v2g = solve(**common, mode=params["mode"], allow_v2g=True)
    v2g_free = solve(**common, mode="max_savings", allow_v2g=True)
    if not (smart["ok"] and v2g["ok"] and v2g_free["ok"]):
        return {"ok": False}
    days = params["plug_days"]
    out = {
        "ok": True, "v2g": v2g, "base_day": base,
        "smart_yr": (base - smart["cost"]) * days,
        "v2g_yr": (base - v2g["cost"]) * days,
        "v2g_free_yr": (base - v2g_free["cost"]) * days,
    }
    out["peace_cost_yr"] = max(0.0, out["v2g_free_yr"] - out["v2g_yr"])
    # The bidirectional premium only buys the V2G increment over smart charging.
    increment = out["v2g_yr"] - out["smart_yr"]
    out["increment_yr"] = increment
    out["payback_yr"] = params["premium"] / increment if increment > 1e-6 else float("inf")
    return out
