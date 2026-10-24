"""Learned strategy: rolling-horizon MPC (§8.B ladder step 3).

At every plugged-in step: take the price forecast for the next H steps and the learned
availability beyond this session, solve the MILP, apply only the first step, repeat.
The car's real SoC is fed back each step, so forecast and habit errors are corrected
continuously rather than accumulating.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from .habits import Habits, predicted_window
from .milp import InfeasibleSchedule, solve
from .problem import VehicleParams, VehicleTrace
from .rule_based import _clip, _step_soc

# forecaster(t, H) -> (buy[H], sell[H]) in currency/MWh, using only information available at t
Forecaster = Callable[[int, int], tuple[np.ndarray, np.ndarray]]


def series_forecaster(buy_fc, sell_fc) -> Forecaster:
    buy_fc, sell_fc = np.asarray(buy_fc), np.asarray(sell_fc)

    def f(t: int, H: int):
        return buy_fc[t:t + H], sell_fc[t:t + H]
    return f


def run_mpc(params: VehicleParams, trace: VehicleTrace, forecaster: Forecaster, habits: Habits,
            horizon: int = 48, reopt_every: int = 1):
    T = trace.T
    p_ch, p_dis = np.zeros(T), np.zeros(T)
    soc = trace.soc0
    plan, t_plan = None, 0
    stats = {"solves": 0, "fallbacks": 0, "binaries": 0}
    for t in range(T):
        if trace.plugged[t]:
            arrived = t == 0 or not trace.plugged[t - 1]
            if plan is None or arrived or (t - t_plan) % reopt_every == 0 or t - t_plan >= len(plan[0]):
                buy_f, sell_f = forecaster(t, horizon)
                a, drive, req = predicted_window(trace, t, len(buy_f), habits, params.soc_min,
                                                 params.soc_max, params.e_cap_kwh)
                v_term = float(np.mean(buy_f)) / params.eta_ch / 1000.0
                try:
                    sol = solve(params, buy_f, sell_f, a, drive, req, soc, v_term, force_binary=True)
                    plan, t_plan = (sol.p_ch, sol.p_dis), t
                    stats["binaries"] += sol.n_binary
                except InfeasibleSchedule:
                    # predicted future can't be met (e.g. odd habits): protect the driver, charge
                    stats["fallbacks"] += 1
                    plan, t_plan = (np.full(1, params.p_ch_max_kw), np.zeros(1)), t
                stats["solves"] += 1
            k = t - t_plan
            p_ch[t], p_dis[t] = _clip(params, soc, plan[0][k], plan[1][k])
        else:
            plan = None
        soc = _step_soc(params, soc, p_ch[t], p_dis[t], trace.drive_kwh[t])
    return p_ch, p_dis, stats
