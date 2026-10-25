"""S0 Config & schema: the interface contract (CONTEXT.md §6), validated on load.

Inputs : config/converter_spec.yaml (hardware-owned), config/assumptions.yaml,
         config/design_targets.yaml.
Outputs: validated Pydantic models. A bad file fails loudly here, not deep in a chart.

Also holds Tony's stage-S2/S3 settings models (forecast:, ingest: sections), copied
unchanged from his branch; every forecast/ingest module imports ForecastConfig /
ExogIngestConfig from here.

Rules from §12: 0 < η < 1 everywhere, load fractions strictly increasing, equal-length
efficiency arrays, SoC limits ordered, the owner's target inside them, dt_h divides a day.
Field names are frozen (§6.2): change values freely, but renaming a field needs a
spec_version bump. Units in names: *_kw, *_kwh, *_per_mwh (£/MWh), fractions 0–1.
"""
from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class PricePolicy(_Strict):
    """Policy toggles for the economics (§4, D-l: the "double fee" case)."""
    import_fee_multiplier: float = Field(ge=0)
    export_fee_multiplier: float = Field(ge=0)
    double_fee_policy: bool


# ------------------------------------------------- price forecast + ingest (Tony, S2/S3)
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


class Assumptions(_Strict):
    region: str
    currency: str
    dt_h: float = Field(gt=0)
    battery: Battery
    owner: Owner
    tariff: Tariff
    optimiser: OptimiserConfig
    price_policy: PricePolicy
    forecast: ForecastConfig               # stage S3 (Tony)
    ingest: IngestConfig                   # stage S2 (Tony)

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
