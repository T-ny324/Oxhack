"""Consistency checks for the single-vehicle optimiser (§12 of the context file)."""
import numpy as np
import pytest

from v2g.optimise.daily import floor_learned, floor_set_minimum
from v2g.optimise.lp import VehicleParams, integrate_soc, solve_window

P = VehicleParams()


def day(neg=False, seed=0):
    """48 half-hours: cheap night, evening peak, car away 08:00-18:00, 9 kWh commute."""
    rng = np.random.default_rng(seed)
    h = np.arange(48) / 2
    wholesale = 60 - 30 * (h < 6) + 80 * ((h >= 17) & (h < 19.5)) + rng.normal(0, 5, 48)
    if neg:
        wholesale[2:6] = -40
    plugged = ~((h >= 8) & (h < 18))
    drive = np.where(~plugged, 9 / (~plugged).sum(), 0.0)
    return wholesale + 15, wholesale - 5, plugged, drive


def test_toy_example_buys_cheap_sells_peak():
    """The 4-slot example in docs/optimisation_problem.md."""
    p = VehicleParams(e_cap_kwh=10, soc_min=0.0, soc_max=1.0, eta_ch=0.999, eta_dis=0.999,
                      p_ch_max_kw=10, p_dis_max_kw=10, dt_h=0.5)
    buy = np.array([30, 40, 150, 60.0])
    r = solve_window(p, buy, buy, np.ones(4, bool), np.zeros(4), np.zeros(5), soc0=0.0)
    # charge in the cheap slots, sell at the peak, then sell the rest at 60 (> 30-40 paid):
    # the 10 kW charger can only empty half the battery in one half-hour.
    assert r.p_ch[0] > 0 and r.p_ch[1] > 0 and r.p_dis[2] == pytest.approx(10) and r.p_dis[3] > 0
    assert r.bill < 0                                   # it makes money
    assert r.solver == "simplex (LP)"


@pytest.mark.parametrize("neg", [False, True])
def test_physics_limits_and_no_simultaneous(neg):
    buy, sell, plugged, drive = day(neg)
    floor = floor_set_minimum(plugged, 0.6, P)
    r = solve_window(P, buy, sell, plugged, drive, floor, soc0=0.5)
    # energy balance (exact by construction, but checks the solver honoured the physics)
    assert np.allclose(r.soc, integrate_soc(P, 0.5, r.p_ch, r.p_dis, drive), atol=1e-9)
    assert r.soc.max() <= P.soc_max + 1e-6
    assert np.all(r.p_ch[~plugged] == 0) and np.all(r.p_dis[~plugged] == 0)
    assert not np.any((r.p_ch > 1e-6) & (r.p_dis > 1e-6))
    assert r.solver == ("branch-and-bound (MILP)" if neg else "simplex (LP)")


def test_option_a_never_below_minimum_at_departure():
    buy, sell, plugged, drive = day()
    r = solve_window(P, buy, sell, plugged, drive, floor_set_minimum(plugged, 0.6, P), soc0=0.6)
    dep = int(np.flatnonzero(~plugged)[0])
    assert r.soc[dep] >= 0.6 - 1e-6
    assert r.shortfall.max() < 1e-6


def test_option_b_holds_enough_for_predicted_trip():
    buy, sell, plugged, drive = day()
    dep = int(np.flatnonzero(~plugged)[0])
    floor = floor_learned(48, [(dep, 9.0)], P, reserve=0.10)
    r = solve_window(P, buy, sell, plugged, drive, floor, soc0=0.6)
    assert r.soc[dep] >= P.soc_min + 9 / P.e_cap_kwh + 0.10 - 1e-6
    assert r.soc[1:].min() >= P.soc_min - 1e-6          # trip never takes it below the floor


def test_more_freedom_never_costs_more():
    """Learned habits (lower floor) must be at least as cheap as a 60 % minimum, and a
    higher user minimum can never be cheaper."""
    buy, sell, plugged, drive = day()
    dep = int(np.flatnonzero(~plugged)[0])
    bills = [solve_window(P, buy, sell, plugged, drive, floor_set_minimum(plugged, m, P), 0.6).bill
             for m in (0.4, 0.6, 0.8)]
    b = solve_window(P, buy, sell, plugged, drive, floor_learned(48, [(dep, 9.0)], P), 0.6).bill
    assert bills[0] <= bills[1] + 1e-9 <= bills[2] + 2e-9
    assert b <= bills[1] + 1e-9


def test_impossible_day_reports_shortfall_instead_of_crashing():
    """Car plugged in for only 1 slot at 20 % but needs 80 %: charge flat out, report the gap."""
    plugged = np.array([True] + [False] * 3)
    r = solve_window(P, np.full(4, 50.0), np.full(4, 30.0), plugged, np.zeros(4),
                     np.array([0.2, 0.8, 0.2, 0.2, 0.2]), soc0=0.2)
    assert r.p_ch[0] == pytest.approx(P.p_ch_max_kw)
    assert r.shortfall[1] > 0
