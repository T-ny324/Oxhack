"""Ingest NESO embedded WIND and SOLAR forecast vintages -> variables
``embedded_wind_mw`` and ``embedded_solar_mw``.

Run
---
::

    python -m v2g.ingest.neso_embedded_forecast [--start 2023-01-01 --end 2026-10-02] [--refresh]
    make data-exog

Source (inspected 2026-10-25): NESO Data Portal dataset
``embedded-wind-and-solar-forecasts`` (CKAN,
``https://api.neso.energy/api/3/action/package_show?id=...``). The yearly
"Embedded Solar and Wind Forecast Archive <year>" CSV resources carry a
forecast-made time; the live "Embedded Solar and Wind Forecast" file does not
and is therefore NOT used.

* Archive columns: ``DATE_GMT, TIME_GMT, SETTLEMENT_DATE, SETTLEMENT_PERIOD,
  EMBEDDED_WIND_FORECAST, EMBEDDED_WIND_CAPACITY, EMBEDDED_SOLAR_FORECAST,
  EMBEDDED_SOLAR_CAPACITY, Forecast_Datetime`` (2025+ also ``source_file``).
  Values are MW. ``DATE_GMT``/``TIME_GMT`` give the period END in UTC, so the
  target START is derived from the settlement date and period with the
  DST-safe helper and cross-checked against ``DATE_GMT + TIME_GMT - 30 min``.
  ``Forecast_Datetime`` (UTC, "Z") is the forecast-made time.
* Hourly publications at hh:12 UTC, each covering about 13 days ahead
  (624 half-hours). Only rows within ``max_lead_hours`` (48 h) of their target
  are kept: ~2,300 rows per day instead of ~15,000. At a 10:00 London issue the
  latest vintage is the 09:12 UTC publication in winter and 08:12 UTC in
  summer; both cover all of Block A and Block B.
* Files are ~650 MB per year (2023-2025 are direct CSV downloads behind a
  redirect; 2026 is a datastore dump). They are cached gzipped and parsed in
  chunks. History back to 2019; this module reads the years in the configured
  range.

Output: ``data/processed/exog/neso_embedded_forecast.parquet`` with
``capacity_mw`` from the file. Raw cache:
``data/raw/neso_embedded_forecast/embedded_archive_<year>.csv.gz`` plus the
``package_show.json`` used to resolve the download URLs.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import time
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
    write_vintages,
)

log = logging.getLogger(__name__)

SOURCE = "neso_embedded_forecast"
DATASET_ID = "embedded-wind-and-solar-forecasts"
RESOURCE_PREFIX = "Embedded Solar and Wind Forecast Archive "
PACKAGE_SHOW_URL = "https://api.neso.energy/api/3/action/package_show"
DEFAULT_RAW_DIR = Path("data/raw/neso_embedded_forecast")
DEFAULT_OUT_PATH = DEFAULT_EXOG_DIR / f"{SOURCE}.parquet"
VARIABLES = {
    "embedded_wind_mw": ("EMBEDDED_WIND_FORECAST", "EMBEDDED_WIND_CAPACITY"),
    "embedded_solar_mw": ("EMBEDDED_SOLAR_FORECAST", "EMBEDDED_SOLAR_CAPACITY"),
}
USECOLS = [
    "DATE_GMT",
    "TIME_GMT",
    "SETTLEMENT_DATE",
    "SETTLEMENT_PERIOD",
    "EMBEDDED_WIND_FORECAST",
    "EMBEDDED_WIND_CAPACITY",
    "EMBEDDED_SOLAR_FORECAST",
    "EMBEDDED_SOLAR_CAPACITY",
    "Forecast_Datetime",
]
CHUNK_ROWS = 400_000


def archive_path(raw_dir: Path, year: int) -> Path:
    return Path(raw_dir) / f"embedded_archive_{year}.csv.gz"


def resource_urls(session, raw_dir: Path, refresh: bool = False) -> dict[int, str]:
    """Year -> download URL of the archive resources, via a cached package_show call."""
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
    urls = {}
    for res in payload["result"]["resources"]:
        name = res.get("name") or ""
        if name.startswith(RESOURCE_PREFIX) and name[len(RESOURCE_PREFIX) :].isdigit():
            urls[int(name[len(RESOURCE_PREFIX) :])] = res["url"]
    return urls


def download_archive(session, url: str, path: Path) -> None:
    """Stream a CSV resource into a gzipped cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with session.get(url, stream=True, timeout=600, allow_redirects=True) as resp:
        resp.raise_for_status()
        with gzip.open(tmp, "wb") as fh:
            shutil.copyfileobj(resp.raw, fh, length=1 << 20)
    tmp.replace(path)


