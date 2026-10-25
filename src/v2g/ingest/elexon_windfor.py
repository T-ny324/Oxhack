"""Ingest GB national WIND generation forecast vintages from the Elexon
Insights WINDFOR dataset -> variable ``wind_da_mw``.

Run
---
::

    python -m v2g.ingest.elexon_windfor [--start 2023-01-01 --end 2026-10-02] [--refresh]
    make data-exog

Why this source (inspected 2026-10-25)
--------------------------------------
NESO's "Historic Day Ahead Wind Forecasts" archive (see
:mod:`v2g.ingest.neso_wind_forecast`) stamps its vintages unreliably from
late 2025 onwards. WINDFOR is NESO's national wind forecast republished by
Elexon with a proper publish time::

    GET https://data.elexon.co.uk/bmrs/api/v1/datasets/WINDFOR
        ?publishDateTimeFrom=2025-11-03T00:00Z&publishDateTimeTo=2025-11-10T00:00Z&format=json

* Rows ``dataset, publishTime, startTime, generation`` (MW). HOURLY
  resolution: each publication holds 73 hourly values from yesterday 21:00 UTC
  to 21:00 UTC two days ahead. Each hourly value is written to both half-hours
  of its hour (a step, not an interpolation), which is documented in the
  processed table by the identical pairs of rows.
* 7-8 publications per day (about 02:30, 04:30, 07:30, 09:30, 15:30, 18:30,
  22:30 UTC). At a 10:00 London issue the latest vintage is the 09:30 UTC one
  in winter and the 07:30 UTC one in summer; both cover all of Block A and B.
* History back to at least 2023-01 (fewer publications per day early on).
  Seven-day publish windows are accepted; one request per 7 days.

Output: ``data/processed/exog/elexon_windfor.parquet`` (``capacity_mw`` NaN).
Raw cache: ``data/raw/elexon_windfor/WINDFOR_<from>_<to>.json.gz``.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from shared.schema import ExogIngestConfig
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

SOURCE = "elexon_windfor"
VARIABLE = "wind_da_mw"
BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1/datasets/WINDFOR"
CHUNK_DAYS = 7
DEFAULT_RAW_DIR = Path("data/raw/elexon_windfor")
DEFAULT_OUT_PATH = DEFAULT_EXOG_DIR / f"{SOURCE}.parquet"


def chunks(start: date, end: date, days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def cache_path(raw_dir: Path, frm: date, to: date) -> Path:
    return Path(raw_dir) / f"WINDFOR_{frm:%Y%m%d}_{to:%Y%m%d}.json.gz"


def fetch_chunk(session, frm: date, to: date) -> str:
    params = {
        "publishDateTimeFrom": f"{frm:%Y-%m-%d}T00:00Z",
        "publishDateTimeTo": f"{to:%Y-%m-%d}T00:00Z",
        "format": "json",
    }
    resp = get_with_retry(session, BASE_URL, params=params)
    load_json_rows(resp.text)
    return resp.text


def rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Raw hourly WINDFOR rows -> half-hourly vintage columns (each hour -> two periods)."""
    if not rows:
        return pd.DataFrame(
            columns=["variable", "target_time_utc", "forecast_made_utc", "value_mw"]
        )
    df = pd.DataFrame(rows)
    start = pd.to_datetime(df["startTime"], utc=True)
    off_hour = start != start.dt.floor("h")
    if off_hour.any():
        raise ValueError(
            f"{SOURCE}: {int(off_hour.sum())} startTime values are not on the hour; the hourly expansion assumes they are"
        )
    made = pd.to_datetime(df["publishTime"], utc=True)
    value = pd.to_numeric(df["generation"], errors="coerce")
    halves = [start, start + pd.Timedelta(minutes=30)]
    return pd.DataFrame(
        {
            "variable": VARIABLE,
            "target_time_utc": pd.concat(halves, ignore_index=True),
            "forecast_made_utc": pd.concat([made, made], ignore_index=True),
            "value_mw": np.concatenate([value.to_numpy(), value.to_numpy()]),
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
    raw_dir = Path(raw_dir)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    own = session is None
    session = session or make_session()
    windows = chunks(start, end)
    try:
        for i, (frm, to) in enumerate(windows, 1):
            path = cache_path(raw_dir, frm, to)
            if path.exists() and not refresh:
                text = read_gzip_text(path)
            else:
                text = fetch_chunk(session, frm, to)
                if pd.Timestamp(to, tz="UTC") < now:
                    write_gzip_text(path, text)
                if cfg.sleep_s:
                    time.sleep(cfg.sleep_s)
            if i % 25 == 0 or i == len(windows):
                log.info("%s: %d/%d publish windows loaded", SOURCE, i, len(windows))
            yield (frm, to), load_json_rows(text)
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
        rows_to_frame(rows)
        for _, rows in load_or_fetch(
            start, end, cfg, raw_dir=raw_dir, session=session, refresh=refresh
        )
    ]
    raw = pd.concat(frames, ignore_index=True) if frames else rows_to_frame([])
    out = finalise_vintages(raw, SOURCE, cfg)
    write_vintages(out, out_path)
    log.info("Wrote %s (%d rows)", out_path, len(out))
    return out


def main(argv: list[str] | None = None) -> None:
    args = common_parser(
        "python -m v2g.ingest.elexon_windfor",
        "Elexon WINDFOR wind forecast vintages",
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
