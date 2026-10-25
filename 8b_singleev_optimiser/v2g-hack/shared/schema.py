"""Shared interface contract (§6). Every stage loads config through these models.

Field names and units are frozen: change values freely, but renaming or re-uniting a
field requires bumping `spec_version`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


# --------------------------------------------------------------------------- spec
class GridSide(BaseModel):
    phases: int = Field(ge=1, le=3)
    v_ac_rms: float = Field(gt=0)
    f_hz: float = Field(gt=0)


class BatterySide(BaseModel):
    v_dc_min: float = Field(gt=0)
    v_dc_max: float = Field(gt=0)


class Power(BaseModel):
    p_charge_max_kw: float = Field(gt=0)
    p_discharge_max_kw: float = Field(ge=0)
    q_max_kvar: float = Field(ge=0)


class EfficiencyCurve(BaseModel):
    load_fraction: list[float]
    eta_charge: list[float]
    eta_discharge: list[float]

    @model_validator(mode="after")
    def _check(self):
        n = len(self.load_fraction)
        if not (len(self.eta_charge) == len(self.eta_discharge) == n) or n < 2:
            raise ValueError("efficiency_curve arrays must have equal length >= 2")
        if any(b <= a for a, b in zip(self.load_fraction, self.load_fraction[1:])):
            raise ValueError("load_fraction must be strictly increasing")
        if not all(0 < x <= 1 for x in self.load_fraction):
            raise ValueError("load_fraction must be in (0, 1]")
        for name in ("eta_charge", "eta_discharge"):
            if not all(0 < x < 1 for x in getattr(self, name)):
                raise ValueError(f"{name} must be strictly between 0 and 1")
        return self

    def eta_at(self, load_fraction: float) -> tuple[float, float]:
        """(eta_charge, eta_discharge) linearly interpolated at a load fraction."""
        lf = float(np.clip(load_fraction, self.load_fraction[0], self.load_fraction[-1]))
        return (
            float(np.interp(lf, self.load_fraction, self.eta_charge)),
            float(np.interp(lf, self.load_fraction, self.eta_discharge)),
        )


class Dynamics(BaseModel):
    response_time_s: float = Field(gt=0)
    ramp_kw_per_s: float = Field(gt=0)


class Cost(BaseModel):
    bom_cost: float = Field(ge=0)
    install_cost: float = Field(ge=0)
    unidirectional_baseline_cost: float = Field(ge=0)

    @property
    def premium(self) -> float:
        return self.bom_cost + self.install_cost - self.unidirectional_baseline_cost


class Uncertainty(BaseModel):
    eta_abs: float = Field(ge=0)
    cost_rel: float = Field(ge=0)


class ConverterSpec(BaseModel):
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
class BatteryAssumptions(BaseModel):
    e_cap_kwh: float = Field(gt=0)
    soc_min: float = Field(ge=0, le=1)
    soc_max: float = Field(ge=0, le=1)
    soc_initial: float = Field(ge=0, le=1)
    soc_departure_default: float = Field(ge=0, le=1)
    eta_batt_rt: float = Field(gt=0, le=1)
    battery_cost_per_kwh: float = Field(ge=0)
    cycle_life: float = Field(gt=0)
    dod: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _order(self):
        if not self.soc_min < self.soc_max:
            raise ValueError("soc_min must be < soc_max")
        return self

    @property
    def c_deg_linear_per_mwh(self) -> float:
        """8.C step 1, converted to currency/MWh of throughput."""
        per_kwh = (self.battery_cost_per_kwh * self.e_cap_kwh) / (
            self.cycle_life * 2 * self.e_cap_kwh * self.dod
        )
        return per_kwh * 1000.0


class Tariff(BaseModel):
    import_adder_per_mwh: float
    export_adder_per_mwh: float


class OptimiserConfig(BaseModel):
    eval_days: int = Field(gt=0)
    history_days: int = Field(ge=0)
    eta_load_fraction: float = Field(gt=0, le=1)
    horizon_steps: int = Field(gt=1)
    reopt_every_steps: int = Field(ge=1)
    rule_discharge_slots: int = Field(ge=0)
    price_source: Literal["forecast", "perfect"]


class DateWindow(BaseModel):
    start: str  # local issue date, inclusive (YYYY-MM-DD)
    end: str


class CommuterProfile(BaseModel):
    """Documented default availability profile for backtests without real sessions (hours are local)."""
    kind: Literal["commuter"] = "commuter"
    weekday_depart_h: float = Field(ge=0, lt=24)
    weekday_return_h: float = Field(gt=0, lt=24)
    weekday_trip_kwh: float = Field(ge=0)
    saturday_depart_h: float = Field(ge=0, lt=24)
    saturday_return_h: float = Field(gt=0, lt=24)
    saturday_trip_kwh: float = Field(ge=0)
    sunday_trip_kwh: float = Field(ge=0)  # 0 = stays plugged in all Sunday


class ForecastBacktestConfig(BaseModel):
    """Rolling backtest of the optimiser on the S3 price forecasts (docs/forecast_backtest.md).

    Paths are relative to the repository root (the forecast artefacts live there)."""
    forecast_path: str
    forecast_fallback_path: str
    prices_path: str
    converter_spec_path: str
    series: str
    models: list[str]
    perfect_foresight_name: str = "perfect_foresight"
    window: DateWindow
    smoke_window: DateWindow
    profile: CommuterProfile
    floor_reserve: float = Field(ge=0, le=1)
    out_dir: str
    figures_dir: str


class Assumptions(BaseModel):
    region: str
    currency: str
    timezone_local: str
    dt_h: float = Field(gt=0)
    battery: BatteryAssumptions
    tariff: Tariff
    optimiser: OptimiserConfig
    forecast_backtest: ForecastBacktestConfig | None = None   # optional: only the forecast backtest needs it

    @field_validator("dt_h")
    @classmethod
    def _dt(cls, v):
        if abs(24 / v - round(24 / v)) > 1e-9:
            raise ValueError("dt_h must divide a day evenly")
        return v


# --------------------------------------------------------------------------- load
def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_spec(path: Path | str | None = None) -> ConverterSpec:
    return ConverterSpec.model_validate(_load_yaml(Path(path or CONFIG_DIR / "converter_spec.yaml")))


def load_assumptions(path: Path | str | None = None) -> Assumptions:
    return Assumptions.model_validate(_load_yaml(Path(path or CONFIG_DIR / "assumptions.yaml")))
