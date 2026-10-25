"""Run the optimiser day by day, as the product would: plan 38 h, act 24 h (§8.B).

Every day at 10:00 (UK time) a fresh price forecast arrives. We plan from 10:00 today to
midnight tomorrow (~38 h) but only EXECUTE until 10:00 tomorrow, then re-plan with the
car's real SoC.

Why look 14 h past the point we stop executing? A window that ends at 10:00 treats the
world as ending there: it would happily sell the battery down at 09:30, and it never sees
the most valuable commuter trade (charge cheap overnight -> sell at the NEXT evening's
peak) because that trade crosses the 10:00 boundary. Planning to midnight tomorrow
includes tomorrow's evening peak, and the bit we throw away is re-planned anyway.

Also in this file:
  * the two app options for the driver's needs (they only change the SoC floor);
  * the "unmanaged" baseline (a normal charger: plug in, charge flat out until full);
  * the bill accounting used to compare them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from shared.schema import Assumptions, ConverterSpec

from .lp import VehicleParams, integrate_soc, solve_window

ISSUE_HOUR_LOCAL = 10         # forecast issue time (price_forecast_contract.md)


def params_from_config(spec: ConverterSpec, a: Assumptions) -> VehicleParams:
    """Efficiency = converter (hardware spec, read at rated power) x battery (split evenly).
    Arbitrage runs the charger flat out almost all the time, so reading the curve at rated
    power is a good single operating point for a linear model."""
    eta_c, eta_d = spec.efficiency_curve.eta_at(1.0)
    batt = np.sqrt(a.battery.eta_batt_rt)
    return VehicleParams(e_cap_kwh=a.battery.e_cap_kwh, soc_min=a.battery.soc_min,
                         soc_max=a.battery.soc_max, eta_ch=eta_c * batt, eta_dis=eta_d * batt,
                         p_ch_max_kw=spec.power.p_charge_max_kw,
                         p_dis_max_kw=spec.power.p_discharge_max_kw, dt_h=a.dt_h)


# =============================================================================== trace
@dataclass
class Trace:
    """What the car does, slot by slot. Slot t covers [index[t], index[t] + dt)."""
    index: pd.DatetimeIndex     # UTC, start of each slot
    plugged: np.ndarray         # bool
    drive_kwh: np.ndarray       # energy used driving in each slot
    departures: list            # [(slot of first away slot, trip energy kWh)]


def trace_from_sessions(sessions: pd.DataFrame, index: pd.DatetimeIndex, dt_h: float) -> Trace:
    """One vehicle's sessions table -> per-slot trace.

    Snapping is conservative: arrivals round UP to the next slot and departures round DOWN,
    so we never assume the car is plugged in when it might not be. Each trip's energy is
    spread evenly over the slots the car is away.
    """
    T, dt = len(index), pd.Timedelta(hours=dt_h)
    up = lambda ts: int(np.clip(np.ceil((ts - index[0]) / dt), 0, T))
    down = lambda ts: int(np.clip(np.floor((ts - index[0]) / dt), 0, T))
    plugged, drive, deps = np.zeros(T, bool), np.zeros(T), []
    s = sessions.sort_values("arrival_utc").reset_index(drop=True)
    spans = []
    for _, r in s.iterrows():
        a = up(r.arrival_utc)
        d = T if pd.isna(r.departure_utc) else down(r.departure_utc)
        if d > a:
            plugged[a:d] = True
            spans.append((a, d, float(r.trip_kwh_next)))
    for i, (a, d, trip) in enumerate(spans):
        if d >= T:
            continue
        back = spans[i + 1][0] if i + 1 < len(spans) else T
        drive[d:back] += trip / max(back - d, 1)
        deps.append((d, trip))
    return Trace(index, plugged, drive, deps)


# ===================================================================== driver options
# Both options produce floor_soc: the minimum SoC at the start of each slot (H + 1 points).
# The optimiser treats it as a near-hard constraint (heavy penalty, see lp.py).

def floor_set_minimum(plugged: np.ndarray, user_min: float, params: VehicleParams) -> np.ndarray:
    """Option A, "I set a minimum": SoC >= user_min whenever the car is plugged in,
    including the moment it leaves. Selling only ever uses energy above that line.

    If the car ARRIVES below the line (it used energy driving), that can't be helped; the
    penalty makes the optimiser charge back above it as fast as the charger allows.
    """
    H = len(plugged)
    f = np.full(H + 1, params.soc_min)
    # SoC point k sits between slot k-1 and slot k: it is "plugged in" if either side is.
    at_charger = np.r_[plugged, False] | np.r_[False, plugged]
    f[at_charger] = max(user_min, params.soc_min)
    return f


def floor_learned(H: int, departures: list, params: VehicleParams,
                  reserve: float = 0.10) -> np.ndarray:
    """Option B, "learn my habits": at each PREDICTED departure, hold enough for the
    PREDICTED trip plus a reserve:  SoC >= soc_min + trip_kWh / E_cap + reserve.
    Between trips the only floor is soc_min, which gives the optimiser more room to sell.

    `departures` will come from the habits ML model (not built yet). Until then the demo
    passes the true trips ("perfect prediction"), labelled as such.
    """
    f = np.full(H + 1, params.soc_min)
    for k, trip in departures:
        if 0 <= k <= H:
            f[k] = max(f[k], min(params.soc_max,
                                 params.soc_min + trip / params.e_cap_kwh + reserve))
    return f


# ======================================================================= daily loop
FloorBuilder = Callable[[int, int], np.ndarray]    # (start, stop) slots -> floor (H + 1)


def run_daily(params: VehicleParams, trace: Trace, buy_fc, sell_fc, floor_for: FloorBuilder,
              soc0: float, tz: str, execute_until: int | None = None) -> dict:
    """Plan 38 h, act 24 h, repeat. Returns executed powers, SoC and per-day solver info.

    buy_fc / sell_fc are the prices the optimiser BELIEVES (Tony's forecast, or actuals
    for a perfect-foresight run). Bills are always settled later on actual prices.
    """
    T = len(trace.index) if execute_until is None else execute_until
    local = trace.index.tz_convert(tz)
    issues = list(np.flatnonzero((local.hour == ISSUE_HOUR_LOCAL) & (local.minute == 0)))
    issues = [i for i in issues if i < T]
    if not issues or issues[0] != 0:
        issues = [0] + issues                      # data may not start exactly at 10:00
    p_ch, p_dis = np.zeros(T), np.zeros(T)
    soc, days = soc0, []
    for n, i in enumerate(issues):
        act_end = issues[n + 1] if n + 1 < len(issues) else T          # next 10:00
        # plan end = midnight at the end of tomorrow (local), capped at the data we have
        midnight = (local[i].normalize() + pd.Timedelta(days=2)).tz_convert("UTC")
        plan_end = min(int(np.searchsorted(trace.index, midnight)), len(trace.index))
        w = slice(i, plan_end)
        res = solve_window(params, buy_fc[w], sell_fc[w], trace.plugged[w],
                           trace.drive_kwh[w], floor_for(i, plan_end), soc)
        k = act_end - i                                                  # act on 24 h only
        p_ch[i:act_end], p_dis[i:act_end] = res.p_ch[:k], res.p_dis[:k]
        # Real SoC carried into tomorrow's plan. Here reality = plan, because the trace is
        # the truth. With PREDICTED habits use run_daily_vs_reality (plan != reality).
        soc = res.soc[k]
        days.append({"issue_utc": str(trace.index[i]), "plan_slots": plan_end - i,
                     "act_slots": k, "solver": res.solver,
                     "max_shortfall": float(res.shortfall[: k + 1].max())})
    socs = integrate_soc(params, soc0, p_ch, p_dis, trace.drive_kwh[:T])
    return {"p_ch": p_ch, "p_dis": p_dis, "soc": socs, "days": days}


# ================================================================ plan vs reality
# (window start i, window end j) -> what the PLAN believes for slots i..j-1:
#   plugged (bool, j - i), drive_kwh (j - i), floor_soc (j - i + 1)
PlanInputs = Callable[[int, int], tuple]


def run_daily_vs_reality(params: VehicleParams, truth: Trace, buy_fc, sell_fc,
                         plan_inputs: PlanInputs, soc0: float, tz: str,
                         execute_until: int | None = None) -> dict:
    """Like run_daily, but the plan uses PREDICTED habits while reality follows `truth`.

    Re-plan events: every day at 10:00 (new price forecast) AND the moment the car really
    plugs in (the charger sees it, so the plan never has to guess an arrival that has
    already happened). plan_inputs builds the plan's belief at each event.

    Execution step (reality), slot by slot until the next event:
      * car actually away      -> power 0, whatever the plan said;
      * car actually plugged in -> do the planned power, but clamped so SoC stays within
        [soc_min, soc_max] (the real SoC can differ from the planned one because the
        true trips differ from the predicted ones);
      * SoC is replayed with the TRUE trips (truth.drive_kwh).
    Every real departure is checked against what the trip really needed:
      required SoC = soc_min + true trip kWh / E_cap; below that = shortfall (logged).
    """
    T = len(truth.index) if execute_until is None else execute_until
    E, dt = params.e_cap_kwh, params.dt_h
    local = truth.index.tz_convert(tz)
    issue = (local.hour == ISSUE_HOUR_LOCAL) & (local.minute == 0)
    arrive = truth.plugged & ~np.r_[False, truth.plugged[:-1]]       # first slot of a real plug-in
    events = sorted({0} | set(np.flatnonzero(issue | arrive).tolist()))
    events = [e for e in events if e < T]
    p_ch, p_dis = np.zeros(T), np.zeros(T)
    soc, days = soc0, []
    for n, i in enumerate(events):
        act_end = events[n + 1] if n + 1 < len(events) else T
        midnight = (local[i].normalize() + pd.Timedelta(days=2)).tz_convert("UTC")
        plan_end = min(int(np.searchsorted(truth.index, midnight)), len(truth.index))
        plugged_b, drive_b, floor_b = plan_inputs(i, plan_end)
        res = solve_window(params, buy_fc[i:plan_end], sell_fc[i:plan_end], plugged_b,
                           drive_b, floor_b, soc)
        for t in range(i, act_end):                                     # ---- execution
            pc, pd_ = res.p_ch[t - i], res.p_dis[t - i]
            if not truth.plugged[t]:
                pc = pd_ = 0.0
            else:
                room = max(0.0, (params.soc_max - soc) * E)                  # kWh to soc_max
                spare = max(0.0, (soc - params.soc_min) * E)                 # kWh above soc_min
                pc = min(pc, room / (params.eta_ch * dt))
                pd_ = min(pd_, spare * params.eta_dis / dt)
            p_ch[t], p_dis[t] = pc, pd_
            soc += ((params.eta_ch * pc - pd_ / params.eta_dis) * dt - truth.drive_kwh[t]) / E
        days.append({"event_utc": str(truth.index[i]), "on_arrival": bool(arrive[i]),
                     "act_slots": act_end - i, "solver": res.solver})
    socs = integrate_soc(params, soc0, p_ch, p_dis, truth.drive_kwh[:T])
    return {"p_ch": p_ch, "p_dis": p_dis, "soc": socs, "days": days,
            "departures": departure_check(params, truth, socs, T)}


def departure_check(params: VehicleParams, truth: Trace, soc: np.ndarray, T: int) -> list[dict]:
    """At every REAL departure before T: did the car leave with enough for the real trip?"""
    out = []
    for d, trip in truth.departures:
        if d < T:
            need = params.soc_min + trip / params.e_cap_kwh
            out.append({"slot": int(d), "trip_kwh": float(trip), "soc": float(soc[d]),
                        "short_kwh": float(max(0.0, need - soc[d]) * params.e_cap_kwh)})
    return out


def run_unmanaged(params: VehicleParams, trace: Trace, soc0: float, T: int) -> dict:
    """Baseline: a normal (one-way) charger. Plug in -> charge at full power until soc_max."""
    p_ch, soc = np.zeros(T), soc0
    for t in range(T):
        if trace.plugged[t]:
            room_kwh = max(0.0, (params.soc_max - soc) * params.e_cap_kwh)
            p_ch[t] = min(params.p_ch_max_kw, room_kwh / (params.eta_ch * params.dt_h))
        soc += (params.eta_ch * p_ch[t] * params.dt_h - trace.drive_kwh[t]) / params.e_cap_kwh
    p_dis = np.zeros(T)
    return {"p_ch": p_ch, "p_dis": p_dis,
            "soc": integrate_soc(params, soc0, p_ch, p_dis, trace.drive_kwh[:T]), "days": []}


# ======================================================================== accounting
def bill(params: VehicleParams, run: dict, buy, sell) -> dict:
    """Settle a schedule on ACTUAL prices.

    Strategies can end the week with different amounts in the battery (the normal charger
    ends full). To compare fairly we credit energy left over at the week's average import
    price:  adj_bill = bill - (SoC_end - SoC_start) x E_cap x mean(buy) / eta_ch.
    """
    T, dt = len(run["p_ch"]), params.dt_h
    buy, sell = np.asarray(buy)[:T], np.asarray(sell)[:T]
    cost = float(np.sum(buy * run["p_ch"]) * dt / 1000)
    income = float(np.sum(sell * run["p_dis"]) * dt / 1000)
    soc = run["soc"]
    credit = (soc[-1] - soc[0]) * params.e_cap_kwh * float(np.mean(buy)) / params.eta_ch / 1000
    return {"import_cost": cost, "export_income": income, "bill": cost - income,
            "end_energy_credit": float(credit), "adj_bill": cost - income - float(credit),
            "import_kwh": float(run["p_ch"].sum() * dt), "export_kwh": float(run["p_dis"].sum() * dt)}
