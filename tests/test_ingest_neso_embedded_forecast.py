"""Tests for v2g.ingest.neso_embedded_forecast and the settlement-period helper (no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from v2g.forecast.timing import settlement_period_to_utc
from v2g.ingest import neso_embedded_forecast as emb
from v2g.ingest import vintages as vg
from v2g.ingest.config import load_exog_ingest_config

REPO = Path(__file__).resolve().parents[1]
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "exog"
    / "neso_embedded_2025-01-01T0912_sample.csv"
)
CFG = load_exog_ingest_config(REPO / "config" / "assumptions.yaml")


def test_settlement_period_to_utc_on_both_clock_change_days():
    # autumn: 50 periods; SP1 = 00:00 BST = 23:00Z the day before, SP50 = 23:30 GMT
    autumn = settlement_period_to_utc(
        ["2025-10-26"] * 50, range(1, 51), "Europe/London"
    )
    assert autumn[0] == pd.Timestamp("2025-10-25T23:00Z") and autumn[
        -1
    ] == pd.Timestamp("2025-10-26T23:30Z")
    assert ((autumn[1:] - autumn[:-1]) == pd.Timedelta("30min")).all()
    # spring: 46 periods; SP1 = 00:00 GMT, SP46 = 23:30 BST = 22:30Z
    spring = settlement_period_to_utc(
        ["2026-03-29"] * 46, range(1, 47), "Europe/London"
    )
    assert spring[0] == pd.Timestamp("2026-03-29T00:00Z") and spring[
        -1
    ] == pd.Timestamp("2026-03-29T22:30Z")
    # ordinary GMT and BST days
    assert settlement_period_to_utc(["2025-11-10"], [1], "Europe/London")[
        0
    ] == pd.Timestamp("2025-11-10T00:00Z")
    assert settlement_period_to_utc(["2025-07-01"], [1], "Europe/London")[
        0
    ] == pd.Timestamp("2025-06-30T23:00Z")
    assert str(spring.dtype) == "datetime64[ns, UTC]"
    # tz-aware date labels (the archive writes 2025-01-01T00:00:00Z) are treated as plain dates
    assert settlement_period_to_utc(["2025-01-01T00:00:00Z"], [3], "Europe/London")[
        0
    ] == pd.Timestamp("2025-01-01T01:00Z")


def test_parse_archive_fixture_into_two_variables():
    out = vg.finalise_vintages(emb.parse_archive(FIXTURE, CFG), emb.SOURCE, CFG)
    assert list(out.columns) == vg.VINTAGE_COLUMNS
    assert set(out["variable"]) == {"embedded_wind_mw", "embedded_solar_mw"}
    assert (out["forecast_made_utc"] == pd.Timestamp("2025-01-01T09:12Z")).all()
    lead_h = (out["target_time_utc"] - out["forecast_made_utc"]) / pd.Timedelta(hours=1)
    assert lead_h.max() <= CFG.max_lead_hours and (lead_h > 0).all()
    wind = out[out["variable"] == "embedded_wind_mw"].set_index("target_time_utc")
    assert (wind.index.to_series().diff().dropna() == pd.Timedelta("30min")).all()
    assert wind.index[0] == pd.Timestamp(
        "2025-01-01T09:30Z"
    )  # first period after the 09:12 publication
    assert (wind["capacity_mw"] == 6606.0).all() and (
        wind["value_mw"] <= wind["capacity_mw"]
    ).all()
    solar = out[out["variable"] == "embedded_solar_mw"]
    assert (solar["value_mw"] >= 0).all() and solar["capacity_mw"].between(
        17000, 17300
    ).all()
    assert out["forecast_made_utc"].notna().all()


def test_time_gmt_is_period_end_and_cross_checks_with_settlement_period(caplog):
    raw = pd.read_csv(FIXTURE)
    end = pd.to_datetime(raw["DATE_GMT"], utc=True) + emb._parse_time_gmt(
        raw["TIME_GMT"]
    )
    start = settlement_period_to_utc(
        raw["SETTLEMENT_DATE"], raw["SETTLEMENT_PERIOD"], CFG.timezone
    )
    assert ((end - pd.Timedelta(minutes=30)).to_numpy() == start.to_numpy()).all()
    bad = raw.copy()
    bad.loc[0, "TIME_GMT"] = "05:00:00"  # disagrees with the settlement period
    with caplog.at_level("WARNING"):
        emb.chunk_to_frame(bad, CFG)
    assert "disagrees with settlement" in caplog.text
    assert (
        emb._parse_time_gmt(pd.Series(["02:30", "02:30:00"])).tolist()
        == [pd.Timedelta("2h30min")] * 2
    )


def test_rows_without_made_time_and_duplicates_handled(caplog):
    raw = pd.read_csv(FIXTURE)
    raw.loc[0:1, "Forecast_Datetime"] = np.nan
    frame = emb.chunk_to_frame(pd.concat([raw, raw.iloc[5:8]]), CFG)
    with caplog.at_level("INFO"):
        out = vg.finalise_vintages(frame, emb.SOURCE, CFG)
    assert "duplicate" in caplog.text
    assert not out.duplicated(
        ["variable", "target_time_utc", "forecast_made_utc"]
    ).any()
    assert out["forecast_made_utc"].notna().all()


def test_capacity_tolerance_violation_raises():
    frame = emb.chunk_to_frame(pd.read_csv(FIXTURE), CFG)
    frame.loc[frame["variable"] == "embedded_wind_mw", "value_mw"] = (
        9_000.0  # capacity is 6606
    )
    with pytest.raises(ValueError, match="exceed capacity"):
        vg.finalise_vintages(frame, emb.SOURCE, CFG)


def test_resource_urls_parsed_from_package_show(tmp_path):
    payload = {
        "result": {
            "resources": [
                {
                    "name": "Embedded Solar and Wind Forecast Archive 2025",
                    "url": "https://x/2025.csv",
                },
                {
                    "name": "Embedded Solar and Wind Forecast",
                    "url": "https://x/live.csv",
                },
                {
                    "name": "Embedded Solar and Wind Forecast Archive 2026",
                    "url": "https://x/2026",
                },
            ]
        }
    }
    (tmp_path / "package_show.json").write_text(__import__("json").dumps(payload))
    assert emb.resource_urls(None, tmp_path) == {
        2025: "https://x/2025.csv",
        2026: "https://x/2026",
    }
