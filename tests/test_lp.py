"""§12 consistency checks for the app's optimiser (src/v2g/optimise/lp.py), without Streamlit."""
import numpy as np
import pytest

from shared.schema import load_assumptions, load_spec
from shared.units import per_mwh_to_per_kwh
from v2g.optimise import lp

SPEC, A = load_spec(), load_assumptions()
B = A.battery

# Same physical inputs the app builds (spec at rated power, battery from assumptions.yaml)
PHYS = dict(p_ch=SPEC.power.p_charge_max_kw, p_dis=SPEC.power.p_discharge_max_kw,
            eta_ch=SPEC.efficiency_curve.eta_charge[-1], eta_dis=SPEC.efficiency_curve.eta_discharge[-1],
            e_cap=B.e_cap_kwh, reserve_kwh=B.reserve_kwh, soc_min=B.soc_min, soc_max=B.soc_max,
            c_deg=float(per_mwh_to_per_kwh(B.c_deg_per_mwh)))
TRIP = 12.0


def tariff(negative: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """17:00 → 07:30 (29 half-hours): evening peak, shoulder, cheap night. £/MWh."""
    buy = np.r_[np.full(4, 380.0), np.full(8, 220.0), np.full(10, 95.0), np.full(7, 160.0)]
    if negative:
        buy[12:16] = -300.0                                 # a very windy night (−30 p/kWh)
    return buy, A.tariff.export_price_fraction * buy


def solve(mode="guaranteed_charge", target=0.6, always_ready=True, allow_v2g=True, negative=False):
    buy, sell = tariff(negative)
    return lp.solve_schedule(buy=tuple(buy), sell=tuple(sell), trip_kwh=TRIP, mode=mode, target=target,
                             always_ready=always_ready, allow_v2g=allow_v2g, **PHYS)


CASES = [("max_savings", True), ("guaranteed_charge", True), ("guaranteed_charge", False)]


@pytest.mark.parametrize("mode, always_ready", CASES)
def test_energy_balance_per_schedule(mode, always_ready):
    """ΔSoC · E_cap == Σ(η_ch · P_ch − P_dis / η_dis) · Δt, slot by slot, to 1e-6."""
    r = solve(mode, always_ready=always_ready)
    assert r["ok"]
    d_soc = np.diff(r["soc"]) * PHYS["e_cap"]
    flows = (PHYS["eta_ch"] * r["p_ch"] - r["p_dis"] / PHYS["eta_dis"]) * lp.DT_H
    assert np.allclose(d_soc, flows, atol=1e-6)
    # periodic day: the window recharges exactly tomorrow's trip
    assert (r["soc"][-1] - r["soc"][0]) * PHYS["e_cap"] == pytest.approx(TRIP, abs=1e-6)


@pytest.mark.parametrize("mode, always_ready", CASES)
def test_soc_stays_in_bounds(mode, always_ready):
    r = solve(mode, always_ready=always_ready)
    assert r["soc"].min() >= B.soc_min - 1e-9 and r["soc"].max() <= B.soc_max + 1e-9


@pytest.mark.parametrize("mode, always_ready", CASES)
def test_no_simultaneous_charge_and_discharge(mode, always_ready):
    r = solve(mode, always_ready=always_ready)
    assert not ((r["p_ch"] > 1e-6) & (r["p_dis"] > 1e-6)).any()


@pytest.mark.parametrize("mode, always_ready", CASES)
@pytest.mark.parametrize("target", [0.4, 0.6, 0.8])
def test_no_simultaneous_charge_and_discharge_negative_prices(mode, always_ready, target):
    """§12: even when prices go negative (−30 p/kWh). Before the charge-or-sell switch, guaranteed
    charge at an 80 % target charged and sold in the same half-hour in 3 slots."""
    r = solve(mode, target=target, always_ready=always_ready, negative=True)
    assert r["ok"]
    assert not ((r["p_ch"] > 1e-6) & (r["p_dis"] > 1e-6)).any()


def test_charge_or_sell_switch_only_where_needed():
    """Normal prices: no extra yes/no variables (plain LP, same answer as before)."""
    buy, sell = tariff()
    args = (buy / 1000, sell / 1000, PHYS["eta_ch"], PHYS["eta_dis"], PHYS["c_deg"])
    assert len(lp.loop_slots(*args)) == 0
    buy, sell = tariff(negative=True)
    assert list(lp.loop_slots(buy / 1000, sell / 1000, *args[2:])) == [12, 13, 14, 15]


def test_departure_requirements_met():
    """Driver's needs come first: guaranteed → target at departure; max savings → trip + reserve."""
    assert solve("guaranteed_charge", target=0.7)["soc"][-1] >= 0.7 - 1e-9
    need = B.soc_min + (TRIP + B.reserve_kwh) / B.e_cap_kwh
    assert solve("max_savings")["soc"][-1] >= need - 1e-9


def evaluate(mode, target, always_ready=True):
    buy, sell = tariff()
    params = dict(PHYS, trip_kwh=TRIP, arrive_slot=0, depart_slot=len(buy), mode=mode, target=target,
                  always_ready=always_ready, plug_days=A.owner.plug_days_per_year, premium=SPEC.cost.premium)
    return lp.evaluate(params, buy, sell)


@pytest.mark.parametrize("always_ready", [True, False])
@pytest.mark.parametrize("target", [0.4, 0.6, 0.8])
def test_max_savings_saves_at_least_guaranteed(target, always_ready):
    """§12: on identical inputs, max-savings savings ≥ guaranteed-charge savings."""
    r = evaluate("guaranteed_charge", target, always_ready)
    assert r["ok"] and r["v2g_free_yr"] >= r["v2g_yr"] - 1e-6


@pytest.mark.parametrize("always_ready", [True, False])
def test_raising_target_never_increases_savings(always_ready):
    savings = [evaluate("guaranteed_charge", t / 100, always_ready)["v2g_yr"] for t in range(30, 95, 5)]
    assert all(b <= a + 1e-6 for a, b in zip(savings, savings[1:])), np.round(savings, 2)


def test_v2g_beats_smart_beats_normal():
    """Selling back can only add options, and smart charging beats charging on arrival."""
    r = evaluate("guaranteed_charge", 0.6)
    assert r["v2g_yr"] >= r["smart_yr"] - 1e-6 >= -1e-6


def test_unmanaged_replaces_the_trip():
    buy, _ = tariff()
    cost = lp.unmanaged_cost(buy, PHYS["p_ch"], PHYS["eta_ch"], TRIP, 0.0)
    slots = int(np.ceil(TRIP / PHYS["eta_ch"] / (PHYS["p_ch"] * lp.DT_H)))
    assert 0 < cost <= buy[:slots].max() / 1000 * TRIP / PHYS["eta_ch"] + 1e-9
