"""S0 Config & schema: the interface contract (CONTEXT.md §6), validated on load.

Inputs : config/converter_spec.yaml (hardware-owned), config/assumptions.yaml,
         config/design_targets.yaml.
Outputs: validated Pydantic models. A bad file fails loudly here, not deep in a chart.

Rules from §12: 0 < η < 1 everywhere, load fractions strictly increasing, equal-length
efficiency arrays, SoC limits ordered, the owner's target inside them, dt_h divides a day.
Field names are frozen (§6.2): change values freely, but renaming a field needs a
spec_version bump. Units in names: *_kw, *_kwh, *_per_mwh (£/MWh), fractions 0–1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")      # a typo'd field name is an error, not ignored


# --------------------------------------------------------------------- converter spec
class GridSide(_Strict):
    phases: int = Field(ge=1, le=3)
    v_ac_rms: float = Field(gt=0)
    f_hz: float = Field(gt=0)


class BatterySide(_Strict):
    v_dc_min: float = Field(gt=0)
    v_dc_max: float = Field(gt=0)

    @model_validator(mode="after")
    def _order(self):
        if not self.v_dc_min < self.v_dc_max:
            raise ValueError("v_dc_min must be < v_dc_max")
        return self


class Power(_Strict):
    p_charge_max_kw: float = Field(gt=0)
    p_discharge_max_kw: float = Field(ge=0)
    q_max_kvar: float = Field(ge=0)


class EfficiencyCurve(_Strict):
    load_fraction: list[float]
    eta_charge: list[float]
    eta_discharge: list[float]

    @model_validator(mode="after")
    def _check(self):
        n = len(self.load_fraction)
        if n < 2 or not (len(self.eta_charge) == len(self.eta_discharge) == n):
            raise ValueError("efficiency_curve arrays must have equal length >= 2")
        if any(b <= a for a, b in zip(self.load_fraction, self.load_fraction[1:])):
            raise ValueError("load_fraction must be strictly increasing")
        if not all(0 < x <= 1 for x in self.load_fraction):
            raise ValueError("load_fraction must be in (0, 1]")
        for name in ("eta_charge", "eta_discharge"):
            if not all(0 < x < 1 for x in getattr(self, name)):
                raise ValueError(f"{name} must be strictly between 0 and 1")
        return self


class Dynamics(_Strict):
    response_time_s: float = Field(gt=0)
    ramp_kw_per_s: float = Field(gt=0)


class Cost(_Strict):
    bom_cost: float = Field(ge=0)
    install_cost: float = Field(ge=0)
    unidirectional_baseline_cost: float = Field(ge=0)

    @property
    def premium(self) -> float:
        """What the owner pays extra for bidirectional over a normal charger."""
        return self.bom_cost + self.install_cost - self.unidirectional_baseline_cost


class Uncertainty(_Strict):
    eta_abs: float = Field(ge=0)
    cost_rel: float = Field(ge=0)


class ConverterSpec(_Strict):
    spec_version: str
    provenance: Literal["placeholder", "hand_calc", "simulation", "datasheet", "measured"]
    topology: str
    architecture: Literal["dc_offboard", "ac_onboard"]
    grid_side: GridSide
    battery_side: BatterySide
    power: Power
    efficiency_curve: EfficiencyCurve
    standby_w: float = Field(ge=0)
    dynamics: Dynamics
    cost: Cost
    lifetime_years: int = Field(gt=0)
    uncertainty: Uncertainty


# --------------------------------------------------------------------- assumptions
class Battery(_Strict):
    e_cap_kwh: float = Field(gt=0)
    reserve_kwh: float = Field(ge=0)
    soc_min: float = Field(ge=0, le=1)
    soc_max: float = Field(ge=0, le=1)
    c_deg_per_mwh: float = Field(ge=0)          # £/MWh of battery throughput

    @model_validator(mode="after")
    def _order(self):
        if not self.soc_min < self.soc_max:
            raise ValueError("soc_min must be < soc_max")
        return self


class Owner(_Strict):
    plug_days_per_year: int = Field(ge=0, le=366)


class Tariff(_Strict):
    export_price_fraction: float = Field(gt=0, le=1)   # export price / import price


class OptimiserConfig(_Strict):
    mode: Literal["max_savings", "guaranteed_charge"]
    target_soc: float = Field(ge=0, le=1)


class Assumptions(_Strict):
    region: str
    currency: str
    dt_h: float = Field(gt=0)
    battery: Battery
    owner: Owner
    tariff: Tariff
    optimiser: OptimiserConfig

    @field_validator("dt_h")
    @classmethod
    def _dt(cls, v):
        if abs(24 / v - round(24 / v)) > 1e-9:
            raise ValueError("dt_h must divide a day evenly")
        return v

    @model_validator(mode="after")
    def _target_inside_limits(self):
        b, t = self.battery, self.optimiser.target_soc
        if not b.soc_min <= t <= b.soc_max:
            raise ValueError(f"target_soc {t} must lie within [soc_min, soc_max] = [{b.soc_min}, {b.soc_max}]")
        return self


# --------------------------------------------------------------------- design targets
class DesignTargets(_Strict):
    value_optimal_p_rated_kw: Optional[float] = None
    marginal_value_per_eta_point: Optional[float] = None
    max_cost_for_5yr_payback: Optional[float] = None
    min_response_time_for_freq_services_s: Optional[float] = None


# --------------------------------------------------------------------- loaders
def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_spec(path: Path | str | None = None) -> ConverterSpec:
    return ConverterSpec.model_validate(_load_yaml(Path(path or CONFIG_DIR / "converter_spec.yaml")))


def load_assumptions(path: Path | str | None = None) -> Assumptions:
    return Assumptions.model_validate(_load_yaml(Path(path or CONFIG_DIR / "assumptions.yaml")))


def load_design_targets(path: Path | str | None = None) -> DesignTargets:
    return DesignTargets.model_validate(_load_yaml(Path(path or CONFIG_DIR / "design_targets.yaml")))
