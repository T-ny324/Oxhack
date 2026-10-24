from __future__ import annotations

from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Optional


class GridSide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phases: int
    v_ac_rms: float
    f_hz: float


class BatterySide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v_dc_min: float
    v_dc_max: float


class PowerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p_charge_max_kw: float
    p_discharge_max_kw: float
    q_max_kvar: float


class EfficiencyCurve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    load_fraction: List[float]
    eta_charge: List[float]
    eta_discharge: List[float]


class Dynamics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_time_s: float
    ramp_kw_per_s: float


class CostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bom_cost: float
    install_cost: float
    unidirectional_baseline_cost: float


class Uncertainty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eta_abs: float
    cost_rel: float


class ConverterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_version: str
    provenance: str
    topology: str
    architecture: str
    grid_side: GridSide
    battery_side: BatterySide
    power: PowerSpec
    efficiency_curve: EfficiencyCurve
    standby_w: float
    dynamics: Dynamics
    cost: CostSpec
    lifetime_years: int
    uncertainty: Uncertainty


class DesignTargets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value_optimal_p_rated_kw: Optional[float] = None
    marginal_value_per_eta_point: Optional[float] = None
    max_cost_for_5yr_payback: Optional[float] = None
    min_response_time_for_freq_services_s: Optional[float] = None


class Assumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str
    currency: str
    currency_symbol: str
    settlement_period_minutes: int
    dt_h: float
    years: List[int]
    scenarios: Dict[str, Dict[str, float]]
    battery: Dict[str, float]
    participation: Dict[str, float]
    price_policy: Dict[str, float | bool]
