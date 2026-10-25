"""Step 7: habits model -> optimiser interface, and the plan-vs-reality replay."""
import numpy as np
import pandas as pd
import pytest

from shared.units import utc_index
from v2g.habits.features import OUT as DATASET, HabitsConfig
from v2g.habits.predict import MODEL, plan_inputs_from_predictions, predict_next_week, to_sessions
from v2g.ingest.synthetic import synthetic_prices, synthetic_sessions
from v2g.optimise.daily import floor_learned, run_daily_vs_reality, trace_from_sessions
from v2g.optimise.lp import VehicleParams

P = VehicleParams()
TZ = HabitsConfig().tz
SCHEMA = ["vehicle_id", "arrival_utc", "departure_utc", "soc_required", "trip_kwh_next"]


# ---------------------------------------------------------------- a commuter week
def local(day, h):
    return (pd.Timestamp(day) + pd.Timedelta(hours=h)).tz_localize(TZ).tz_convert("UTC")


START = local("2018-11-05", 10)                       # Monday 10:00 (= UTC in November)
DAYS = pd.date_range("2018-11-05", periods=7, freq="D")


def true_sessions(trip=9.0):
    """Plugged in 18:00 -> 07:30 every day; 9 kWh commute. Starts plugged in on Sunday."""
    arr = [local("2018-11-04", 18)] + [local(d, 18) for d in DAYS] + [local("2018-11-12", 18)]
    dep = [local(d, 7.5) for d in DAYS] + [local("2018-11-12", 7.5), local("2018-11-13", 7.5)]
    return pd.DataFrame({"vehicle_id": "ev0", "arrival_utc": arr, "departure_utc": dep, "trip_kwh_next": trip})


def predicted_days(dep_h, trip, p=0.9, arr_h=18.5):
    """day_predictions()-shaped frame: the same prediction every day."""
    return pd.DataFrame({"vehicle_id": "ev0", "day": DAYS, "p_plugs_in": p, "trip_kwh": trip,
                         "arrival_utc": [local(d, arr_h) for d in DAYS],
                         "departure_utc": [local(d, dep_h) for d in DAYS],
                         "valid": True, "plugs_in": p >= 0.5})


def replay(days, trip=9.0, always_ready=False, mode="max_savings", target_soc=0.8):
    index = utc_index(START.tz_localize(None), START.tz_localize(None) + pd.Timedelta(days=9), P.dt_h)
    s = true_sessions(trip)
    truth = trace_from_sessions(s, index, P.dt_h)
    w = synthetic_prices(START.tz_localize(None), 9, P.dt_h, TZ, seed=1)["price_wholesale_per_mwh"].to_numpy()
    pi = plan_inputs_from_predictions(days, s, truth.plugged, index, P, TZ, always_ready=always_ready,
                                      mode=mode, target_soc=target_soc)
    T = 7 * 48
    return run_daily_vs_reality(P, truth, w + 15, w - 5, pi, 0.6, TZ, execute_until=T), truth, T


# ---------------------------------------------------------------- predicted sessions
def test_predicted_sessions_schema_and_validity():
    s = to_sessions(predicted_days(31.0, 12.0), P)
    assert list(s.columns[:5]) == SCHEMA
    for c in ("arrival_utc", "departure_utc"):
        assert str(s[c].dt.tz) == "UTC"
    assert (s["departure_utc"] > s["arrival_utc"]).all()
    assert (s["trip_kwh_next"] >= 0).all()
    assert s["soc_required"].between(P.soc_min, P.soc_max).all()
    # same columns/dtypes the optimiser already consumes from the synthetic table
    syn = synthetic_sessions("2018-11-05", 2, 1, TZ, 0.6)
    for c in SCHEMA:
        assert s[c].dtype.kind == syn[c].dtype.kind


def test_overlapping_predictions_keep_the_earlier_departure():
    """A predicted 3-day stay overlaps the next days' plug-ins: keep the earliest departure."""
    s = to_sessions(predicted_days(80.0, 12.0), P)
    assert (s["arrival_utc"].iloc[1:].to_numpy() >= s["departure_utc"].iloc[:-1].to_numpy()).all()


def test_predicted_sessions_work_with_trace_and_floor_unchanged():
    s = to_sessions(predicted_days(31.0, 12.0), P)
    index = utc_index(START.tz_localize(None), START.tz_localize(None) + pd.Timedelta(days=7), P.dt_h)
    tr = trace_from_sessions(s, index, P.dt_h)
    floor = floor_learned(len(index), tr.departures, P)
    assert tr.plugged.any() and len(tr.departures) >= 5
    assert np.isclose(tr.drive_kwh.sum(), sum(e for _, e in tr.departures))
    assert floor.min() >= P.soc_min and floor.max() <= P.soc_max


