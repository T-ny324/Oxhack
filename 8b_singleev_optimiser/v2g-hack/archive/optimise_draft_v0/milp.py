"""Core schedule optimiser: one LP/MILP per call, solved with HiGHS via scipy.

minimise  sum_t [ (p_buy_t + c_deg) * P^ch_t - (p_sell_t - c_deg) * P^dis_t ] * dt / 1000
          - v_term * E_cap * soc_T
s.t.      soc_{t+1} = soc_t + (eta_ch P^ch_t - P^dis_t / eta_dis) dt / E_cap - drive_t / E_cap
          soc_min <= soc_t <= soc_max,   soc_t >= soc_req_t at departures
          0 <= P^ch_t  <= a_t P_ch_max * z_t
          0 <= P^dis_t <= a_t P_dis_max * (1 - z_t),  z_t in {0,1}   (only where needed)

The terminal value v_term (currency per kWh *in the battery*) stops the optimiser dumping
the battery at the end of the horizon. Accounting uses the same v_term, so the optimum of
this problem is exactly the best achievable "adjusted cost" — the inequality
perfect-foresight <= any feasible schedule holds by construction.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, milp

from .problem import VehicleParams, integrate_soc


class InfeasibleSchedule(RuntimeError):
    pass


@dataclass
class Solution:
    p_ch: np.ndarray
    p_dis: np.ndarray
    soc: np.ndarray          # T + 1, re-integrated from powers
    objective: float
    n_binary: int


def needs_binary(params: VehicleParams, buy, sell) -> np.ndarray:
    """Steps where simultaneous charge+discharge could look profitable to a pure LP.

    Burning energy through losses pays when you are paid to import (buy < 0), or when an
    instant charge-discharge loop earns money (sell * eta_ch * eta_dis > buy). Everywhere else
    the LP never does it, so we skip the binary and keep the problem fast.
    """
    buy, sell = np.asarray(buy), np.asarray(sell)
    loop = sell * params.eta_ch * params.eta_dis - buy - params.c_deg_per_mwh * (1 + params.eta_ch * params.eta_dis)
    return (buy < 0) | (loop > 0)


def solve(params: VehicleParams, buy, sell, plugged, drive_kwh, soc_req, soc0: float,
          v_term_per_kwh: float, force_binary: bool = False, time_limit: float = 60.0) -> Solution:
    buy = np.asarray(buy, float)
    sell = np.asarray(sell, float)
    a = np.asarray(plugged, bool)
    drive = np.asarray(drive_kwh, float)
    req = np.asarray(soc_req, float)
    T = len(buy)
    dt, E = params.dt_h, params.e_cap_kwh
    soc0 = float(np.clip(soc0, 0.0, 1.0))

    bin_steps = np.flatnonzero(a & (np.ones(T, bool) if force_binary else needs_binary(params, buy, sell)))
    B = len(bin_steps)
    # variable layout: [p_ch (T) | p_dis (T) | soc (T+1) | z (B)]
    i_ch, i_dis, i_soc, i_z = 0, T, 2 * T, 3 * T + 1
    n = 3 * T + 1 + B

    c = np.zeros(n)
    c[i_ch:i_ch + T] = (buy + params.c_deg_per_mwh) * dt / 1000.0
    c[i_dis:i_dis + T] = (-sell + params.c_deg_per_mwh) * dt / 1000.0
    c[i_soc + T] = -v_term_per_kwh * E

    lb = np.zeros(n)
    ub = np.zeros(n)
    ub[i_ch:i_ch + T] = a * params.p_ch_max_kw
    ub[i_dis:i_dis + T] = a * params.p_dis_max_kw
    soc_lb = np.full(T + 1, params.soc_min)
    soc_ub = np.full(T + 1, params.soc_max)
    has_req = ~np.isnan(req)
    soc_lb[:T][has_req] = np.maximum(params.soc_min, np.minimum(req[has_req], params.soc_max))
    soc_lb[0] = soc_ub[0] = soc0          # initial state is fixed (may sit outside the band)
    lb[i_soc:i_soc + T + 1] = soc_lb
    ub[i_soc:i_soc + T + 1] = soc_ub
    ub[i_z:] = 1.0

    # dynamics: soc_{t+1} - soc_t - k_ch p_ch_t + k_dis p_dis_t = -drive_t / E
    rows = np.repeat(np.arange(T), 4)
    cols = np.column_stack([i_soc + 1 + np.arange(T), i_soc + np.arange(T),
                            i_ch + np.arange(T), i_dis + np.arange(T)]).ravel()
    vals = np.tile([1.0, -1.0, -params.eta_ch * dt / E, dt / (params.eta_dis * E)], T)
    A_dyn = sp.csr_matrix((vals, (rows, cols)), shape=(T, n))
    cons = [LinearConstraint(A_dyn, -drive / E, -drive / E)]

    if B:
        r = np.arange(B)
        # p_ch_t - Pch z <= 0 ;  p_dis_t + Pdis z <= Pdis
        A1 = sp.csr_matrix((np.r_[np.ones(B), -np.full(B, params.p_ch_max_kw)],
                            (np.r_[r, r], np.r_[i_ch + bin_steps, i_z + r])), shape=(B, n))
        A2 = sp.csr_matrix((np.r_[np.ones(B), np.full(B, params.p_dis_max_kw)],
                            (np.r_[r, r], np.r_[i_dis + bin_steps, i_z + r])), shape=(B, n))
        cons += [LinearConstraint(A1, -np.inf, 0.0),
                 LinearConstraint(A2, -np.inf, params.p_dis_max_kw)]

    integrality = np.zeros(n)
    integrality[i_z:] = 1
    res = milp(c, constraints=cons, integrality=integrality, bounds=Bounds(lb, ub),
               options={"time_limit": time_limit, "mip_rel_gap": 1e-6})
    if res.x is None:
        raise InfeasibleSchedule(f"solver status {res.status}: {res.message}")

    p_ch = np.clip(res.x[i_ch:i_ch + T], 0, None)
    p_dis = np.clip(res.x[i_dis:i_dis + T], 0, None)
    p_ch[p_ch < 1e-7] = 0.0
    p_dis[p_dis < 1e-7] = 0.0
    # any residual overlap is solver noise on non-binary steps: net it out (never profitable there)
    both = (p_ch > 0) & (p_dis > 0)
    if both.any():
        net = p_ch[both] - p_dis[both]
        p_ch[both] = np.clip(net, 0, None)
        p_dis[both] = np.clip(-net, 0, None)
    soc = integrate_soc(params, soc0, p_ch, p_dis, drive)
    return Solution(p_ch, p_dis, soc, float(res.fun), B)
