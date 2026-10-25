"""As-of selection of exogenous forecast VINTAGES for the S3 models.

The ingest stage (:mod:`v2g.ingest.vintages`) stores every forecast with two
timestamps: ``target_time_utc`` (the half-hour it is for) and
``forecast_made_utc`` (when it was published). For an issue at time ``t`` a
model may only see, for each target period, the LATEST vintage with
``forecast_made_utc <= t - exog_known_lag_minutes``. Older vintages that cover
the target are used automatically when no newer one exists; if none exists at
all the value is NaN and flagged missing.

:class:`ExogVintages` indexes the combined vintage table once and answers
as-of queries quickly. :meth:`ExogVintages.slot_layout` lays the snapshot out
as local day d and d+1 x 48 slots per variable (the DST convention of
:mod:`v2g.forecast.lear_data`: repeated autumn slots averaged, absent spring
slots interpolated), so the feature vector has a fixed length. Feature names
are ``<variable>_d0_s34`` / ``<variable>_d1_s07`` with the ``_mw`` suffix
dropped, e.g. ``wind_da_d1_s34``.

Derived variable ``residual_load`` = demand - wind - embedded wind - embedded
solar, per slot from the same snapshot, only when ``include_residual_load`` is
set and ``demand_da_mw`` is among the variables (it subtracts whichever of the
other three are configured). Note: NDF national demand is already net of
embedded generation, so this is a feature, not an accounting identity.

Actuals guard: a table without ``forecast_made_utc`` is refused, so actual
outturn can never be passed in as if it were a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast.lear_data import SLOTS, build_day_matrix
from v2g.forecast.timing import FREQ, local_to_utc, tzinfo
from v2g.ingest.vintages import DEFAULT_EXOG_DIR, VINTAGE_COLUMNS

RESIDUAL_LOAD = "residual_load"
DEMAND = "demand_da_mw"
RESIDUAL_SUBTRACT = ("wind_da_mw", "embedded_wind_mw", "embedded_solar_mw")
NS_PER_MINUTE = 60 * 10**9


def require_vintage_table(table: pd.DataFrame) -> None:
    """Raise unless ``table`` carries the vintage schema (in particular forecast_made_utc)."""
    missing = [
        c
        for c in ("variable", "target_time_utc", "forecast_made_utc", "value_mw")
        if c not in table.columns
    ]
    if missing:
        raise ValueError(
            f"exogenous table lacks {missing}; forecasts must carry forecast_made_utc vintages, actual outturn is not allowed"
        )
    for col in ("target_time_utc", "forecast_made_utc"):
        if not (
            pd.api.types.is_datetime64_any_dtype(table[col])
            and table[col].dt.tz is not None
        ):
            raise ValueError(f"exogenous table column {col} must be tz-aware UTC")


def short_name(variable: str) -> str:
    return variable.removesuffix("_mw")


@dataclass
class Snapshot:
    values: (
        pd.DataFrame
    )  # index: 30-min UTC grid over local days d and d+1; columns: series
    missing: pd.DataFrame  # same shape, True where no usable vintage existed


class ExogVintages:
    """Vintage table indexed per variable for as-of queries."""

    def __init__(self, table: pd.DataFrame, cfg: ForecastConfig):
        require_vintage_table(table)
        self.cfg = cfg
        self.variables = list(cfg.exog.variables)
        present = set(table["variable"].unique())
        absent = [v for v in self.variables if v not in present]
        if absent:
            raise ValueError(
                f"configured exog variables missing from the table: {absent}; present: {sorted(present)}"
            )
        self.series = list(self.variables)
        if cfg.exog.include_residual_load and DEMAND in self.variables:
            self.series.append(RESIDUAL_LOAD)
        self.feature_names = [
            f"{short_name(s)}_d{day}_s{slot:02d}"
            for s in self.series
            for day in (0, 1)
            for slot in range(SLOTS)
        ]
        self._index: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for variable in self.variables:
            sub = table.loc[
                table["variable"] == variable,
                ["target_time_utc", "forecast_made_utc", "value_mw"],
            ]
            sub = sub.sort_values(
                ["target_time_utc", "forecast_made_utc"], kind="mergesort"
            )
            self._index[variable] = (
                sub["target_time_utc"]
                .dt.tz_convert(None)
                .to_numpy()
                .astype("datetime64[ns]")
                .astype("int64"),
                sub["forecast_made_utc"]
                .dt.tz_convert(None)
                .to_numpy()
                .astype("datetime64[ns]")
                .astype("int64"),
                sub["value_mw"].to_numpy(dtype="float64"),
            )
        self.n_rows = len(table)

    # ------------------------------------------------------------------ queries

    def _grid(self, issue_utc: pd.Timestamp) -> pd.DatetimeIndex:
        d = issue_utc.tz_convert(tzinfo(self.cfg)).date()
        bounds = local_to_utc(
            pd.DatetimeIndex([pd.Timestamp(d), pd.Timestamp(d + timedelta(days=2))]),
            self.cfg,
        )
        return pd.date_range(
            bounds[0],
            bounds[1],
            freq=FREQ,
            inclusive="left",
            unit="ns",
            name="target_time_utc",
        )

    def _latest(
        self, variable: str, grid: pd.DatetimeIndex, cutoff_ns: int
    ) -> np.ndarray:
        target, made, value = self._index[variable]
        g = grid.tz_convert(None).to_numpy().astype("datetime64[ns]").astype("int64")
        lo, hi = np.searchsorted(target, g[0]), np.searchsorted(
            target, g[-1], side="right"
        )
        t, m, v = target[lo:hi], made[lo:hi], value[lo:hi]
        ok = m <= cutoff_ns
        t, v = (
            t[ok][::-1],
            v[ok][::-1],
        )  # reversed: first occurrence per target = latest made
        uniq, first = np.unique(t, return_index=True)
        out = np.full(len(g), np.nan)
        pos = np.searchsorted(g, uniq)
        hit = (pos < len(g)) & (g[np.minimum(pos, len(g) - 1)] == uniq)
        out[pos[hit]] = v[first[hit]]
        return out

    def asof_snapshot(self, issue_utc) -> Snapshot:
        """Latest usable vintage per target over local days d and d+1 for every configured series."""
        issue_utc = pd.Timestamp(issue_utc).tz_convert("UTC")
        cutoff_ns = (
            int(issue_utc.tz_convert(None).to_datetime64().astype("int64"))
            - self.cfg.exog.exog_known_lag_minutes * NS_PER_MINUTE
        )
        grid = self._grid(issue_utc)
        values = pd.DataFrame(
            {v: self._latest(v, grid, cutoff_ns) for v in self.variables}, index=grid
        )
        if RESIDUAL_LOAD in self.series:
            residual = values[DEMAND].copy()
            for other in RESIDUAL_SUBTRACT:
                if other in values:
                    residual = residual - values[other]
            values[RESIDUAL_LOAD] = residual
        return Snapshot(values=values, missing=values.isna())

    def slot_layout(self, issue_utc) -> np.ndarray:
        """Fixed-length vector: for each series, local day d then d+1, 48 slots each."""
        issue_utc = pd.Timestamp(issue_utc).tz_convert("UTC")
        snap = self.asof_snapshot(issue_utc)
        d = issue_utc.tz_convert(tzinfo(self.cfg)).date()
        parts = []
        for s in self.series:
            matrix = build_day_matrix(snap.values[s].rename("actual"), self.cfg)
            matrix = matrix.reindex([d, d + timedelta(days=1)])
            parts.append(matrix.to_numpy(dtype="float64").ravel())
        return np.concatenate(parts)


def load_exog_table(
    cfg: ForecastConfig, exog_dir: Path | str = DEFAULT_EXOG_DIR
) -> pd.DataFrame:
    """Combined vintage table from the processed exog parquet files (configured sources or all)."""
    exog_dir = Path(exog_dir)
    if cfg.exog.sources:
        paths = [exog_dir / f"{src}.parquet" for src in cfg.exog.sources]
    else:
        paths = sorted(exog_dir.glob("*.parquet"))
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"exog parquet files not found: {missing}; run `make data-exog`"
        )
    if not paths:
        raise FileNotFoundError(f"no exog parquet files in {exog_dir}")
    table = pd.concat(
        [pd.read_parquet(p, columns=VINTAGE_COLUMNS) for p in paths], ignore_index=True
    )
    require_vintage_table(table)
    return table[table["variable"].isin(cfg.exog.variables)].reset_index(drop=True)


def load_exog(
    cfg: ForecastConfig, exog_dir: Path | str = DEFAULT_EXOG_DIR
) -> ExogVintages:
    return ExogVintages(load_exog_table(cfg, exog_dir), cfg)


def asof_snapshot(issue_utc, cfg: ForecastConfig, vintages: ExogVintages) -> Snapshot:
    """Functional wrapper around :meth:`ExogVintages.asof_snapshot`."""
    if vintages.cfg is not cfg and vintages.cfg != cfg:
        raise ValueError("vintages were indexed with a different config")
    return vintages.asof_snapshot(issue_utc)