# ---------------------------------------------------------------- plan vs reality
@pytest.mark.parametrize("dep_h, trip, ready", [
    (31.0, 12.0, False),    # good, cautious prediction
    (34.0, 3.0, False),     # bad: departure predicted 2.5 h too late, trip far too small
    (17.0, 12.0, False),    # predicted departure before arrival -> "departure unknown" path
    (34.0, 3.0, True),      # bad prediction, always-ready mode
])
def test_replay_respects_physical_limits(dep_h, trip, ready):
    r, truth, T = replay(predicted_days(dep_h, trip), always_ready=ready)
    soc, eps = r["soc"], 1e-9
    away = ~truth.plugged[:T]
    assert (r["p_ch"][away] == 0).all() and (r["p_dis"][away] == 0).all()    # car away: no power
    assert (r["p_ch"] >= 0).all() and (r["p_dis"] >= 0).all()
    assert soc.max() <= P.soc_max + eps                                      # never overfilled
    sold = r["p_dis"] > 0
    assert (soc[1:][sold] >= P.soc_min - eps).all()                          # never sold below soc_min
    assert not ((r["p_ch"] > 0) & (r["p_dis"] > 0)).any()
    assert any(d["on_arrival"] for d in r["days"])                           # re-planned on arrival
    assert len(r["departures"]) == len([d for d, _ in truth.departures if d < T])


def test_good_prediction_has_no_shortfall_and_still_sells():
    r, _, _ = replay(predicted_days(31.0, 12.0))
    assert max(d["short_kwh"] for d in r["departures"]) < 1e-6
    assert r["p_dis"].sum() > 0                                              # V2G actually happens


def test_bad_prediction_is_logged_not_hidden():
    """Trip 4x bigger than predicted and leaves early: shortfalls must show up in the log."""
    r, _, _ = replay(predicted_days(34.0, 3.0), trip=36.0)
    assert any(d["short_kwh"] > 0 for d in r["departures"])


@pytest.mark.parametrize("dep_h, trip", [(31.0, 12.0), (34.0, 3.0)])   # good and bad predictions
def test_guaranteed_charge_with_predicted_habits(dep_h, trip):
    """Guaranteed charge does not rely on predictions: even when the departure is predicted
    2.5 h too late, the car leaves at >= target and never sells below it."""
    r, truth, T = replay(predicted_days(dep_h, trip), mode="guaranteed_charge", target_soc=0.8)
    assert min(d["soc"] for d in r["departures"]) >= 0.8 - 1e-6
    sold = r["p_dis"] > 0
    assert (r["soc"][1:][sold] >= 0.8 - 1e-6).all()


def test_predict_rejects_non_monday():
    with pytest.raises(ValueError):
        predict_next_week(true_sessions().assign(energy_kwh=5.0), local("2018-11-06", 0), model=object())


# ---------------------------------------------------------------- real data + saved model
@pytest.fixture(scope="module")
def real():
    if not (DATASET.exists() and MODEL.exists()):
        pytest.skip("run the ingest, features and model steps first")
    import joblib
    return pd.read_parquet(DATASET), pd.read_parquet(DATASET.with_name("sessions.parquet")), \
        joblib.load(MODEL)["model"]


def test_predict_next_week_matches_dataset_and_is_valid(real):
    df, sessions, model = real
    test = df[df["split"] == "test"]
    ws = test["week_start"].value_counts().index[0]                    # a busy test week
    vids = test.loc[test["week_start"] == ws, "vehicle_id"].unique()[:5]
    ws_utc = ws.tz_localize(TZ).tz_convert("UTC")
    last_week = sessions[sessions["vehicle_id"].isin(vids)
                         & (sessions["arrival_utc"] >= ws_utc - pd.Timedelta(days=8))
                         & (sessions["arrival_utc"] < ws_utc)]
    pred, days = predict_next_week(last_week, ws_utc, model=model, return_days=True)

    # same answer as the stored dataset rows: no train/serve mismatch, no hidden leakage
    rows = df[df["vehicle_id"].isin(vids) & (df["week_start"] == ws)].sort_values(["vehicle_id", "day"])
    ref = model.predict(rows)
    got = days.sort_values(["vehicle_id", "day"])
    assert np.allclose(got["p_plugs_in"], ref["p_plugs_in"])
    assert np.allclose(got["trip_kwh"], ref["pred_trip_kwh"])

    assert list(pred.columns[:5]) == SCHEMA and len(pred) > 0
    assert str(pred["arrival_utc"].dt.tz) == "UTC" and str(pred["departure_utc"].dt.tz) == "UTC"
    assert (pred["departure_utc"] > pred["arrival_utc"]).all()
    assert (pred["trip_kwh_next"] >= 0).all()
    assert (pred["arrival_utc"] >= ws_utc).all()
