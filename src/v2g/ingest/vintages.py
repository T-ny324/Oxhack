"""Shared schema and cleaning helpers for exogenous forecast VINTAGES.

Every exogenous ingest module (``neso_*``, ``elexon_ndf``, ``elexon_windfor``)
writes the same long table, one parquet per source in ``data/processed/exog/``:

====================  ===================  ===========================================
column                dtype                meaning
====================  ===================  ===========================================
source                str                  e.g. "elexon_ndf"
variable              str                  wind_da_mw | embedded_wind_mw | embedded_solar_mw
                                           | demand_da_mw | wind_neso_da_mw
target_time_utc       datetime64[ns, UTC]  START of the half-hour the forecast is FOR
forecast_made_utc     datetime64[ns, UTC]  when the forecast was made / published
value_mw              float64              forecast, MW (NaN where the source had none)
capacity_mw           float64              installed capacity if the source reports it, else NaN
====================  ===================  ===========================================

Why vintages: at a 10:00 issue only forecasts published before 10:00 are
knowable. Models select, per target period, the latest row with
``forecast_made_utc <= issue`` (:mod:`v2g.forecast.exog`). Actual outturn is
never stored here.

Cleaning rules applied by :func:`finalise_vintages` (same spirit as
``elexon_mid``):

* UTC everywhere; settlement date + period converted with the DST-safe helper
  (:func:`v2g.forecast.timing.settlement_period_to_utc`, SP1 = 00:00 local).
* Rows without a forecast-made time are dropped (they are not vintages).
* Rows whose forecast was made after the target period started are dropped
  (archive artefacts, not forecasts).
* Rows further ahead than ``max_lead_hours`` are dropped (configurable).
* Exact duplicate (variable, target_time_utc, forecast_made_utc) rows are
  dropped keeping the last; counts are logged.
* Values stay NaN where missing; nothing is filled.
* Plausibility: value within the configured [min, max] for the variable and,
  where a capacity is reported, value <= capacity * tolerance. Violations raise.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import random
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from shared.schema import ExogIngestConfig

log = logging.getLogger(__name__)

VINTAGE_COLUMNS = [
    "source",
    "variable",
    "target_time_utc",
    "forecast_made_utc",
    "value_mw",
    "capacity_mw",
]
UTC_DTYPE = "datetime64[ns, UTC]"
DEFAULT_EXOG_DIR = Path("data/processed/exog")
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "oxhack-v2g/0.1 (v2g.ingest exog vintages)"


# ---------------------------------------------------------------------- fetching


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def get_with_retry(
    session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 5,
    backoff_s: float = 1.0,
    timeout_s: float = 120.0,
):
    """GET with exponential backoff on 429/5xx/connection errors; other 4xx raise at once."""
    attempt = 0
    while True:
        try:
            resp = session.get(
                url, params=params, timeout=timeout_s, allow_redirects=True
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code not in RETRY_STATUSES:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} for {url} {params}: {resp.text[:300]}",
                    response=resp,
                )
            reason = f"HTTP {resp.status_code}"
        attempt += 1
        if attempt > max_retries:
            raise RuntimeError(
                f"Giving up on {url} after {max_retries} retries ({reason})"
            )
        delay = backoff_s * 2 ** (attempt - 1) + random.uniform(0, 0.25)
        log.warning(
            "Retry %d/%d for %s in %.1fs (%s)", attempt, max_retries, url, delay, reason
        )
        time.sleep(delay)


def read_gzip_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def write_gzip_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def load_json_rows(text: str) -> list[dict]:
    payload = json.loads(text)
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise TypeError(f"unexpected payload shape: {type(payload).__name__}")
    return data


# ---------------------------------------------------------------------- cleaning


def finalise_vintages(
    df: pd.DataFrame, source: str, cfg: ExogIngestConfig
) -> pd.DataFrame:
    """Apply the cleaning rules from the module docstring and return the contract frame."""
    required = {"variable", "target_time_utc", "forecast_made_utc", "value_mw"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{source}: vintage frame lacks columns {sorted(missing)}")
    out = df.copy()
    out["source"] = source
    if "capacity_mw" not in out:
        out["capacity_mw"] = np.nan
    for col in ("target_time_utc", "forecast_made_utc"):
        series = pd.to_datetime(out[col], utc=True, errors="coerce")
        out[col] = series.astype(UTC_DTYPE)
    out["value_mw"] = pd.to_numeric(out["value_mw"], errors="coerce").astype("float64")
    out["capacity_mw"] = pd.to_numeric(out["capacity_mw"], errors="coerce").astype(
        "float64"
    )
    out["variable"] = out["variable"].astype(str)

    n0 = len(out)
    out = out[out["forecast_made_utc"].notna() & out["target_time_utc"].notna()]
    n_no_made = n0 - len(out)
    if n_no_made:
        log.warning(
            "%s: dropped %d rows without a forecast-made or target time",
            source,
            n_no_made,
        )

    late = out["forecast_made_utc"] > out["target_time_utc"]
    if late.any():
        log.warning(
            "%s: dropped %d rows made after their target started (not forecasts: backcasts or archive artefacts)",
            source,
            int(late.sum()),
        )
        out = out[~late]

    lead_h = (out["target_time_utc"] - out["forecast_made_utc"]) / pd.Timedelta(hours=1)
    far = lead_h > cfg.max_lead_hours
    if far.any():
        log.info(
            "%s: dropped %d rows more than %g h ahead of their target",
            source,
            int(far.sum()),
            cfg.max_lead_hours,
        )
        out = out[~far]

    n1 = len(out)
    out = out.drop_duplicates(
        subset=["variable", "target_time_utc", "forecast_made_utc"], keep="last"
    )
    n_dups = n1 - len(out)
    (log.warning if n_dups else log.info)(
        "%s: dropped %d duplicate (variable, target, made) rows", source, n_dups
    )

    off_grid = out["target_time_utc"] != out["target_time_utc"].dt.floor("30min")
    if off_grid.any():
        raise ValueError(
            f"{source}: {int(off_grid.sum())} target times are not on the half-hour grid"
        )

    assert_plausible(out, source, cfg)
    out = out.sort_values(
        ["variable", "target_time_utc", "forecast_made_utc"], kind="mergesort"
    ).reset_index(drop=True)
    return out[VINTAGE_COLUMNS]


def assert_plausible(df: pd.DataFrame, source: str, cfg: ExogIngestConfig) -> None:
    for variable, group in df.groupby("variable", sort=False):
        bounds = cfg.plausible_mw.get(variable)
        if bounds is None:
            raise ValueError(
                f"{source}: no plausible_mw bounds configured for variable {variable!r}"
            )
        values = group["value_mw"]
        known = values.notna()
        bad = known & ((values < bounds[0]) | (values > bounds[1]))
        if bad.any():
            raise ValueError(
                f"{source}/{variable}: {int(bad.sum())} values outside plausible range {bounds} MW, "
                f"e.g. {values[bad].head(3).tolist()}"
            )
        cap = group["capacity_mw"]
        over = known & cap.notna() & (values > cap * cfg.capacity_tolerance)
        if over.any():
            raise ValueError(
                f"{source}/{variable}: {int(over.sum())} values exceed capacity x {cfg.capacity_tolerance}"
            )


# ------------------------------------------------------------------------ output


def write_vintages(df: pd.DataFrame, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, engine="pyarrow")
    return path


def load_vintages(paths: list[Path | str]) -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in paths]
    if not frames:
        raise FileNotFoundError("no vintage parquet files given")
    out = pd.concat(frames, ignore_index=True)
    for col in ("target_time_utc", "forecast_made_utc"):
        out[col] = out[col].astype(UTC_DTYPE)
    return out


def summarise_vintages(df: pd.DataFrame) -> str:
    """Per-variable rows, span, share of half-hours with at least one vintage, median lead."""
    lines = []
    for variable, g in df.groupby("variable", sort=True):
        targets = g["target_time_utc"]
        span_periods = (
            int((targets.max() - targets.min()) / pd.Timedelta(minutes=30)) + 1
        )
        covered = targets.nunique() / span_periods if span_periods else float("nan")
        lead_h = ((targets - g["forecast_made_utc"]) / pd.Timedelta(hours=1)).median()
        lines.append(
            f"  {variable:18s} rows {len(g):>9,} | targets {targets.min():%Y-%m-%d %H:%M} -> {targets.max():%Y-%m-%d %H:%M} UTC"
            f" | periods with >=1 vintage {100 * covered:5.1f}% | median lead {lead_h:5.1f} h"
            f" | value NaN {100 * g['value_mw'].isna().mean():.2f}%"
        )
    return "\n".join(lines) if lines else "  (no rows)"


# --------------------------------------------------------------------------- CLI


def common_parser(
    prog: str, description: str, default_raw_dir: Path, default_out: Path
) -> argparse.ArgumentParser:
    """Argument parser shared by the exogenous ingest modules."""
    from v2g.ingest.config import DEFAULT_ASSUMPTIONS_PATH

    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument(
        "--start", help="first date, inclusive (default: ingest.exog.start in config)"
    )
    p.add_argument(
        "--end", help="end date, exclusive (default: ingest.exog.end in config)"
    )
    p.add_argument("--refresh", action="store_true", help="re-download even if cached")
    p.add_argument(
        "--raw-dir", default=str(default_raw_dir), help="raw cache directory"
    )
    p.add_argument("--out", default=str(default_out), help="processed vintage parquet")
    p.add_argument(
        "--config", default=str(DEFAULT_ASSUMPTIONS_PATH), help="assumptions yaml"
    )
    p.add_argument("--log-level", default="INFO")
    return p


def resolve_dates(args, cfg: ExogIngestConfig) -> tuple[date, date]:
    start = date.fromisoformat(args.start) if args.start else cfg.start
    end = date.fromisoformat(args.end) if args.end else cfg.end
    if end <= start:
        raise ValueError(f"end {end} must be after start {start}")
    return start, end


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
