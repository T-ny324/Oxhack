"""Tests for v2g.ingest.elexon_windfor on a saved real response (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from v2g.ingest import elexon_windfor as wf
from v2g.ingest import vintages as vg
from v2g.ingest.config import load_exog_ingest_config

REPO = Path(__file__).resolve().parents[1]
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "exog"
    / "elexon_windfor_2025-11-03_sample.json"
)
CFG = load_exog_ingest_config(REPO / "config" / "assumptions.yaml")


def load_rows() -> list[dict]:
    return json.load(open(FIXTURE, encoding="utf-8"))["data"]


def test_fixture_is_hourly_with_two_publications():
    rows = load_rows()
    assert set(rows[0]) == {"dataset", "publishTime", "startTime", "generation"}
    assert sorted({r["publishTime"] for r in rows}) == [
        "2025-11-03T07:30:00Z",
        "2025-11-03T09:30:00Z",
    ]
    starts = pd.to_datetime([r["startTime"] for r in rows], utc=True)
    assert (starts.minute == 0).all()


def test_hourly_values_expand_to_both_half_hours():
    out = vg.finalise_vintages(wf.rows_to_frame(load_rows()), wf.SOURCE, CFG)
    assert list(out.columns) == vg.VINTAGE_COLUMNS
    assert (out["variable"] == "wind_da_mw").all()
    pub = out[out["forecast_made_utc"] == pd.Timestamp("2025-11-03T09:30Z")].set_index(
        "target_time_utc"
    )["value_mw"]
    assert (
        pub.loc[pd.Timestamp("2025-11-03T12:00Z")]
        == pub.loc[pd.Timestamp("2025-11-03T12:30Z")]
    )
    assert (pub.index.to_series().diff().dropna() == pd.Timedelta("30min")).all()
    # 73 hourly values -> 146 half-hours, but leads beyond max_lead_hours (48 h) are dropped
    lead_h = (out["target_time_utc"] - out["forecast_made_utc"]) / pd.Timedelta(hours=1)
    assert lead_h.max() <= CFG.max_lead_hours and (lead_h >= 0).all()
    assert out["forecast_made_utc"].notna().all()
    # each publication starts at yesterday 21:00: those already-started periods are not forecasts and are dropped
    assert out["target_time_utc"].min() >= pd.Timestamp("2025-11-03T07:30Z")


def test_non_hourly_start_times_raise():
    rows = load_rows()
    rows[0]["startTime"] = "2025-11-03T12:30:00Z"
    with pytest.raises(ValueError, match="not on the hour"):
        wf.rows_to_frame(rows)


def test_chunks_tile_the_range_in_seven_day_windows():
    windows = wf.chunks(date(2025, 1, 1), date(2025, 1, 21))
    assert windows == [
        (date(2025, 1, 1), date(2025, 1, 8)),
        (date(2025, 1, 8), date(2025, 1, 15)),
        (date(2025, 1, 15), date(2025, 1, 21)),
    ]


def test_duplicates_from_overlapping_windows_are_dropped():
    rows = load_rows()
    frame = wf.rows_to_frame(
        rows + rows
    )  # the same publication appearing in two cached windows
    out = vg.finalise_vintages(frame, wf.SOURCE, CFG)
    assert not out.duplicated(
        ["variable", "target_time_utc", "forecast_made_utc"]
    ).any()
    assert len(out) == len(vg.finalise_vintages(wf.rows_to_frame(rows), wf.SOURCE, CFG))


def test_plausibility_raise_on_absurd_wind():
    frame = wf.rows_to_frame(load_rows())
    future = frame.index[frame["target_time_utc"] > frame["forecast_made_utc"]][
        0
    ]  # a genuine forecast row
    frame.loc[future, "value_mw"] = 90_000.0
    with pytest.raises(ValueError, match="outside plausible range"):
        vg.finalise_vintages(frame, wf.SOURCE, CFG)
