"""Tests for v2g.ingest.elexon_mid.

No network: the fixture is one real API response saved on 2026-10-24 for
``from=2025-11-03T00:00Z&to=2025-11-04T00:00Z&dataProviders=APXMIDP`` (both
bounds inclusive, hence 49 rows). Network behaviour is exercised through a fake
session that synthesises API-shaped rows for whatever window is requested.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from v2g.ingest import elexon_mid as em

FIXTURE = Path(__file__).parent / "fixtures" / "elexon_mid_apxmidp_2025-11-03.json"
PROVIDER = "APXMIDP"
# The fixture's 49 rows run 2025-11-03T00:00Z .. 2025-11-04T00:00Z inclusive,
# i.e. the half-open range below.
START = pd.Timestamp("2025-11-03T00:00Z")
END = pd.Timestamp("2025-11-04T00:30Z")
HALF_HOUR = pd.Timedelta("30min")


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def clean_from(payloads, start=START, end=END) -> pd.DataFrame:
    return em.clean_prices(
        em.payloads_to_frame(payloads, PROVIDER), start, end, PROVIDER
    )


# ---------------------------------------------------------------- fake network


def synth_payload(
    frm: str, to: str, provider: str, price: float = 50.0, volume: float = 1000.0
) -> dict:
    """Rows the real API would return for an inclusive [frm, to] window: newest first."""
    stamps = pd.date_range(pd.Timestamp(frm), pd.Timestamp(to), freq="30min")[::-1]
    rows = [
        {
            "startTime": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dataProvider": provider,
            "settlementDate": t.strftime("%Y-%m-%d"),
            "settlementPeriod": 0,  # not used by the parser
            "price": price,
            "volume": volume,
        }
        for t in stamps
    ]
    return {"metadata": {"datasets": ["MID"]}, "data": rows}


class FakeResponse:
    def __init__(
        self, status_code: int, payload: dict | None = None, headers: dict | None = None
    ):
        self.status_code = status_code
        self._payload = (
            payload
            if payload is not None
            else {"metadata": {"datasets": ["MID"]}, "data": []}
        )
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Stands in for requests.Session. Records calls; optional scripted statuses first."""

    def __init__(self, statuses: list[int] | None = None):
        self.calls: list[dict] = []
        self._statuses = list(statuses or [])

    def get(self, url: str, *, params: dict, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        status = self._statuses.pop(0) if self._statuses else 200
        if status != 200:
            return FakeResponse(status)
        return FakeResponse(
            200, synth_payload(params["from"], params["to"], params["dataProviders"])
        )


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(em.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


# -------------------------------------------------------------------- fixture


def test_fixture_has_the_api_shape_the_parser_relies_on():
    payload = load_fixture()
    assert set(payload) == {"metadata", "data"}
    assert len(payload["data"]) == 49
    assert set(payload["data"][0]) == {
        "startTime",
        "dataProvider",
        "settlementDate",
        "settlementPeriod",
        "price",
        "volume",
    }
    assert {r["dataProvider"] for r in payload["data"]} == {PROVIDER}
    assert all(r["startTime"].endswith("Z") for r in payload["data"])


# ------------------------------------------------------------------- cleaning


def test_index_is_tz_aware_utc_strictly_increasing_30min_no_gaps():
    df = clean_from([load_fixture()])
    assert list(df.columns) == em.OUTPUT_COLUMNS
    assert str(df["ts_utc"].dtype) == "datetime64[ns, UTC]"
    assert df["ts_utc"].is_unique
    assert df["ts_utc"].is_monotonic_increasing
    assert (df["ts_utc"].diff().dropna() == HALF_HOUR).all()
    assert len(df) == 49
    assert df["ts_utc"].iloc[0] == START
    assert df["ts_utc"].iloc[-1] == END - HALF_HOUR
    assert not df["is_missing"].any()
    assert (df["provider"] == PROVIDER).all()
    assert df["price_wholesale_per_mwh"].dtype == "float64"
    assert df["volume_mwh"].dtype == "float64"
    assert df["is_missing"].dtype == bool


def test_zero_or_null_volume_rows_become_nan_and_missing():
    payload = load_fixture()
    zero = payload["data"][5]
    zero["volume"], zero["price"] = 0.0, 0.0
    null = payload["data"][9]
    null["volume"] = None

    df = clean_from([payload]).set_index("ts_utc")
    for row in (zero, null):
        ts = pd.Timestamp(row["startTime"])
        assert np.isnan(df.at[ts, "price_wholesale_per_mwh"])
        assert bool(df.at[ts, "is_missing"])
    assert (
        df.at[pd.Timestamp(zero["startTime"]), "volume_mwh"] == 0.0
    )  # volume kept as reported
    assert int(df["is_missing"].sum()) == 2


def test_real_zero_or_negative_price_with_volume_is_kept():
    payload = load_fixture()
    free = payload["data"][3]
    free["price"] = 0.0  # volume stays > 0: a genuine £0 price
    negative = payload["data"][4]
    negative["price"] = -12.5

    df = clean_from([payload]).set_index("ts_utc")
    assert df.at[pd.Timestamp(free["startTime"]), "price_wholesale_per_mwh"] == 0.0
    assert not df.at[pd.Timestamp(free["startTime"]), "is_missing"]
    assert (
        df.at[pd.Timestamp(negative["startTime"]), "price_wholesale_per_mwh"] == -12.5
    )
    assert not df["is_missing"].any()


def test_gap_in_source_becomes_missing_row_not_dropped_and_not_filled():
    payload = load_fixture()
    removed = payload["data"].pop(20)

    df = clean_from([payload])
    assert len(df) == 49  # the period is still there
    row = df.set_index("ts_utc").loc[pd.Timestamp(removed["startTime"])]
    assert bool(row["is_missing"])
    assert np.isnan(row["price_wholesale_per_mwh"])  # no forward fill / interpolation
    assert np.isnan(row["volume_mwh"])
    assert row["provider"] == PROVIDER
    assert int(df["is_missing"].sum()) == 1


def test_duplicate_timestamps_dropped_keep_last_and_logged(caplog):
    payload = load_fixture()
    dup = dict(payload["data"][10])
    dup["price"] = 999.0
    payload["data"].append(dup)  # later occurrence of the same startTime

    with caplog.at_level(logging.INFO, logger="v2g.ingest.elexon_mid"):
        df = clean_from([payload])
    assert len(df) == 49
    assert df["ts_utc"].is_unique
    assert (
        df.set_index("ts_utc").at[
            pd.Timestamp(dup["startTime"]), "price_wholesale_per_mwh"
        ]
        == 999.0
    )
    assert "Dropped 1 duplicate timestamps" in caplog.text


def test_other_providers_are_filtered_out():
    payload = load_fixture()
    foreign = dict(payload["data"][0])
    foreign["dataProvider"] = "N2EXMIDP"
    foreign["price"], foreign["volume"] = 0.0, 0.0
    payload["data"].append(foreign)

    df = clean_from([payload])
    assert len(df) == 49
    assert (df["provider"] == PROVIDER).all()
    assert not df["is_missing"].any()  # the APXMIDP row for that period still wins


def test_median_price_is_plausible_gbp_per_mwh():
    df = clean_from([load_fixture()])
    median = df.loc[~df["is_missing"], "price_wholesale_per_mwh"].median()
    assert 20 <= median <= 300, f"median {median} looks like p/kWh or the wrong field"


# ---------------------------------------------------------------- time / DST


def test_dst_local_day_lengths_in_utc_index():
    idx = em.build_utc_index(
        pd.Timestamp("2025-03-01T00:00Z"), pd.Timestamp("2025-12-01T00:00Z")
    )
    assert str(idx.dtype) == "datetime64[ns, UTC]"
    assert (idx[1:] - idx[:-1] == HALF_HOUR).all()

    counts = pd.Series(idx.tz_convert("Europe/London").date).value_counts()
    assert counts[dt.date(2025, 10, 26)] == 50  # clocks go back: 25 local hours
    assert counts[dt.date(2025, 3, 30)] == 46  # clocks go forward: 23 local hours
    assert counts[dt.date(2025, 6, 15)] == 48


def test_parse_utc_handles_dates_and_zones_and_rejects_off_grid():
    assert em.parse_utc("2023-01-01") == pd.Timestamp("2023-01-01T00:00Z")
    assert em.parse_utc("2025-11-03T00:00Z") == pd.Timestamp("2025-11-03T00:00Z")
    assert str(em.parse_utc("2025-11-03").tz) == "UTC"
    assert em.parse_utc("2025-11-03").unit == "ns"
    # BST wall-clock input is converted to UTC, never stored as local
    assert em.parse_utc("2025-07-01T01:00+01:00") == pd.Timestamp("2025-07-01T00:00Z")
    with pytest.raises(ValueError):
        em.parse_utc("2025-11-03T00:07Z")


# ------------------------------------------------------------------ chunking


def test_iter_chunks_tiles_20_days_into_three_non_overlapping_windows():
    start, end = pd.Timestamp("2025-01-01T00:00Z"), pd.Timestamp("2025-01-21T00:00Z")
    chunks = em.iter_chunks(start, end)
    assert len(chunks) == 3
    assert chunks[0][0] == start
    assert chunks[-1][1] == end - HALF_HOUR
    for (_, prev_to), (next_from, _) in itertools.pairwise(chunks):
        assert next_from == prev_to + HALF_HOUR  # contiguous, no overlap, no gap
    for frm, to in chunks:
        assert (
            to - frm <= pd.Timedelta(days=7) - HALF_HOUR
        )  # within the API's 7-day limit


def test_twenty_day_range_makes_three_requests_then_uses_cache(tmp_path, no_sleep):
    start, end = pd.Timestamp("2025-01-01T00:00Z"), pd.Timestamp("2025-01-21T00:00Z")

    session = FakeSession()
    payloads = em.load_or_fetch_chunks(
        start, end, PROVIDER, raw_dir=tmp_path, session=session, sleep_s=0
    )
    assert len(session.calls) == 3
    assert [c["params"]["from"] for c in session.calls] == [
        "2025-01-01T00:00Z",
        "2025-01-08T00:00Z",
        "2025-01-15T00:00Z",
    ]
    assert [c["params"]["to"] for c in session.calls] == [
        "2025-01-07T23:30Z",
        "2025-01-14T23:30Z",
        "2025-01-20T23:30Z",
    ]
    for call in session.calls:
        assert call["url"] == em.BASE_URL
        assert call["params"]["dataProviders"] == PROVIDER
        assert call["params"]["format"] == "json"
    assert sum(len(p["data"]) for p in payloads) == 20 * 48

    df = em.clean_prices(em.payloads_to_frame(payloads, PROVIDER), start, end, PROVIDER)
    assert len(df) == 20 * 48
    assert not df["is_missing"].any()

    cached = sorted(p.name for p in tmp_path.glob("*.json"))
    assert cached == [
        "APXMIDP_20250101T0000Z_20250107T2330Z.json",
        "APXMIDP_20250108T0000Z_20250114T2330Z.json",
        "APXMIDP_20250115T0000Z_20250120T2330Z.json",
    ]

    # Re-run: everything comes from cache, zero network calls.
    offline = FakeSession()
    again = em.load_or_fetch_chunks(
        start, end, PROVIDER, raw_dir=tmp_path, session=offline, sleep_s=0
    )
    assert offline.calls == []
    assert again == payloads

    # --refresh forces re-download.
    refreshed = FakeSession()
    em.load_or_fetch_chunks(
        start,
        end,
        PROVIDER,
        raw_dir=tmp_path,
        session=refreshed,
        refresh=True,
        sleep_s=0,
    )
    assert len(refreshed.calls) == 3


def test_window_ending_in_the_future_is_not_cached(tmp_path, no_sleep):
    start, end = pd.Timestamp("2025-01-01T00:00Z"), pd.Timestamp("2025-01-10T00:00Z")
    pretend_now = pd.Timestamp("2025-01-09T12:00Z")  # second window still in progress
    em.load_or_fetch_chunks(
        start,
        end,
        PROVIDER,
        raw_dir=tmp_path,
        session=FakeSession(),
        sleep_s=0,
        now=pretend_now,
    )
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [
        "APXMIDP_20250101T0000Z_20250107T2330Z.json"
    ]


# -------------------------------------------------------------------- retries


def test_retries_on_5xx_and_429_then_succeeds(no_sleep):
    session = FakeSession(statuses=[503, 429])
    text = em.fetch_chunk(
        session,
        pd.Timestamp("2025-01-01T00:00Z"),
        pd.Timestamp("2025-01-01T23:30Z"),
        PROVIDER,
        backoff_s=0.01,
    )
    assert len(session.calls) == 3
    assert len(json.loads(text)["data"]) == 48
    assert len(no_sleep) == 2
    assert no_sleep[1] > no_sleep[0]  # exponential backoff


def test_gives_up_after_max_retries(no_sleep):
    session = FakeSession(statuses=[500] * 10)
    with pytest.raises(RuntimeError, match="Giving up"):
        em.fetch_chunk(
            session,
            pd.Timestamp("2025-01-01T00:00Z"),
            pd.Timestamp("2025-01-01T23:30Z"),
            PROVIDER,
            max_retries=2,
            backoff_s=0.01,
        )
    assert len(session.calls) == 3


def test_non_retryable_4xx_fails_immediately(no_sleep):
    session = FakeSession(statuses=[400])
    with pytest.raises(requests.HTTPError, match="HTTP 400"):
        em.fetch_chunk(
            session,
            pd.Timestamp("2025-01-01T00:00Z"),
            pd.Timestamp("2025-01-01T23:30Z"),
            PROVIDER,
        )
    assert len(session.calls) == 1
    assert no_sleep == []


# ------------------------------------------------------------------ end to end


def test_run_writes_parquet_with_contract_dtypes(tmp_path, no_sleep):
    out = tmp_path / "processed" / "prices.parquet"
    df = em.run(
        "2025-01-01",
        "2025-01-03",
        PROVIDER,
        raw_dir=tmp_path / "raw",
        out_path=out,
        session=FakeSession(),
        sleep_s=0,
    )
    back = pd.read_parquet(out)
    assert list(back.columns) == em.OUTPUT_COLUMNS
    assert str(back["ts_utc"].dtype) == "datetime64[ns, UTC]"
    assert back["price_wholesale_per_mwh"].dtype == "float64"
    assert back["is_missing"].dtype == bool
    assert len(back) == len(df) == 2 * 48
    assert back["ts_utc"].iloc[0] == pd.Timestamp("2025-01-01T00:00Z")
    assert back["ts_utc"].iloc[-1] == pd.Timestamp("2025-01-02T23:30Z")
    assert back["price_wholesale_per_mwh"].median() == 50.0

    summary = em.summarise(back)
    assert "rows (30-min periods)    : 96" in summary
    assert "median 50.00" in summary
    assert "0.00%" in summary
