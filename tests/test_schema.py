"""§12 schema checks: the config files validate, and bad values are rejected loudly."""
import numpy as np
import pytest
from pydantic import ValidationError

from shared.schema import (Assumptions, ConverterSpec, load_assumptions, load_design_targets,
                           load_spec)
from shared.units import p_per_kwh_to_per_mwh, per_mwh_to_p_per_kwh, signed_power


def test_spec_validates():
    spec = load_spec()
    assert spec.spec_version == "0.1"
    assert 0 < spec.power.p_charge_max_kw < 100
    ec = spec.efficiency_curve
    assert all(0 < e < 1 for e in ec.eta_charge + ec.eta_discharge)
    assert all(b > a for a, b in zip(ec.load_fraction, ec.load_fraction[1:]))
    assert spec.cost.premium == pytest.approx(600)


def _bad_spec(**changes):
    raw = load_spec().model_dump()
    for path, value in changes.items():
        node = raw
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return raw


@pytest.mark.parametrize("change", [
    {"efficiency_curve__eta_charge": [0.80, 0.88, 0.94, 0.96, 0.96, 1.0]},     # η must be < 1
    {"efficiency_curve__eta_discharge": [0.0, 0.87, 0.93, 0.955, 0.955, 0.95]}, # η must be > 0
    {"efficiency_curve__load_fraction": [0.05, 0.25, 0.10, 0.50, 0.75, 1.0]},   # must increase
    {"efficiency_curve__eta_charge": [0.9, 0.9]},                               # unequal lengths
    {"provenance": "guess"},                                                    # unknown provenance
    {"power__p_charge_max_kw": 0},                                              # must be > 0
])
def test_bad_spec_rejected(change):
    with pytest.raises(ValidationError):
        ConverterSpec.model_validate(_bad_spec(**change))


def test_spec_rejects_typo_fields():
    raw = load_spec().model_dump()
    raw["power"]["p_charge_max_kW"] = raw["power"].pop("p_charge_max_kw")      # wrong case = new field
    with pytest.raises(ValidationError):
        ConverterSpec.model_validate(raw)


def test_assumptions_validate_and_match_the_app_defaults():
    a = load_assumptions()
    assert (a.region, a.currency, a.dt_h) == ("GB", "GBP", 0.5)
    assert (a.battery.e_cap_kwh, a.battery.reserve_kwh, a.battery.soc_min, a.battery.soc_max) == (60, 6, 0.2, 0.9)
    assert a.battery.c_deg_per_mwh == 20                    # = 0.02 £/kWh
    assert a.owner.plug_days_per_year == 300 and a.tariff.export_price_fraction == 0.85
    assert (a.optimiser.mode, a.optimiser.target_soc) == ("guaranteed_charge", 0.6)


@pytest.mark.parametrize("section, key, value", [
    ("battery", "soc_min", 0.95),                  # soc_min must be < soc_max
    ("optimiser", "target_soc", 0.95),             # target must lie within [soc_min, soc_max]
    ("optimiser", "mode", "cheapest"),             # unknown app mode
    ("tariff", "export_price_fraction", 1.2),      # export can't pay more than import here
    (None, "dt_h", 0.7),                           # must divide a day
])
def test_bad_assumptions_rejected(section, key, value):
    raw = load_assumptions().model_dump()
    (raw[section] if section else raw)[key] = value
    with pytest.raises(ValidationError):
        Assumptions.model_validate(raw)


def test_design_targets_load():
    assert load_design_targets().value_optimal_p_rated_kw is None


def test_units_and_sign_convention():
    assert p_per_kwh_to_per_mwh(1) == 10                     # 1 p/kWh = 10 £/MWh
    assert np.allclose(per_mwh_to_p_per_kwh(p_per_kwh_to_per_mwh([3.5, 27])), [3.5, 27])
    assert list(signed_power([7, 0], [0, 5])) == [7, -5]     # + charging, − exporting
