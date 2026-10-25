from __future__ import annotations

import itertools
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    load_fraction: list[float]
    eta_charge: list[float]
    eta_discharge: list[float]


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
    value_optimal_p_rated_kw: float | None = None
    marginal_value_per_eta_point: float | None = None
    max_cost_for_5yr_payback: float | None = None
    min_response_time_for_freq_services_s: float | None = None


class DateWindow(BaseModel):
    """Inclusive range of local calendar dates."""

    model_config = ConfigDict(extra="forbid")
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before start {self.start}")
        return self


class ForecastConfig(BaseModel):
    """Stage S3 settings: the ``forecast:`` section of config/assumptions.yaml."""

    model_config = ConfigDict(extra="forbid")
    series: str
    issue_local_time: str  # "HH:MM" wall-clock in `timezone`
    timezone: str  # IANA zone, resolved with zoneinfo
    price_known_lag_periods: int = Field(ge=0)
    smoke_window: DateWindow
    backtest_window: DateWindow
    quantile_history_days: int = Field(gt=0)
    lead_buckets_h: list[float] = Field(min_length=2)

    @field_validator("issue_local_time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f"issue_local_time must be HH:MM, got {value!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"issue_local_time out of range: {value!r}")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("lead_buckets_h")
    @classmethod
    def _increasing(cls, edges: list[float]) -> list[float]:
        if any(b <= a for a, b in itertools.pairwise(edges)):
            raise ValueError(f"lead_buckets_h must be strictly increasing, got {edges}")
        return edges


class Assumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str
    currency: str
    currency_symbol: str
    settlement_period_minutes: int
    dt_h: float
    years: list[int]
    scenarios: dict[str, dict[str, float]]
    battery: dict[str, float]
    participation: dict[str, float]
    price_policy: dict[str, float | bool]
    forecast: ForecastConfig
