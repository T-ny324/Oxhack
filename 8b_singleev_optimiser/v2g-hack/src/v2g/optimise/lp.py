"""Single-vehicle charge / sell optimiser for ONE planning window (§8.B).

See docs/optimisation_problem.md for the plain-language version. This file is that
document turned into code.

THEORY IN ONE PARAGRAPH
-----------------------
We choose, for every half-hour slot t, a charging power P_ch[t] and a selling power P_dis[t].
The bill is linear in those powers (price x energy), and every physical rule (battery
physics, battery limits, charger limits) is a linear equality or inequality. A linear
objective over linear constraints is a Linear Program (LP). An LP's feasible region is a
convex polytope, and a linear objective is always optimised at a corner (vertex) of it.
The SIMPLEX method walks from vertex to neighbouring vertex, always improving the
objective, until no neighbour is better. That vertex is the global optimum (LPs have no
local optima that are not global, because they are convex). We use the HiGHS
implementation of the DUAL simplex (scipy `linprog(method="highs-ds")`).

STANDARD FORM USED HERE
-----------------------
    minimise    c @ x
    subject to  A_eq @ x == b_eq          (battery physics)
                A_ub @ x <= b_ub          (driver's needs: soft SoC floors)
                lb <= x <= ub             (battery and charger limits)

Decision vector x (H = number of slots in the window):

    x = [ P_ch[0..H-1] | P_dis[0..H-1] | SoC[0..H] | short[0..H] ]
          kW             kW              fraction    fraction

    SoC[t]   = state of charge at the START of slot t (so SoC has H + 1 entries,
               SoC[0] is today's measured SoC and is fixed).
    short[t] = how far SoC[t] falls below the driver's floor (a "slack" variable).

THE ONE NON-CONVEX BIT
----------------------
"Don't charge and discharge in the same slot" needs a yes/no variable, which breaks
convexity (the problem becomes a mixed-integer LP, MILP). With ordinary prices the LP
never does it anyway: charging and discharging at once just burns energy through losses
and costs money. It only pays when prices are negative (you get paid to consume, so
burning energy is profitable). So:
  * normal window  -> plain LP, solved by simplex;
  * window containing slots where the loop could pay -> add a binary for those slots only
    and solve the MILP with branch-and-bound (HiGHS solves each branch's LP with dual
    simplex, so it is still simplex underneath).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, linprog, milp

# Penalty for falling below the driver's floor, in £ per kWh of shortfall per slot.
# Electricity costs ~£0.05-0.30/kWh, so £5/kWh is far above anything selling could earn:
# the optimiser will ALWAYS meet the floor if it physically can. The slack only exists so
# that a physically impossible day (e.g. the car arrives nearly empty 30 min before it is
# needed again) still returns a schedule, "charge flat out", plus a reported shortfall,
# instead of crashing with "infeasible".
SHORTFALL_PENALTY_PER_KWH = 5.0


@dataclass(frozen=True)
class VehicleParams:
    """Physical parameters. Efficiencies come from the hardware team's converter spec."""
    e_cap_kwh: float = 60.0     # usable battery size
    soc_min: float = 0.20       # never plan below this (V2G floor)
    soc_max: float = 0.90       # never charge above this
    eta_ch: float = 0.93        # grid -> battery  (converter x battery)
    eta_dis: float = 0.93       # battery -> grid
    p_ch_max_kw: float = 7.4    # charger rating, charging
    p_dis_max_kw: float = 7.4   # charger rating, selling
    dt_h: float = 0.5           # slot length (half-hour settlement periods)


@dataclass
class WindowResult:
    p_ch: np.ndarray          # kW, length H
    p_dis: np.ndarray         # kW, length H
    soc: np.ndarray           # fraction, length H + 1
    shortfall: np.ndarray     # fraction below the floor, length H + 1 (should be ~0)
    bill: float               # £, energy bill for the window at the prices given
    solver: str               # "simplex (LP)" or "branch-and-bound (MILP)"


