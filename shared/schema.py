from __future__ import annotations

import itertools
from datetime import date
from typing import Literal
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


class LearConfig(BaseModel):
    """LEAR settings: the ``forecast.lear`` subsection of config/assumptions.yaml."""

    model_config = ConfigDict(extra="forbid")
    calibration_windows_days: list[int] = Field(min_length=1)
    recalibrate_every_days: int = Field(ge=1)
    impute_max_gap_periods: int = Field(ge=0)
    lambda_selection: Literal["aic", "bic", "cv"]
    lambda_selection_by_model: dict[str, Literal["aic", "bic", "cv"]] = Field(
        default_factory=dict
    )
    n_jobs: int
    write_component_models: bool

    @field_validator("calibration_windows_days")
    @classmethod
    def _positive_increasing(cls, windows: list[int]) -> list[int]:
        if any(w <= 0 for w in windows) or sorted(set(windows)) != list(windows):
            raise ValueError(
                f"calibration_windows_days must be positive, unique and increasing, got {windows}"
            )
        return windows


class ExogConfig(BaseModel):
    """Exogenous-feature settings for stage S3 (``forecast.exog``)."""

    model_config = ConfigDict(extra="forbid")
    enabled_for_models: list[str]
    variables: list[str] = Field(min_length=1)
    exog_known_lag_minutes: int = Field(ge=0)
    include_residual_load: bool
    sources: list[str] | None = (
        None  # processed exog parquet stems to load; None = every file in the exog dir
    )

    @field_validator("variables")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError(f"exog.variables has duplicates: {value}")
        return value


class GuardConfig(BaseModel):
    """Plausibility bounds (£/MWh) every written forecast value must satisfy."""

    model_config = ConfigDict(extra="forbid")
    lo: float
    hi: float

    @model_validator(mode="after")
    def _ordered(self) -> GuardConfig:
        if self.hi <= self.lo:
            raise ValueError(f"guard.hi ({self.hi}) must exceed guard.lo ({self.lo})")
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
    lear: LearConfig
    exog: ExogConfig
    validation_window: DateWindow
    guard: GuardConfig

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


class ExogIngestConfig(BaseModel):
    """Ingest settings for exogenous forecast vintages (``ingest.exog`` in config/assumptions.yaml)."""

    model_config = ConfigDict(extra="forbid")
    start: date  # first publish/target date, inclusive
    end: date  # exclusive
    timezone: str  # IANA zone of the settlement-period clock
    max_lead_hours: float = Field(
        gt=0
    )  # keep vintages up to this far ahead of their target
    capacity_tolerance: float = Field(
        ge=1.0
    )  # value <= capacity * tolerance where capacity is reported
    plausible_mw: dict[str, list[float]]  # variable -> [min, max]
    sleep_s: float = Field(ge=0)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("plausible_mw")
    @classmethod
    def _ranges(cls, value: dict[str, list[float]]) -> dict[str, list[float]]:
        for name, bounds in value.items():
            if len(bounds) != 2 or bounds[0] >= bounds[1]:
                raise ValueError(
                    f"plausible_mw[{name}] must be [min, max] with min < max, got {bounds}"
                )
        return value

    @model_validator(mode="after")
    def _ordered(self) -> ExogIngestConfig:
        if self.end <= self.start:
            raise ValueError("ingest.exog end must be after start")
        return self


class IngestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exog: ExogIngestConfig


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
    ingest: IngestConfig
