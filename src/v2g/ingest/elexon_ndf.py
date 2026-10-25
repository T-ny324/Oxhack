"""Ingest GB day-ahead national DEMAND forecast vintages from the Elexon
Insights NDF dataset (National Demand Forecast) -> variable ``demand_da_mw``.

Run
---
::

    python -m v2g.ingest.elexon_ndf [--start 2023-01-01 --end 2026-10-02] [--refresh]
    make data-exog

Why this source (inspected 2026-10-25)
--------------------------------------
NESO's own "Day Ahead Demand Forecast" archive only holds cardinal points
(peaks/troughs), not half-hours. The Elexon stream carries NESO's half-hourly
national demand forecast WITH a publish time, back to at least 2023-01::

    GET https://data.elexon.co.uk/bmrs/api/v1/datasets/NDF
        ?publishDateTimeFrom=2025-11-03T00:00Z&publishDateTimeTo=2025-11-04T00:00Z&format=json

* Body ``{"data": [...]}``; rows ``dataset, demand, publishTime, startTime,
  settlementDate, settlementPeriod, boundary``. ``startTime`` is the UTC START
  of the half-hour; ``demand`` is MW; ``boundary`` is ``N`` (national) for NDF.
* The window between PublishDateTimeFrom and PublishDateTimeTo is limited to
  one day (inclusive), so one request per publish day: ~48 publications,
  ~2,800 rows, ~480 KB. The raw body is cached gzipped.
* Publications land every ~30 min at hh:16/hh:46 and cover from the next
  half-hour to 04:30 local the day after; a separate publication at 08:45 local
  (07:45Z in BST, 08:45Z in GMT) covers tomorrow's operational day 05:00 ->
  05:00. So at a 10:00 local issue the latest vintage per target comes from the
  09:46 publication for today and tomorrow up to 04:30, and from the 08:45
  publication for the rest of tomorrow: Block A and Block B are fully covered.
* Typical lead at a 10:00 issue: 0.25 h (next period) to ~38 h.

Output: ``data/processed/exog/elexon_ndf.parquet`` in the vintage schema
(:mod:`v2g.ingest.vintages`), boundary ``N`` only, ``capacity_mw`` NaN.
Raw cache: ``data/raw/elexon_ndf/NDF_<YYYYMMDD>.json.gz`` per publish day.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
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
    load_json_rows,
    make_session,
    read_gzip_text,
    resolve_dates,
    summarise_vintages,
    write_gzip_text,
    write_vintages,
)

log = logging.getLogger(__name__)

SOURCE = "elexon_ndf"
VARIABLE = "demand_da_mw"
BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets/NDF"
BOUNDARY = "N"
DEFAULT_RAW_DIR = Path("data/raw/elexon_ndf")
DEFAULT_OUT_PATH = DEFAULT_EXOG_DIR / f"{SOURCE}.parquet"


def cache_path(raw_dir: Path, day: date) -> Path:
    return Path(raw_dir) / f"NDF_{day:%Y%m%d}.json.gz"


def fetch_day(session, day: date) -> str:
    params = {
        "publishDateTimeFrom": f"{day:%Y-%m-%d}T00:00Z",
        "publishDateTimeTo": f"{day + timedelta(days=1):%Y-%m-%d}T00:00Z",
        "format": "json",
    }
    resp = get_with_retry(session, BASE_URL, params=params)
    load_json_rows(resp.text)  # validate
    return resp.text


def rows_to_frame(rows: list[dict], cfg: ExogIngestConfig) -> pd.DataFrame:
    """Raw NDF rows -> vintage columns (boundary N only)."""
    if not rows:
        return pd.DataFrame(
            columns=["variable", "target_time_utc", "forecast_made_utc", "value_mw"]
        )
    df = pd.DataFrame(rows)
    other = df["boundary"] != BOUNDARY
    if other.any():
        log.info("ignoring %d rows with boundary != %s", int(other.sum()), BOUNDARY)
        df = df[~other]
    target = pd.to_datetime(df["startTime"], utc=True)
    expected = settlement_period_to_utc(
        df["settlementDate"], df["settlementPeriod"], cfg.timezone
    )
    mismatch = target.to_numpy() != expected.to_numpy()
    if mismatch.any():
        log.warning(
            "%d rows where startTime disagrees with settlement date/period; keeping startTime",
            int(mismatch.sum()),
        )
    return pd.DataFrame(
        {
            "variable": VARIABLE,
            "target_time_utc": target,
            "forecast_made_utc": pd.to_datetime(df["publishTime"], utc=True),
            "value_mw": pd.to_numeric(df["demand"], errors="coerce"),
        }
    )


def load_or_fetch(
    start: date,
    end: date,
    cfg: ExogIngestConfig,
    *,
    raw_dir: Path,
    session=None,
    refresh=False,
    now=None,
):
    """Yield (day, rows) for every publish day in [start, end), from cache when possible."""
    raw_dir = Path(raw_dir)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    own = session is None
    session = session or make_session()
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    try:
        for i, day in enumerate(days, 1):
            path = cache_path(raw_dir, day)
            if path.exists() and not refresh:
                text = read_gzip_text(path)
            else:
                text = fetch_day(session, day)
                if pd.Timestamp(day + timedelta(days=1), tz="UTC") < now:
                    write_gzip_text(path, text)
                if cfg.sleep_s:
                    time.sleep(cfg.sleep_s)
            if i % 100 == 0 or i == len(days):
                log.info("%s: %d/%d publish days loaded", SOURCE, i, len(days))
            yield day, load_json_rows(text)
    finally:
        if own:
            session.close()


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
    frames = [
        rows_to_frame(rows, cfg)
        for _, rows in load_or_fetch(
            start, end, cfg, raw_dir=raw_dir, session=session, refresh=refresh
        )
    ]
    raw = pd.concat(frames, ignore_index=True) if frames else rows_to_frame([], cfg)
    out = finalise_vintages(raw, SOURCE, cfg)
    write_vintages(out, out_path)
    log.info("Wrote %s (%d rows)", out_path, len(out))
    return out


def main(argv: list[str] | None = None) -> None:
    args = common_parser(
        "python -m v2g.ingest.elexon_ndf",
        "Elexon NDF demand forecast vintages",
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
    print(f"{SOURCE}: publish days {start}..{end} (exclusive)")
    print(summarise_vintages(out))
    print(f"  written to {args.out}")


if __name__ == "__main__":
    main()
