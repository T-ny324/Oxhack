"""Tests for v2g.ingest.neso_wind_forecast on a saved slice of the real archive (no network)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from v2g.ingest import neso_wind_forecast as nw
from v2g.ingest import vintages as vg
from v2g.ingest.config import load_exog_ingest_config

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "exog" / "neso_wind_sample.csv"
CFG = load_exog_ingest_config(REPO / "config" / "assumptions.yaml")


def test_fixture_covers_both_clock_change_days():
    raw = pd.read_csv(FIXTURE)
    counts = raw.groupby("Date").size().to_dict()
    assert (
        counts["2025-10-26"] == 50
        and counts["2026-03-29"] == 46
        and counts["2024-03-11"] == 48
    )


def test_datetime_gmt_agrees_with_settlement_period_on_dst_days(caplog):
    with caplog.at_level("WARNING"):
        frame = nw.parse_csv(FIXTURE, CFG)
    assert "disagrees" not in caplog.text
    autumn = frame[
        frame["target_time_utc"]
        .dt.strftime("%Y-%m-%d")
        .isin(["2025-10-25", "2025-10-26"])
    ]
    assert pd.Timestamp("2025-10-25T23:00Z") in set(
        autumn["target_time_utc"]
    )  # SP1 of the 50-period day
    assert pd.Timestamp("2025-10-26T23:30Z") in set(autumn["target_time_utc"])  # SP50


def test_vintage_schema_and_day_ahead_stamp():
    out = vg.finalise_vintages(nw.parse_csv(FIXTURE, CFG), nw.SOURCE, CFG)
    assert list(out.columns) == vg.VINTAGE_COLUMNS
    assert (out["variable"] == "wind_neso_da_mw").all()
    assert out["forecast_made_utc"].notna().all()
    assert (out["value_mw"] <= out["capacity_mw"] * CFG.capacity_tolerance).all()
    normal = out[out["target_time_utc"].dt.strftime("%Y-%m-%d") == "2024-03-11"]
    assert (
        normal["forecast_made_utc"].dt.strftime("%Y-%m-%d %H:%M") == "2024-03-10 08:49"
    ).all()  # made the day before


def test_same_day_stamps_are_kept_literally_and_reported_in_coverage():
    out = vg.finalise_vintages(nw.parse_csv(FIXTURE, CFG), nw.SOURCE, CFG)
    sept = out[out["target_time_utc"].dt.strftime("%Y-%m-%d") == "2026-09-30"]
    assert (sept["forecast_made_utc"] == pd.Timestamp("2026-09-30T08:45Z")).all()
    assert (
        len(sept) < 48
    )  # periods before 08:45 on the day have made > target and were dropped as artefacts
    cov = nw.coverage_by_month(out, CFG)
    assert cov.loc[pd.Period("2024-03")] == 1.0 and cov.loc[pd.Period("2026-09")] == 0.0


def test_artefact_rows_made_after_target_are_dropped(caplog):
    raw = pd.read_csv(FIXTURE)
    raw.loc[raw["Date"] == "2024-03-11", "Forecast_Timestamp"] = (
        "2024-11-30T12:00:00"  # stamped months later
    )
    tmp = FIXTURE.parent / "_tmp_wind.csv"
    raw.to_csv(tmp, index=False)
    try:
        with caplog.at_level("WARNING"):
            out = vg.finalise_vintages(nw.parse_csv(tmp, CFG), nw.SOURCE, CFG)
    finally:
        tmp.unlink()
    assert "made after their target" in caplog.text
    assert not (out["target_time_utc"].dt.strftime("%Y-%m-%d") == "2024-03-11").any()


def test_plausibility_bounds_raise():
    frame = nw.parse_csv(FIXTURE, CFG)
    frame.loc[0, "value_mw"] = -5.0
    with pytest.raises(ValueError, match="outside plausible range"):
        vg.finalise_vintages(frame, nw.SOURCE, CFG)