def _parse_time_gmt(time_gmt: pd.Series) -> pd.Series:
    text = time_gmt.astype(str).str.strip()
    text = text.where(text.str.len() != 5, text + ":00")  # "02:30" -> "02:30:00"
    return pd.to_timedelta(text, errors="coerce")


def chunk_to_frame(chunk: pd.DataFrame, cfg: ExogIngestConfig) -> pd.DataFrame:
    """One CSV chunk -> long vintage rows for both variables, lead-filtered early."""
    chunk = chunk[chunk["Forecast_Datetime"].notna()]
    if chunk.empty:
        return pd.DataFrame(
            columns=[
                "variable",
                "target_time_utc",
                "forecast_made_utc",
                "value_mw",
                "capacity_mw",
            ]
        )
    made = pd.to_datetime(chunk["Forecast_Datetime"], utc=True, errors="coerce")
    target = settlement_period_to_utc(
        chunk["SETTLEMENT_DATE"],
        chunk["SETTLEMENT_PERIOD"].astype("int64"),
        cfg.timezone,
    )
    end_gmt = pd.to_datetime(
        chunk["DATE_GMT"], utc=True, errors="coerce"
    ) + _parse_time_gmt(chunk["TIME_GMT"])
    check = end_gmt.notna()
    mismatch = check & (
        (end_gmt - pd.Timedelta(minutes=30)).to_numpy() != target.to_numpy()
    )
    if mismatch.any():
        log.warning(
            "%d rows where DATE_GMT/TIME_GMT disagrees with settlement date/period; keeping the settlement-derived start",
            int(mismatch.sum()),
        )
    lead_h = (pd.Series(target, index=chunk.index) - made) / pd.Timedelta(hours=1)
    keep = (lead_h <= cfg.max_lead_hours) & made.notna()
    frames = []
    for variable, (value_col, cap_col) in VARIABLES.items():
        frames.append(
            pd.DataFrame(
                {
                    "variable": variable,
                    "target_time_utc": pd.Series(target, index=chunk.index)[
                        keep
                    ].to_numpy(),
                    "forecast_made_utc": made[keep].to_numpy(),
                    "value_mw": pd.to_numeric(
                        chunk.loc[keep, value_col], errors="coerce"
                    ).to_numpy(),
                    "capacity_mw": pd.to_numeric(
                        chunk.loc[keep, cap_col], errors="coerce"
                    ).to_numpy(),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def parse_archive(path: Path, cfg: ExogIngestConfig) -> pd.DataFrame:
    frames = []
    for i, chunk in enumerate(
        pd.read_csv(
            path, usecols=lambda c: c in USECOLS, chunksize=CHUNK_ROWS, low_memory=False
        ),
        1,
    ):
        frames.append(chunk_to_frame(chunk, cfg))
        if i % 5 == 0:
            log.info("%s: %d chunks parsed from %s", SOURCE, i, path.name)
    return (
        pd.concat(frames, ignore_index=True)
        if frames
        else chunk_to_frame(pd.DataFrame(columns=USECOLS), cfg)
    )


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
    own = session is None
    session = session or make_session()
    years = list(range(start.year, (end - pd.Timedelta(days=1)).year + 1))
    frames = []
    try:
        urls = None
        for year in years:
            path = archive_path(raw_dir, year)
            if not path.exists() or refresh:
                urls = (
                    urls
                    if urls is not None
                    else resource_urls(session, raw_dir, refresh)
                )
                if year not in urls:
                    raise FileNotFoundError(
                        f"{SOURCE}: no archive resource for {year} in dataset {DATASET_ID}"
                    )
                log.info("%s: downloading archive %d", SOURCE, year)
                download_archive(session, urls[year], path)
                if cfg.sleep_s:
                    time.sleep(cfg.sleep_s)
            log.info("%s: parsing %s", SOURCE, path.name)
            frames.append(parse_archive(path, cfg))
    finally:
        if own:
            session.close()
    raw = pd.concat(frames, ignore_index=True)
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC")
    raw = raw[(raw["forecast_made_utc"] >= lo) & (raw["forecast_made_utc"] < hi)]
    out = finalise_vintages(raw, SOURCE, cfg)
    write_vintages(out, out_path)
    log.info("Wrote %s (%d rows)", out_path, len(out))
    return out


def main(argv: list[str] | None = None) -> None:
    args = common_parser(
        "python -m v2g.ingest.neso_embedded_forecast",
        "NESO embedded wind/solar forecast vintages",
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
    print(
        f"{SOURCE}: forecasts made {start}..{end} (exclusive), leads <= {cfg.max_lead_hours:g} h"
    )
    print(summarise_vintages(out))
    print(f"  written to {args.out}")


if __name__ == "__main__":
    main()
