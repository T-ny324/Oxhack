#!/usr/bin/env python3
"""Cross-check the C dynamic-programming scheduler against an exact continuous LP.

Usage: python3 tools/check_against_lp.py [days]   (needs numpy+scipy; builds build-host/plan_dump itself if missing)
The LP uses the same prices, efficiencies, SoC limits, degradation cost and
departure requirement as src/scheduler.c, so the gap is purely the DP's
0.5%-SoC grid and its integer step limits.
"""
import os, subprocess, sys, io
import numpy as np
from scipy.optimize import linprog

E, SOC_MIN, SOC_MAX, REQ = 60.0, 0.20, 0.90, 0.80
PMAX, ETA_C, ETA_D, DT = 7.4, 0.955, 0.95, 0.5
DEG = 6000.0 / (3000 * 2 * E * 0.7)

def load(day):
    if not os.path.exists("build-host/plan_dump"):
        subprocess.check_call(["make", "-C", "host", "plan", "DAY=0"], stdout=subprocess.DEVNULL)
    out = subprocess.check_output(["build-host/plan_dump", str(day), "--csv"], text=True)
    lines = out.strip().splitlines()
    net = float(lines[0].split("net_v2g=")[1].split()[0])
    standby = float(lines[0].split("standby=")[1])
    rows = np.genfromtxt(io.StringIO("\n".join(lines[1:])), delimiter=",", names=True)
    soc0 = float(rows["soc"][0])
    return net, standby, rows, soc0

def lp(rows, soc0):
    m = rows["plugged"] > 0.5
    D = int(m.sum()); buy = rows["buy"][:D] / 1000; sell = rows["sell"][:D] / 1000
    # x = [pc(0..D-1), pd(0..D-1)], grid kW
    c = np.concatenate([buy * DT + DEG * ETA_C * DT,         # minimise cost
                        -sell * DT + DEG * DT / ETA_D])
    L = np.tril(np.ones((D, D)))
    dsoc_c = ETA_C * DT / E * L
    dsoc_d = -DT / (ETA_D * E) * L
    A = np.block([[dsoc_c, dsoc_d], [-dsoc_c, -dsoc_d]])
    b = np.concatenate([np.full(D, SOC_MAX - soc0), np.full(D, soc0 - SOC_MIN)])
    # departure requirement: soc_D >= REQ
    A = np.vstack([A, np.concatenate([-dsoc_c[-1], -dsoc_d[-1]])])
    b = np.concatenate([b, [soc0 - REQ]])
    res = linprog(c, A_ub=A, b_ub=b, bounds=[(0, PMAX)] * (2 * D), method="highs")
    return -res.fun

days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
print("day   DP net   LP net   gap(GBP)  gap(%)")
for d in range(days):
    net, standby, rows, soc0 = load(d)
    lp_net = lp(rows, soc0) - standby
    print(f"{d:3d}  {net:8.3f} {lp_net:8.3f}  {lp_net-net:8.3f}  {abs(lp_net-net)/max(abs(lp_net),1e-9)*100:6.1f}")
