"""Stage S2 ingest: GB wholesale half-hourly prices from the Elexon Insights
Solution API, dataset MID (Market Index Data).

Run
---
::

    python -m v2g.ingest.elexon_mid --start 2023-01-01 --end 2026-10-01 \\
        [--provider APXMIDP] [--refresh]
    make data-prices

``--end`` is exclusive: the default range covers 2023-01-01T00:00Z up to and
including the 2026-09-30T23:30Z period (training data plus the agreed
2025-10-01 -> 2026-09-30 backtest window).

Output
------
``data/processed/prices.parquet`` with exactly one row per 30-minute UTC
settlement period over [start, end), no gaps:

======================== ====================== ================================
column                   dtype                  meaning
======================== ====================== ================================
ts_utc                   datetime64[ns, UTC]    START of the half-hour
price_wholesale_per_mwh  float64                £/MWh (never converted to p/kWh)
volume_mwh               float64                MWh traded in the period
provider                 str                    e.g. "APXMIDP"
is_missing               bool                   True where price is NaN
======================== ====================== ================================

Only the first two columns are required by the S3 -> S5 forecast contract; the
rest are extra. Nothing is forward-filled or interpolated here: imputation
belongs to the modelling stage.

Cleaning rules
--------------
* Everything in UTC; local time is never stored.
* A complete 30-min UTC index is built over [start, end) and the data is
  reindexed onto it, so gaps appear as NaN with ``is_missing=True``. No period
  is ever silently dropped.
* Rows with volume == 0, volume < 0 or null volume get price NaN and
  ``is_missing=True`` (a zero-volume "price" of 0 is not a real £0 price). The
  reported volume is kept as-is.
* Exact duplicate timestamps are dropped, keeping the last occurrence; the
  count is logged.

What the live API actually returns (inspected 2026-10-24)
---------------------------------------------------------
Endpoint, no API key required::

    GET https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index
        ?from=2025-11-03T00:00Z&to=2025-11-04T00:00Z&dataProviders=APXMIDP&format=json

* Body: ``{"metadata": {"datasets": ["MID"]}, "data": [...]}``.
* Row fields: ``startTime`` (ISO-8601 UTC, trailing ``Z``), ``dataProvider``,
  ``settlementDate``, ``settlementPeriod``, ``price``, ``volume``. Example::

      {"startTime": "2025-11-04T00:00:00Z", "dataProvider": "APXMIDP",
       "settlementDate": "2025-11-04", "settlementPeriod": 1,
       "price": 4.40, "volume": 2198.300}

* ``startTime`` is the START of the half-hour, in UTC. Checked across the
  clock change: in July, settlementPeriod 1 of settlementDate 2025-07-08 has
  startTime 2025-07-07T23:00:00Z (BST midnight); in November it is
  00:00:00Z. ``settlementDate``/``settlementPeriod`` follow the UK local
  clock, so they are NOT used to build the index.
* Units: ``price`` is £/MWh and ``volume`` is MWh (Elexon MID definition).
  Observed values of roughly £4-115/MWh and 1,000-3,000 MWh per half-hour are
  consistent with that.
* The provider filter parameter is ``dataProviders`` (plural). The singular
  ``dataProvider`` is silently ignored and returns every provider.
* Providers seen: ``APXMIDP`` (real volumes) and ``N2EXMIDP`` (price 0.00 and
  volume 0.000 on every row inspected). APXMIDP only is fetched by default.
* ``from`` and ``to`` are both INCLUSIVE on ``startTime``: a 24 h request
  returns 49 rows. Rows are returned newest-first.
* Maximum window is 7 days. Longer requests fail with HTTP 400:
  ``"The date range between From and To inclusive must not exceed 7 days"``.
  We therefore request non-overlapping windows ``[from, from + 7d - 30min]``,
  which return exactly 336 rows for a complete week.
* Data is available back to at least 2023-01-01. No rate-limit headers are
  returned; we still sleep briefly between calls and back off on 429/5xx.

Caching
-------
Each raw chunk body is stored verbatim at
``data/raw/elexon_mid/<provider>_<from>_<to>.json`` (timestamps formatted
``YYYYMMDDTHHMMZ`` so file names are portable, e.g.
``APXMIDP_20250101T0000Z_20250107T2330Z.json``). Cached chunks are reused
unless ``--refresh`` is passed, so re-runs are cheap and work offline. A chunk
whose window ends in the future is never cached, because the API would have
returned a partial week.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index"
DEFAULT_PROVIDER = "APXMIDP"
DEFAULT_START = "2023-01-01T00:00Z"
DEFAULT_END = "2026-10-01T00:00Z"  # exclusive
MAX_CHUNK_DAYS = 7  # API limit: From..To inclusive must not exceed 7 days
FREQ = "30min"
PERIOD = pd.Timedelta(FREQ)
DEFAULT_RAW_DIR = Path("data/raw/elexon_mid")
DEFAULT_OUT_PATH = Path("data/processed/prices.parquet")
OUTPUT_COLUMNS = [
    "ts_utc",
    "price_wholesale_per_mwh",
    "volume_mwh",
    "provider",
    "is_missing",
]
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "oxhack-v2g/0.1 (v2g.ingest.elexon_mid)"


class SessionLike(Protocol):
    """The subset of requests.Session we use; tests pass a fake."""

    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> Any: ...


# --------------------------------------------------------------------------- time


def parse_utc(value: str | datetime | pd.Timestamp) -> pd.Timestamp:
    """Parse a date or datetime as a UTC, nanosecond Timestamp on the 30-min grid.

    Naive inputs (e.g. ``2023-01-01``) are taken to be UTC. Aware inputs are
    converted to UTC.
    """
    ts = pd.Timestamp(value)
    if ts is pd.NaT:
        raise ValueError(f"Could not parse timestamp: {value!r}")
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    ts = ts.as_unit("ns")
    if ts != ts.floor(FREQ):
        raise ValueError(f"{value!r} is not aligned to the {FREQ} settlement grid")
    return ts


def fmt_api(ts: pd.Timestamp) -> str:
    """Format for the API's ``from``/``to`` parameters, e.g. 2025-11-03T00:00Z."""
    return ts.strftime("%Y-%m-%dT%H:%MZ")