def loop_could_pay(params: VehicleParams, buy, sell) -> np.ndarray:
    """Slots where charging and selling at the same time could look profitable to an LP.

    Do both at once: buy x kWh (cost buy*x), and sell back as much as possible without
    changing SoC, which is eta_ch*eta_dis*x (earning sell*eta_ch*eta_dis*x).
    It pays if   sell * eta_ch * eta_dis > buy,   which with sell < buy only happens
    when buy < 0. Only these slots need a yes/no variable.
    """
    buy, sell = np.asarray(buy), np.asarray(sell)
    return (buy < 0) | (sell * params.eta_ch * params.eta_dis > buy)


def solve_window(params: VehicleParams, buy, sell, plugged, drive_kwh, floor_soc,
                 soc0: float) -> WindowResult:
    """Optimise one planning window.

    buy, sell   : £/MWh per slot (length H)
    plugged     : bool per slot, True if the car is at the charger
    drive_kwh   : energy the car uses driving in each slot (0 while plugged)
    floor_soc   : the driver's minimum SoC at the start of each slot (length H + 1).
                  This is where the app option (set-a-minimum vs learned habits) enters;
                  see daily.py for how each option builds it.
    soc0        : measured SoC now
    """
    buy = np.asarray(buy, float)
    sell = np.asarray(sell, float)
    plugged = np.asarray(plugged, bool)
    drive = np.asarray(drive_kwh, float)
    floor = np.asarray(floor_soc, float)
    H = len(buy)
    assert len(floor) == H + 1, "floor_soc is per SoC point, so it has H + 1 entries"
    E, dt = params.e_cap_kwh, params.dt_h

    # Positions of each block inside x.
    iC, iD, iS, iU = 0, H, 2 * H, 3 * H + 1        # P_ch, P_dis, SoC, short
    n = 4 * H + 2

    # ---------------------------------------------------------------- objective c @ x
    # Bill in £:  sum_t (buy[t] * P_ch[t] - sell[t] * P_dis[t]) * dt / 1000
    # (/1000 converts £/MWh x kWh into £.) Selling has a negative cost, i.e. it earns money.
    c = np.zeros(n)
    c[iC:iC + H] = buy * dt / 1000
    c[iD:iD + H] = -sell * dt / 1000
    # Shortfall penalty: £/kWh x (fraction x E_cap kWh).
    c[iU:iU + H + 1] = SHORTFALL_PENALTY_PER_KWH * E

    # ---------------------------------------------------------------- bounds lb <= x <= ub
    lb, ub = np.zeros(n), np.zeros(n)
    # Constraint 3, charger limits: power only while plugged in, up to the rating.
    ub[iC:iC + H] = plugged * params.p_ch_max_kw
    ub[iD:iD + H] = plugged * params.p_dis_max_kw
    # Constraint 2, battery limits (upper side hard; the lower side is the soft floor below).
    lb[iS:iS + H + 1] = 0.0
    ub[iS:iS + H + 1] = params.soc_max
    lb[iS] = ub[iS] = np.clip(soc0, 0.0, 1.0)      # SoC[0] is measured, not chosen
    ub[iU:iU + H + 1] = 1.0                        # shortfall is a fraction of the battery
    ub[iU] = 0.0                                   # no shortfall can be "chosen" at t = 0

    # ---------------------------------------------------------------- A_eq: battery physics
    # Constraint 1, one row per slot:
    #   SoC[t+1] - SoC[t] - (eta_ch*dt/E) P_ch[t] + (dt/(eta_dis*E)) P_dis[t] = -drive[t]/E
    # Charging adds energy after losses (x eta_ch). Selling P_dis kW to the grid drains
    # MORE than P_dis from the battery (/ eta_dis). Driving drains drive[t] kWh.
    t = np.arange(H)
    rows = np.repeat(t, 4)
    cols = np.column_stack([iS + t + 1, iS + t, iC + t, iD + t]).ravel()
    vals = np.tile([1.0, -1.0, -params.eta_ch * dt / E, dt / (params.eta_dis * E)], H)
    A_eq = sp.csr_matrix((vals, (rows, cols)), shape=(H, n))
    b_eq = -drive / E

    # ---------------------------------------------------------------- A_ub: driver's floor
    # Constraint 5 (soft): SoC[t] + short[t] >= floor[t]  <=>  -SoC[t] - short[t] <= -floor[t]
    k = np.arange(H + 1)
    A_ub = sp.csr_matrix((np.full(2 * (H + 1), -1.0),
                          (np.r_[k, k], np.r_[iS + k, iU + k])), shape=(H + 1, n))
    b_ub = -np.maximum(floor, 0.0)
    b_ub[0] = 0.0                                  # SoC[0] is fixed; don't penalise it

    # ---------------------------------------------------------------- solve
    risky = np.flatnonzero(plugged & loop_could_pay(params, buy, sell))
    if len(risky) == 0:
        # Pure LP -> dual simplex.
        res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                      bounds=np.column_stack([lb, ub]), method="highs-ds")
        x, solver = res.x, "simplex (LP)"
    else:
        # Constraint 4: add z[t] in {0,1} for the risky slots only.
        #   P_ch[t]  <= P_ch_max * z[t]          (z = 1 means "charging allowed")
        #   P_dis[t] <= P_dis_max * (1 - z[t])   (z = 0 means "selling allowed")
        B = len(risky)
        r = np.arange(B)
        nz = n + B
        pad = lambda M: sp.hstack([M, sp.csr_matrix((M.shape[0], B))]).tocsr()
        A1 = sp.csr_matrix((np.r_[np.ones(B), -np.full(B, params.p_ch_max_kw)],
                            (np.r_[r, r], np.r_[iC + risky, n + r])), shape=(B, nz))
        A2 = sp.csr_matrix((np.r_[np.ones(B), np.full(B, params.p_dis_max_kw)],
                            (np.r_[r, r], np.r_[iD + risky, n + r])), shape=(B, nz))
        cons = [LinearConstraint(pad(A_eq), b_eq, b_eq),
                LinearConstraint(pad(A_ub), -np.inf, b_ub),
                LinearConstraint(A1, -np.inf, 0.0),
                LinearConstraint(A2, -np.inf, params.p_dis_max_kw)]
        integrality = np.r_[np.zeros(n), np.ones(B)]
        res = milp(np.r_[c, np.zeros(B)], constraints=cons, integrality=integrality,
                   bounds=Bounds(np.r_[lb, np.zeros(B)], np.r_[ub, np.ones(B)]))
        x, solver = (res.x[:n] if res.x is not None else None), "branch-and-bound (MILP)"
    if x is None:
        raise RuntimeError(f"optimiser failed: {res.message}")

    # Clean solver round-off (values like 1e-12 kW).
    p_ch = np.where(x[iC:iC + H] > 1e-7, x[iC:iC + H], 0.0)
    p_dis = np.where(x[iD:iD + H] > 1e-7, x[iD:iD + H], 0.0)
    soc = integrate_soc(params, soc0, p_ch, p_dis, drive)   # exact, from the powers
    bill = float(np.sum(buy * p_ch - sell * p_dis) * dt / 1000)
    return WindowResult(p_ch, p_dis, soc, np.clip(x[iU:iU + H + 1], 0, None), bill, solver)


def integrate_soc(params: VehicleParams, soc0: float, p_ch, p_dis, drive_kwh) -> np.ndarray:
    """Replay the battery-physics equation forward. This is the single source of truth for
    reported SoC, so the energy balance in our outputs is exact by construction."""
    dE = (params.eta_ch * np.asarray(p_ch) - np.asarray(p_dis) / params.eta_dis) * params.dt_h \
        - np.asarray(drive_kwh)
    return soc0 + np.r_[0.0, np.cumsum(dE)] / params.e_cap_kwh
