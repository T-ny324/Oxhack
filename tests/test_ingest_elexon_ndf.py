"""Tests for v2g.ingest.elexon_ndf on a saved real response (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from v2g.ingest import elexon_ndf as ndf
from v2g.ingest import vintages as vg
from v2g.ingest.config import load_exog_ingest_config

REPO = Path(__file__).resolve().parents[1]
FIXTURE = (
    Path(__file__).parent / "fixtures" / "exog" / "elexon_ndf_2025-11-03_sample.json"
)
CFG = load_exog_ingest_config(REPO / "config" / "assumptions.yaml")


def load_rows() -> list[dict]:
    return json.load(open(FIXTURE, encoding="utf-8"))["data"]


def test_fixture_shape_matches_api():
    rows = load_rows()
    assert set(rows[0]) == {
        "dataset",
        "demand",
        "publishTime",
        "startTime",
        "settlementDate",
        "settlementPeriod",
        "boundary",
    }
    assert {r["boundary"] for r in rows} == {"N"}
    assert sorted({r["publishTime"] for r in rows}) == [
        "2025-11-03T08:45:00Z",
        "2025-11-03T09:16:00Z",
        "2025-11-03T09:46:00Z",
    ]


def test_parse_and_finalise_vintage_schema():
    out = vg.finalise_vintages(ndf.rows_to_frame(load_rows(), CFG), ndf.SOURCE, CFG)
    assert list(out.columns) == vg.VINTAGE_COLUMNS
    assert str(out["target_time_utc"].dtype) == "datetime64[ns, UTC]"
    assert str(out["forecast_made_utc"].dtype) == "datetime64[ns, UTC]"
    assert out["value_mw"].dtype == "float64" and out["capacity_mw"].isna().all()
    assert (out["variable"] == "demand_da_mw").all() and (
        out["source"] == "elexon_ndf"
    ).all()
    assert out["forecast_made_utc"].notna().all()
    assert (out["forecast_made_utc"] <= out["target_time_utc"]).all()
    assert len(out) == 125
    # the 08:45 local publication covers tomorrow's operational day 05:00 -> 05:00 local (GMT in November)
    da = out[out["forecast_made_utc"] == pd.Timestamp("2025-11-03T08:45Z")]
    assert len(da) == 48
    assert da["target_time_utc"].min() == pd.Timestamp("2025-11-04T05:00Z")
    assert da["target_time_utc"].max() == pd.Timestamp("2025-11-05T04:30Z")
    # the 09:46 publication starts at the next half-hour
    latest = out[out["forecast_made_utc"] == pd.Timestamp("2025-11-03T09:46Z")]
    assert latest["target_time_utc"].min() == pd.Timestamp("2025-11-03T10:00Z")


def test_duplicates_dropped_and_logged(caplog):
    rows = load_rows()
    frame = ndf.rows_to_frame(rows + rows[:5], CFG)
    with caplog.at_level("WARNING"):
        out = vg.finalise_vintages(frame, ndf.SOURCE, CFG)
    assert len(out) == 125
    assert "dropped 5 duplicate" in caplog.text
    assert not out.duplicated(
        ["variable", "target_time_utc", "forecast_made_utc"]
    ).any()


def test_rows_made_after_target_and_without_made_time_are_dropped(caplog):
    frame = ndf.rows_to_frame(load_rows(), CFG)
    frame.loc[0, "forecast_made_utc"] = frame.loc[0, "target_time_utc"] + pd.Timedelta(
        hours=5
    )  # made after target
    frame.loc[1, "forecast_made_utc"] = pd.NaT
    with caplog.at_level("WARNING"):
        out = vg.finalise_vintages(frame, ndf.SOURCE, CFG)
    assert len(out) == 123
    assert (
        "made after their target" in caplog.text
        and "without a forecast-made" in caplog.text
    )
    assert out["forecast_made_utc"].notna().all()


def test_plausibility_bounds_raise():
    frame = ndf.rows_to_frame(load_rows(), CFG)
    frame.loc[3, "value_mw"] = 250_000.0
    with pytest.raises(ValueError, match="outside plausible range"):
        vg.finalise_vintages(frame, ndf.SOURCE, CFG)


def test_missing_values_stay_nan():
    frame = ndf.rows_to_frame(load_rows(), CFG)
    frame.loc[7, "value_mw"] = np.nan
    out = vg.finalise_vintages(frame, ndf.SOURCE, CFG)
    assert out["value_mw"].isna().sum() == 1


def test_run_uses_cache_and_writes_parquet(tmp_path):
    day = pd.Timestamp("2025-11-03").date()
    path = ndf.cache_path(tmp_path / "raw", day)
    vg.write_gzip_text(path, FIXTURE.read_text(encoding="utf-8"))

    class NoNetwork:
        def get(self, *a, **k):
            raise AssertionError("network must not be used when cached")

        def close(self):
            pass

    out = ndf.run(
        day,
        day + pd.Timedelta(days=1),
        CFG,
        raw_dir=tmp_path / "raw",
        out_path=tmp_path / "ndf.parquet",
        session=NoNetwork(),
    )
    back = pd.read_parquet(tmp_path / "ndf.parquet")
    assert len(back) == len(out) == 125
    assert str(back["target_time_utc"].dtype) == "datetime64[ns, UTC]"
    summary = vg.summarise_vintages(back)
    assert "demand_da_mw" in summary and "median lead" in summary