def fmt_file(ts: pd.Timestamp) -> str:
    """Filesystem-safe timestamp for cache file names, e.g. 20251103T0000Z."""
    return ts.strftime("%Y%m%dT%H%MZ")


def build_utc_index(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """Complete 30-minute UTC grid over [start, end), end exclusive."""
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")
    return pd.date_range(
        start, end, freq=FREQ, inclusive="left", unit="ns", name="ts_utc"
    )


def iter_chunks(
    start: pd.Timestamp, end: pd.Timestamp, chunk_days: int = MAX_CHUNK_DAYS
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Non-overlapping ``(from, to_inclusive)`` windows that tile [start, end).

    The API treats ``to`` as inclusive, so each window ends one period before
    the next one starts. A 20-day range gives three windows: 7 + 7 + 6 days.
    """
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")
    if not 1 <= chunk_days <= MAX_CHUNK_DAYS:
        raise ValueError(f"chunk_days must be in 1..{MAX_CHUNK_DAYS}, got {chunk_days}")
    step = pd.Timedelta(days=chunk_days)
    chunks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        chunks.append((cur, nxt - PERIOD))
        cur = nxt
    return chunks


# ---------------------------------------------------------------------- fetching


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _retry_after_seconds(resp: Any) -> float | None:
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def fetch_chunk(
    session: SessionLike,
    start: pd.Timestamp,
    to_inclusive: pd.Timestamp,
    provider: str = DEFAULT_PROVIDER,
    *,
    max_retries: int = 5,
    backoff_s: float = 1.0,
    timeout_s: float = 30.0,
) -> str:
    """GET one window and return the raw JSON body as text.

    Retries with exponential backoff (plus jitter, honouring ``Retry-After``)
    on 429, 5xx, timeouts and connection errors. Any other 4xx fails at once
    with the API's own validation message, which is usually informative.
    """
    params = {
        "from": fmt_api(start),
        "to": fmt_api(to_inclusive),
        "dataProviders": provider,
        "format": "json",
    }
    attempt = 0
    while True:
        retry_after: float | None = None
        try:
            resp = session.get(BASE_URL, params=params, timeout=timeout_s)
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                text = resp.text
                try:
                    json.loads(text)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Non-JSON 200 response for {params['from']}..{params['to']}: {text[:200]!r}"
                    ) from exc
                return text
            if resp.status_code not in RETRY_STATUSES:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code} for {params}: {resp.text[:500]}",
                    response=resp,
                )
            reason = f"HTTP {resp.status_code}"
            retry_after = _retry_after_seconds(resp)

        attempt += 1
        if attempt > max_retries:
            raise RuntimeError(
                f"Giving up on {params['from']}..{params['to']} after {max_retries} retries ({reason})"
            )
        delay = (
            retry_after if retry_after is not None else backoff_s * 2 ** (attempt - 1)
        )
        delay += random.uniform(0, 0.25)
        log.warning(
            "Retry %d/%d for %s..%s in %.1fs (%s)",
            attempt,
            max_retries,
            params["from"],
            params["to"],
            delay,
            reason,
        )
        time.sleep(delay)


def load_or_fetch_chunks(
    start: pd.Timestamp,
    end: pd.Timestamp,
    provider: str = DEFAULT_PROVIDER,
    *,
    raw_dir: Path | str = DEFAULT_RAW_DIR,
    session: SessionLike | None = None,
    refresh: bool = False,
    sleep_s: float = 0.2,
    chunk_days: int = MAX_CHUNK_DAYS,
    now: pd.Timestamp | None = None,
) -> list[dict[str, Any]]:
    """Return one parsed JSON payload per window, using the raw cache when possible.

    A window is fetched if its cache file is absent or ``refresh`` is set.
    Fetched windows that end before ``now`` are written to the cache; windows
    that end in the future are not, since the API will have returned a
    partial week.
    """
    raw_dir = Path(raw_dir)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    chunks = iter_chunks(start, end, chunk_days)
    own_session = session is None
    if own_session:
        session = make_session()
    payloads: list[dict[str, Any]] = []
    try:
        for i, (a, b) in enumerate(chunks, 1):
            path = raw_dir / f"{provider}_{fmt_file(a)}_{fmt_file(b)}.json"
            if path.exists() and not refresh:
                text = path.read_text(encoding="utf-8")
                source = f"cache {path.name}"
            else:
                text = fetch_chunk(session, a, b, provider)
                if b < now:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                    source = f"api -> {path.name}"
                else:
                    source = "api (not cached: window not yet complete)"
                if sleep_s:
                    time.sleep(sleep_s)
            payload = json.loads(text)
            n_rows = len(payload.get("data") or [])
            log.info(
                "chunk %d/%d %s..%s: %d rows from %s",
                i,
                len(chunks),
                fmt_api(a),
                fmt_api(b),
                n_rows,
                source,
            )
            payloads.append(payload)
    finally:
        if own_session:
            session.close()  # type: ignore[union-attr]
    return payloads


# ----------------------------------------------------------------------- parsing


def payloads_to_frame(
    payloads: Iterable[dict[str, Any]], provider: str = DEFAULT_PROVIDER
) -> pd.DataFrame:
    """Flatten API payloads into a raw frame with the contract column names.

    Keeps only rows for ``provider`` (the API filter is trusted but verified).
    No cleaning happens here; see :func:`clean_prices`.
    """
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        data = payload.get("data")
        if not isinstance(data, list):
            raise TypeError(
                f"Unexpected API payload: no 'data' list; keys={list(payload)}"
            )
        rows.extend(data)

    if not rows:
        return pd.DataFrame(
            {
                "ts_utc": pd.DatetimeIndex([], dtype="datetime64[ns, UTC]"),
                "price_wholesale_per_mwh": pd.Series(dtype="float64"),
                "volume_mwh": pd.Series(dtype="float64"),
                "provider": pd.Series(dtype=str),
            }
        )

    df = pd.DataFrame(rows)
    required = {"startTime", "dataProvider", "price", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"API rows lack expected fields {sorted(missing)}; got {list(df.columns)}"
        )

    other = df["dataProvider"] != provider
    if other.any():
        log.warning(
            "Ignoring %d rows from other providers %s",
            int(other.sum()),
            sorted(df.loc[other, "dataProvider"].unique()),
        )
        df = df.loc[~other]

    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(df["startTime"], utc=True).astype(
                "datetime64[ns, UTC]"
            ),
            "price_wholesale_per_mwh": pd.to_numeric(
                df["price"], errors="coerce"
            ).astype("float64"),
            "volume_mwh": pd.to_numeric(df["volume"], errors="coerce").astype(
                "float64"
            ),
            "provider": df["dataProvider"].astype(str),
        }
    ).reset_index(drop=True)


def clean_prices(
    raw: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    provider: str = DEFAULT_PROVIDER,
) -> pd.DataFrame:
    """Apply the cleaning rules and return the contract frame over [start, end).

    Steps: drop duplicate timestamps (keep last) -> drop off-grid or
    out-of-range rows (logged) -> zero/null volume gives NaN price -> reindex
    onto the complete 30-min UTC grid -> set ``is_missing``.
    """
    df = raw.copy()

    n_before = len(df)
    df = df.drop_duplicates(subset="ts_utc", keep="last")
    n_dups = n_before - len(df)
    (log.warning if n_dups else log.info)(
        "Dropped %d duplicate timestamps (kept last occurrence)", n_dups
    )

    off_grid = df["ts_utc"] != df["ts_utc"].dt.floor(FREQ)
    if off_grid.any():
        log.warning(
            "Ignoring %d rows not aligned to the %s grid", int(off_grid.sum()), FREQ
        )
        df = df.loc[~off_grid]

    outside = (df["ts_utc"] < start) | (df["ts_utc"] >= end)
    if outside.any():
        log.info(
            "Ignoring %d rows outside the requested range [%s, %s)",
            int(outside.sum()),
            fmt_api(start),
            fmt_api(end),
        )
        df = df.loc[~outside]

    df = df.copy()
    no_volume = df["volume_mwh"].isna() | (df["volume_mwh"] <= 0)
    if no_volume.any():
        log.info(
            "%d rows with zero/null volume: price set to NaN", int(no_volume.sum())
        )
    df.loc[no_volume, "price_wholesale_per_mwh"] = np.nan

    index = build_utc_index(start, end)
    out = df.set_index("ts_utc").reindex(index).reset_index()
    out["provider"] = provider
    out["is_missing"] = out["price_wholesale_per_mwh"].isna()
    out = out[OUTPUT_COLUMNS]

    n_missing = int(out["is_missing"].sum())
    log.info(
        "%d/%d periods missing (%.2f%%)",
        n_missing,
        len(out),
        100.0 * n_missing / len(out),
    )
    return out


# ------------------------------------------------------------------------ output


def write_parquet(df: pd.DataFrame, path: Path | str = DEFAULT_OUT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, engine="pyarrow")
    return path


def summarise(df: pd.DataFrame) -> str:
    """Short human-readable summary printed at the end of a run."""
    n = len(df)
    if n == 0:
        return "Elexon MID prices: no rows"
    n_missing = int(df["is_missing"].sum())
    prices = df["price_wholesale_per_mwh"].dropna()
    first, last = df["ts_utc"].iloc[0], df["ts_utc"].iloc[-1]
    lines = [
        "Elexon MID wholesale prices",
        f"  provider                 : {df['provider'].iloc[0]}",
        f"  rows (30-min periods)    : {n:,}",
        f"  span (UTC, period start) : {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M}",
        f"  missing                  : {n_missing:,} ({100.0 * n_missing / n:.2f}%)",
    ]
    if len(prices):
        lines.append(
            f"  price £/MWh              : min {prices.min():.2f} | "
            f"median {prices.median():.2f} | max {prices.max():.2f}"
        )
    else:
        lines.append("  price £/MWh              : no non-missing rows")
    return "\n".join(lines)


def run(
    start: str | datetime | pd.Timestamp = DEFAULT_START,
    end: str | datetime | pd.Timestamp = DEFAULT_END,
    provider: str = DEFAULT_PROVIDER,
    *,
    raw_dir: Path | str = DEFAULT_RAW_DIR,
    out_path: Path | str = DEFAULT_OUT_PATH,
    refresh: bool = False,
    session: SessionLike | None = None,
    sleep_s: float = 0.2,
    chunk_days: int = MAX_CHUNK_DAYS,
) -> pd.DataFrame:
    """Fetch (or load from cache), clean, write the parquet, and return the frame."""
    start_ts, end_ts = parse_utc(start), parse_utc(end)
    log.info(
        "Ingesting %s MID prices for [%s, %s)",
        provider,
        fmt_api(start_ts),
        fmt_api(end_ts),
    )
    payloads = load_or_fetch_chunks(
        start_ts,
        end_ts,
        provider,
        raw_dir=raw_dir,
        session=session,
        refresh=refresh,
        sleep_s=sleep_s,
        chunk_days=chunk_days,
    )
    raw = payloads_to_frame(payloads, provider)
    df = clean_prices(raw, start_ts, end_ts, provider)
    written = write_parquet(df, out_path)
    log.info("Wrote %s (%d rows)", written, len(df))
    return df


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m v2g.ingest.elexon_mid",
        description="Ingest GB wholesale half-hourly prices (Elexon MID) to data/processed/prices.parquet.",
    )
    p.add_argument(
        "--start",
        default=DEFAULT_START,
        help=f"UTC start, inclusive (default {DEFAULT_START})",
    )
    p.add_argument(
        "--end", default=DEFAULT_END, help=f"UTC end, EXCLUSIVE (default {DEFAULT_END})"
    )
    p.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help="MID data provider (default APXMIDP)",
    )
    p.add_argument(
        "--refresh", action="store_true", help="re-download chunks even if cached"
    )
    p.add_argument(
        "--raw-dir", default=str(DEFAULT_RAW_DIR), help="raw chunk cache directory"
    )
    p.add_argument("--out", default=str(DEFAULT_OUT_PATH), help="output parquet path")
    p.add_argument(
        "--chunk-days",
        type=int,
        default=MAX_CHUNK_DAYS,
        help=f"days per request, max {MAX_CHUNK_DAYS}",
    )
    p.add_argument(
        "--sleep", type=float, default=0.2, help="seconds to sleep between API calls"
    )
    p.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    df = run(
        args.start,
        args.end,
        args.provider,
        raw_dir=args.raw_dir,
        out_path=args.out,
        refresh=args.refresh,
        sleep_s=args.sleep,
        chunk_days=args.chunk_days,
    )
    print(summarise(df))
    print(f"  written to               : {args.out}")


if __name__ == "__main__":
    main()
