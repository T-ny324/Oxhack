"""Ingest NESO's "Historic Day Ahead Wind Forecasts" archive -> variable
``wind_neso_da_mw``.

Run
---
::

    python -m v2g.ingest.neso_wind_forecast [--start 2023-01-01 --end 2026-10-02] [--refresh]
    make data-exog

Source (inspected 2026-10-25): NESO Data Portal dataset ``day-ahead-wind-forecast``,
resource "Historic Day Ahead Wind Forecasts" (one CSV, ~10 MB, 2018-04 onwards,
download URL resolved via ``package_show`` because it changes daily).

* Columns ``Datetime_GMT, Date, Settlement_period, Capacity, Incentive_forecast,
  Forecast_Timestamp``. ``Datetime_GMT`` is the UTC period START (no "Z"
  suffix), ``Date``/``Settlement_period`` the local settlement labels (periods
  run to 50 on the autumn change day), ``Capacity`` and ``Incentive_forecast``
  are MW, ``Forecast_Timestamp`` is when the forecast was made. The timestamp
  has no zone marker; it is read as UTC, which is the conservative choice (if
  it were London time the true UTC instant would be earlier, never later).
* Normally ONE vintage per target day, made at ~08:49 UTC the day before
  (2024: 99.7% of rows). At a 10:00 London issue that vintage covers all of
  Block B and the previous day's covers Block A.
* DATA QUALITY: the archive's stamps are unreliable in the backtest period.
  A share of rows is stamped months after the target (dropped as artefacts),
  and from 2025-09 the "day-ahead" rows are increasingly stamped at 08:45 on
  the target day itself (2025-11: 93% of days; 2026-05 onwards: 100%), which
  cannot be the real publication time (the live file is modified ~08:40 UTC
  the day before). Read literally, that leaves no usable vintage for tomorrow.
  The vintages are stored exactly as published, so this variable is kept OUT
  of the model (``forecast.exog.variables``); :mod:`v2g.ingest.elexon_windfor`
  provides ``wind_da_mw`` with proper publish times instead.

Output: ``data/processed/exog/neso_wind_forecast.parquet`` with ``capacity_mw``.
Raw cache: ``data/raw/neso_wind_forecast/historic_day_ahead_wind_forecasts.csv.gz``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from shared.schema import ExogIngestConfig
from v2g.forecast.timing import settlement_period_to_utc
from v2g.ingest.config import load_exog_ingest_config
from v2g.ingest.vintages import (
    DEFAULT_EXOG_DIR,
    common_parser,
    configure_logging,
    finalise_vintages,
    get_with_retry,
    make_session,
    resolve_dates,
    summarise_vintages,
    write_gzip_text,
    write_vintages,
)

log = logging.getLogger(__name__)

SOURCE = "neso_wind_forecast"
VARIABLE = "wind_neso_da_mw"
DATASET_ID = "day-ahead-wind-forecast"
RESOURCE_NAME = "Historic Day Ahead Wind Forecasts"
PACKAGE_SHOW_URL = "https://api.neso.energy/api/3/action/package_show"
DEFAULT_RAW_DIR = Path("data/raw/neso_wind_forecast")
DEFAULT_OUT_PATH = DEFAULT_EXOG_DIR / f"{SOURCE}.parquet"
ARCHIVE_NAME = "historic_day_ahead_wind_forecasts.csv.gz"


def resource_url(session, raw_dir: Path, refresh: bool = False) -> str:
    raw_dir = Path(raw_dir)
    cache = raw_dir / "package_show.json"
    if cache.exists() and not refresh:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    else:
        payload = get_with_retry(
            session, PACKAGE_SHOW_URL, params={"id": DATASET_ID}
        ).json()
        raw_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")
    for res in payload["result"]["resources"]:
        if res.get("name") == RESOURCE_NAME:
            return res["url"]
    raise FileNotFoundError(
        f"{SOURCE}: resource {RESOURCE_NAME!r} not found in dataset {DATASET_ID}"
    )


def parse_csv(path: Path, cfg: ExogIngestConfig) -> pd.DataFrame:
    df = pd.read_csv(path)
    target = pd.to_datetime(df["Datetime_GMT"], utc=True, errors="coerce")
    expected = settlement_period_to_utc(
        df["Date"], df["Settlement_period"].astype("int64"), cfg.timezone
    )
    mismatch = target.notna() & (target.to_numpy() != expected.to_numpy())
    if mismatch.any():
        log.warning(
            "%d rows where Datetime_GMT disagrees with Date/Settlement_period; keeping Datetime_GMT",
            int(mismatch.sum()),
        )
    return pd.DataFrame(
        {
            "variable": VARIABLE,
            "target_time_utc": target,
            "forecast_made_utc": pd.to_datetime(
                df["Forecast_Timestamp"], utc=True, errors="coerce"
            ),  # read as UTC (see docstring)
            "value_mw": pd.to_numeric(df["Incentive_forecast"], errors="coerce"),
            "capacity_mw": pd.to_numeric(df["Capacity"], errors="coerce"),
        }
    )


def coverage_by_month(out: pd.DataFrame, cfg: ExogIngestConfig) -> pd.Series:
    """Share of target days (local) with a vintage made before the target day started."""
    local_day = out["target_time_utc"].dt.tz_convert(cfg.timezone).dt.normalize()
    day_start_utc = local_day.dt.tz_convert("UTC")
    before = out["forecast_made_utc"] < day_start_utc
    per_day = before.groupby(local_day.dt.date).any()
    return per_day.groupby(pd.to_datetime(per_day.index).to_period("M")).mean()


def run(
    start: date,
    end: date,
    cfg: ExogIngestConfig,
    *,
    raw_dir=DEFAULT_RAW_DIR,
    out_path=DEFAULT_OUT_PATH,
    refresh=False,
    session=None,
) -> pd.DataFrame:
    raw_dir = Path(raw_dir)
    path = raw_dir / ARCHIVE_NAME
    if not path.exists() or refresh:
        own = session is None
        session = session or make_session()
        try:
            url = resource_url(session, raw_dir, refresh)
            log.info("%s: downloading %s", SOURCE, RESOURCE_NAME)
            write_gzip_text(path, get_with_retry(session, url).text)
        finally:
            if own:
                session.close()
    raw = parse_csv(path, cfg)
    lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    raw = raw[(raw["target_time_utc"] >= lo) & (raw["target_time_utc"] < hi)]
    out = finalise_vintages(raw, SOURCE, cfg)
    write_vintages(out, out_path)
    log.info("Wrote %s (%d rows)", out_path, len(out))
    return out


def main(argv: list[str] | None = None) -> None:
    args = common_parser(
        "python -m v2g.ingest.neso_wind_forecast",
        "NESO historic day-ahead wind forecast vintages",
        DEFAULT_RAW_DIR,
        DEFAULT_OUT_PATH,
    ).parse_args(argv)
    configure_logging(args.log_level)
    cfg = load_exog_ingest_config(args.config)
    start, end = resolve_dates(args, cfg)
    out = run(
        start,
        end,
        cfg,
        raw_dir=Path(args.raw_dir),
        out_path=args.out,
        refresh=args.refresh,
    )
    print(f"{SOURCE}: targets {start}..{end} (exclusive)")
    print(summarise_vintages(out))
    cov = coverage_by_month(out, cfg)
    print(
        "  share of target days with a vintage made before the day began, by month (last 15):"
    )
    print("  " + ", ".join(f"{p}: {v:.0%}" for p, v in cov.tail(15).items()))
    print(f"  written to {args.out}")


if __name__ == "__main__":
    main()
