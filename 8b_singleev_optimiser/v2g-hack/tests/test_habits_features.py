"""Habits dataset (step 3): no leakage, DST-safe time conversion, clean splits."""
import numpy as np
import pandas as pd
import pytest

from v2g.habits.features import (FEATURES, OUT, HabitsConfig, assign_splits, build_features,
                                 local_hours_to_utc, local_midnight_utc)
from v2g.ingest.electric_nation import IngestConfig, add_trip_proxy

CFG = HabitsConfig()
TZ = CFG.tz


def commuter(weeks=5, seed=0):
    """One car: plugged in ~18:00 -> ~07:00 local every day, random energy, a few 0 kWh."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2018-10-01", periods=7 * weeks, freq="D")      # spans the Oct DST change
    arr = pd.Series(days + pd.to_timedelta(rng.normal(18, 1, len(days)), unit="h"))
    dep = pd.Series(days + pd.to_timedelta(rng.normal(31, 0.5, len(days)), unit="h"))
    s = pd.DataFrame({
        "vehicle_id": "ev0",
        "arrival_utc": arr.dt.tz_localize(TZ).dt.tz_convert("UTC"),
        "departure_utc": dep.dt.tz_localize(TZ).dt.tz_convert("UTC"),
        "energy_kwh": np.where(rng.random(len(days)) < 0.15, 0.0, rng.gamma(3, 3, len(days))),
    })
    return add_trip_proxy(s, IngestConfig())


def test_features_ignore_everything_after_the_cutoff():
    """Change every fact that only becomes known after week_start: features must not move."""
    s = commuter()
    week = pd.Timestamp("2018-10-22")                                 # a Monday
    weeks = pd.DataFrame({"vehicle_id": ["ev0"], "week_start": [week]})
    cutoff = local_midnight_utc(week, TZ)

    future = s.copy()
    after = future["arrival_utc"] >= cutoff
    future.loc[after, "energy_kwh"] *= 10                             # future charges
    future.loc[after, ["arrival_utc", "departure_utc"]] += pd.Timedelta(hours=3)
    ongoing = (future["arrival_utc"] < cutoff) & (future["departure_utc"] > cutoff)
    future.loc[ongoing, "departure_utc"] += pd.Timedelta(hours=5)     # still plugged at cutoff
    future = add_trip_proxy(future.drop(columns="trip_kwh_next"), IngestConfig())

    a = build_features(s, weeks, CFG)[FEATURES]
    b = build_features(future, weeks, CFG)[FEATURES]
    pd.testing.assert_frame_equal(a, b)
    # sanity: the targets DID change, so the perturbation was real
    assert not build_features(s, weeks, CFG)["y_trip_kwh"].equals(build_features(future, weeks, CFG)["y_trip_kwh"])


def test_hours_are_local_wall_clock_across_dst():
    """18:00 local is 18.0 both sides of the Oct clock change, although UTC moves by 1 h."""
    s = commuter()
    weeks = pd.DataFrame({"vehicle_id": ["ev0"] * 2, "week_start": pd.to_datetime(["2018-10-22", "2018-10-29"])})
    f = build_features(s, weeks, CFG)
    p = f[f["y_plugs_in"] == 1]
    assert p["y_plugin_h"].between(0, 24).all()
    assert (p["y_plugout_h"] > p["y_plugin_h"]).all()
    assert p["y_plugin_h"].mean() == pytest.approx(18, abs=0.6)       # no 1 h jump after DST


@pytest.mark.parametrize("day, hours, side, expect_utc", [
    ("2018-10-27", 32.5, "early", "2018-10-28 08:30"),   # next morning, after clocks went back (GMT)
    ("2018-10-28", 1.5, "early", "2018-10-28 00:30"),    # 01:30 happens twice: first (BST) one
    ("2018-10-28", 1.5, "late", "2018-10-28 01:30"),     # ... or the second (GMT) one
    ("2018-03-25", 1.5, "late", "2018-03-25 01:00"),     # 01:30 does not exist: jump to 02:00 BST
    ("2018-07-01", 18.0, "late", "2018-07-01 17:00"),    # ordinary summer day
])
def test_local_hours_to_utc_dst(day, hours, side, expect_utc):
    out = local_hours_to_utc(pd.Series([pd.Timestamp(day)]), pd.Series([hours]), TZ, side)
    assert out.iloc[0] == pd.Timestamp(expect_utc, tz="UTC")


def test_nonexistent_time_early_side_is_before_the_gap():
    out = local_hours_to_utc(pd.Series([pd.Timestamp("2018-03-25")]), pd.Series([1.5]), TZ, "early")
    assert out.iloc[0] < pd.Timestamp("2018-03-25 01:00", tz="UTC")


def test_splits_hold_out_vehicles_and_time():
    df = pd.DataFrame({"vehicle_id": np.repeat([f"v{i}" for i in range(20)], 10),
                       "week_start": np.tile(pd.date_range("2018-01-01", periods=10, freq="7D"), 20)})
    sp = assign_splits(df, CFG)
    test_v = set(df.loc[sp == "test", "vehicle_id"])
    assert len(test_v) == 4 and not test_v & set(df.loc[sp != "test", "vehicle_id"])
    assert df.loc[sp == "train", "week_start"].max() < df.loc[sp == "val", "week_start"].min()


# ------------------------------------------------------------- the real dataset
@pytest.fixture(scope="module")
def real():
    if not OUT.exists():
        pytest.skip("run `python -m v2g.habits.features` first")
    return pd.read_parquet(OUT)


def test_real_dataset_shapes(real):
    assert not real.duplicated(["vehicle_id", "day"]).any()
    assert set(real.loc[real["split"] == "test", "vehicle_id"]).isdisjoint(real.loc[real["split"] != "test", "vehicle_id"])
    p = real[real["y_plugs_in"] == 1]
    assert p["y_plugin_h"].between(0, 24).all() and (p["y_plugout_h"] > p["y_plugin_h"]).all()
    assert (p["y_trip_kwh"].dropna() >= 0).all()
