"""Electric Nation ingest: small hand-built readings, plus checks on the real output if present."""
import numpy as np
import pandas as pd
import pytest

from v2g.ingest.electric_nation import OUT, IngestConfig, build_sessions, load_readings

CFG = IngestConfig()
TZ = "Europe/London"


def readings(charger, start_utc, n, amps, tx):
    """n consecutive 3-minute readings starting at start_utc (UTC)."""
    ts = pd.date_range(pd.Timestamp(start_utc, tz="UTC"), periods=n, freq="3min")
    return pd.DataFrame({"charger": charger, "ts_utc": ts, "amps": np.broadcast_to(amps, n).astype(float),
                         "tx": pd.array([tx] * n, dtype="Int64")})


def build(*parts):
    return build_sessions(pd.concat(parts, ignore_index=True), CFG)


def test_load_csv_is_utc_and_reads_nulls(tmp_path):
    """BOM header, 'NULL' strings, and TimeStamp interpreted as UTC (not local)."""
    p = tmp_path / "gf3.csv"
    p.write_text("﻿ChargerID,GroupID,TimeStamp,AdjustedTimeStamp,MaxAmpsUsed,AmpsAllocated,TransactionID\n"
                 "UK-1,G,2018-10-28 00:30:00.000,2018-10-28 00:30:00.000,16.0,32,NULL\n"
                 "UK-1,G,2018-10-28 00:33:00.000,2018-10-28 00:33:00.000,16.0,32,7\n")
    r = load_readings(p)
    assert str(r["ts_utc"].dt.tz) == "UTC"
    assert r["tx"].isna().tolist() == [True, False]
    assert r["ts_utc"].iloc[0] == pd.Timestamp("2018-10-28 00:30", tz="UTC")


@pytest.mark.parametrize("day, expect_local_hours", [
    # autumn: clocks go back at 01:00 UTC. 00:30 UTC is still BST (01:30 local); the source's
    # AdjustedTimeStamp wrongly says 00:30 here, which is why we derive local time ourselves.
    ("2018-10-28", {"00:30": 1.5, "01:30": 1.5}),
    # spring: clocks go forward at 01:00 UTC. 00:30 UTC is GMT (00:30), 01:30 UTC is 02:30 BST.
    ("2018-03-25", {"00:30": 0.5, "01:30": 2.5}),
])
def test_clock_change_day(day, expect_local_hours):
    """A car plugged in across the clock change: one session, true UTC duration, right local time."""
    s, _ = build(readings("UK-1", f"{day} 00:00", 40, 10.0, 1))        # 00:00 -> 02:00 UTC
    assert len(s) == 1
    assert s["departure_utc"].iloc[0] - s["arrival_utc"].iloc[0] == pd.Timedelta(hours=2)
    for hhmm, h in expect_local_hours.items():
        loc = pd.Timestamp(f"{day} {hhmm}", tz="UTC").tz_convert(TZ)
        assert loc.hour + loc.minute / 60 == h


def test_duplicates_do_not_double_count():
    r = readings("UK-1", "2018-05-01 18:00", 20, 16.0, 1)             # 1 h at 16 A
    s, st = build(r, r)                                                # every reading twice
    assert st["readings_dropped_duplicate"] == 20
    assert s["energy_kwh"].iloc[0] == pytest.approx(16 * 230 / 1000 * 1.0)


def test_silent_gap_splits_and_back_to_back_merges():
    s, _ = build(
        readings("UK-1", "2018-05-01 18:00", 20, 16.0, 1),
        readings("UK-1", "2018-05-02 18:00", 20, 16.0, 1),             # same tx, a day of silence
        readings("UK-1", "2018-05-02 19:06", 20, 16.0, 2),             # new tx 6 min later: same plug-in
    )
    assert len(s) == 2
    assert s["n_fragments"].tolist() == [1, 2]


def test_short_dropped_zero_energy_kept():
    s, st = build(
        readings("UK-1", "2018-05-01 08:00", 1, 16.0, 1),              # 3 min: broken
        readings("UK-1", "2018-05-01 18:00", 40, 0.0, 2),              # 2 h, no energy: KEEP
    )
    assert st["dropped_under_min_duration"] == 1
    assert len(s) == 1 and s["energy_kwh"].iloc[0] == 0


def test_trip_proxy_skips_zero_energy_sessions():
    """[charge, 0 kWh, charge 12 kWh]: both trips before the 12 kWh charge get 12, not 0."""
    s, _ = build(
        readings("UK-1", "2018-05-01 18:00", 20, 16.0, 1),
        readings("UK-1", "2018-05-02 18:00", 20, 0.0, 2),
        readings("UK-1", "2018-05-03 18:00", 40, 6 / (230 / 1000), 3),   # 6 kW for 2 h = 12 kWh
    )
    assert s["trip_kwh_next"].iloc[:2].tolist() == pytest.approx([12, 12])
    assert np.isnan(s["trip_kwh_next"].iloc[2])                      # no later charge seen


# --------------------------------------------------------- the real processed table
@pytest.fixture(scope="module")
def real():
    if not OUT.exists():
        pytest.skip("run `python -m v2g.ingest.electric_nation` first")
    return pd.read_parquet(OUT)


def test_real_utc_and_order(real):
    for c in ("arrival_utc", "departure_utc"):
        assert str(real[c].dt.tz) == "UTC"
    assert (real["departure_utc"] > real["arrival_utc"]).all()


def test_real_no_overlaps(real):
    s = real.sort_values(["vehicle_id", "arrival_utc"])
    prev_dep = s.groupby("vehicle_id")["departure_utc"].shift()
    assert not (s["arrival_utc"] < prev_dep).any()


def test_real_energy_non_negative(real):
    assert (real["energy_kwh"] >= 0).all()
    assert (real["trip_kwh_next"].dropna() >= CFG.min_charge_kwh).all()
    assert (real["source"] == "electric_nation").all()
