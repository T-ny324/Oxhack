"""App optimisation modes (§8.B.1) and their consistency checks (§12)."""
import numpy as np
import pandas as pd
import pytest

from shared.schema import Assumptions, load_assumptions
from shared.units import utc_index
from v2g.ingest.synthetic import synthetic_prices, synthetic_sessions
from v2g.optimise.daily import (MODES, bill, floor_for_mode, mode_floor_builder, run_daily,
                                trace_from_sessions)
from v2g.optimise.lp import VehicleParams

P = VehicleParams()
TZ = "Europe/London"
START, T = "2025-11-03 10:00", 7 * 48
TOL_GBP = 0.01                       # rolling-horizon re-planning can wobble by pennies


def week(vehicle: int):
    """One synthetic week: ev000 is a commuter, ev001 stays at home most of the day."""
    prices = synthetic_prices(START, 9, P.dt_h, TZ, seed=3)
    index = pd.DatetimeIndex(prices["ts_utc"])
    w = prices["price_wholesale_per_mwh"].to_numpy()
    s = synthetic_sessions(START, 9, 2, TZ, 0.6, seed=3)
    tr = trace_from_sessions(s[s["vehicle_id"] == f"ev{vehicle:03d}"], index, P.dt_h)
    return tr, w + 15, w - 5


def run(mode, target, vehicle=0):
    tr, buy, sell = week(vehicle)
    r = run_daily(P, tr, buy, sell, mode_floor_builder(mode, tr, P, target), 0.6, TZ, execute_until=T)
    return r, tr, bill(P, r, buy, sell)["adj_bill"]


@pytest.mark.parametrize("vehicle", [0, 1])
@pytest.mark.parametrize("target", [0.6, 0.8])
def test_guaranteed_charge_holds_target(vehicle, target):
    """§12: in guaranteed-charge mode SoC >= target whenever the car is needed (every
    departure), in 100 % of schedules, and selling never dips below the target."""
    r, tr, _ = run("guaranteed_charge", target, vehicle)
    soc, eps = r["soc"], 1e-6
    for d, _ in tr.departures:
        if d < T:
            assert soc[d] >= target - eps, f"left at {soc[d]:.3f} < target {target}"
    sold = r["p_dis"] > 0
    assert (soc[1:][sold] >= target - eps).all()


@pytest.mark.parametrize("vehicle", [0, 1])
def test_max_savings_saves_at_least_as_much(vehicle):
    """§12: on identical inputs, max-savings savings >= guaranteed-charge savings."""
    _, _, b_max = run("max_savings", 0.8, vehicle)
    _, _, b_gc = run("guaranteed_charge", 0.8, vehicle)
    assert b_max <= b_gc + TOL_GBP


@pytest.mark.parametrize("vehicle", [0, 1])
def test_raising_target_never_increases_savings(vehicle):
    """§12: raising the target never increases savings (bills never go down)."""
    bills = [run("guaranteed_charge", t, vehicle)[2] for t in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
    assert all(b2 >= b1 - TOL_GBP for b1, b2 in zip(bills, bills[1:])), np.round(bills, 3)


def test_mode_floors():
    plugged = np.array([True] * 4 + [False] * 2 + [True] * 2)
    deps = [(4, 12.0)]                                       # leaves at slot 4 with a 12 kWh trip
    ms = floor_for_mode("max_savings", plugged, deps, P)
    gc = floor_for_mode("guaranteed_charge", plugged, deps, P, target_soc=0.8)
    assert ms[4] == pytest.approx(P.soc_min + 12 / P.e_cap_kwh + 0.10) and ms[1] == P.soc_min
    assert (gc >= ms).all()                                  # superset of constraints
    assert gc[1] == 0.8 and gc[5] == P.soc_min               # target only while plugged in
    with pytest.raises(ValueError):
        floor_for_mode("max_saving", plugged, deps, P)       # typo -> loud error


def test_config_mode_fields():
    a = load_assumptions()
    assert a.optimiser.mode in MODES and 0 <= a.optimiser.target_soc <= 1
    raw = a.model_dump()
    raw["optimiser"]["mode"] = "cheapest"
    with pytest.raises(Exception):
        Assumptions.model_validate(raw)
    del raw["optimiser"]["mode"], raw["optimiser"]["target_soc"]   # older configs still load
    assert Assumptions.model_validate(raw).optimiser.mode == "max_savings"
