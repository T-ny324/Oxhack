"""Habits model -> optimiser interface.

    predict_next_week(sessions_last_week, week_start_utc)  -> sessions table

The output has the SAME schema as the synthetic sessions table (synthetic.py), so
daily.trace_from_sessions() and floor_learned() work unchanged:
    vehicle_id, arrival_utc, departure_utc, soc_required, trip_kwh_next, source
One predicted session per day the classifier says "plugs in" (p >= 0.5):
    arrival   = predicted P90 plug-in time  (late side: never count on a car too early)
    departure = predicted P10 departure     (early side: never plan past when it leaves)
    trip_kwh_next = predicted P90 trip energy
Wall-clock hours are converted to UTC with the same safe-side DST rule.

Also here: plan_inputs_from_predictions(), the "belief" the optimiser plans with in
run_daily_vs_reality: known reality now (is the car plugged in?) + predictions after.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from v2g.optimise.daily import floor_for_mode, trace_from_sessions
from v2g.optimise.lp import VehicleParams

from .features import HabitsConfig, build_features, local_hours_to_utc

ROOT = Path(__file__).resolve().parents[3]
MODEL = ROOT / "artefacts" / "habits" / "habits_model.joblib"
RESERVE = 0.10                                   # same reserve floor_learned applies


def load_model(path: Path = MODEL):
    return joblib.load(path)["model"]


def day_predictions(rows: pd.DataFrame, pred: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Per (vehicle, local day) predictions (any predictor) -> UTC times.

    Keeps EVERY day, including "won't plug in" days: if the car does turn up, the
    simulation still needs that day's departure and trip predictions.
    A day whose predicted departure is not after its predicted arrival cannot be a session;
    it is marked plugs_in = False (the plan then simply doesn't count on the car).
    """
    d = pd.DataFrame({"vehicle_id": rows["vehicle_id"].to_numpy(), "day": rows["day"].to_numpy(),
                      "p_plugs_in": pred["p_plugs_in"].to_numpy(),
                      "trip_kwh": pred["pred_trip_kwh"].clip(lower=0).to_numpy()})
    d["arrival_utc"] = local_hours_to_utc(d["day"], pred["pred_plugin_h"].reset_index(drop=True), tz, "late")
    d["departure_utc"] = local_hours_to_utc(d["day"], pred["pred_plugout_h"].reset_index(drop=True), tz, "early")
    d["valid"] = d["departure_utc"] > d["arrival_utc"] + pd.Timedelta(minutes=15)
    d["plugs_in"] = (d["p_plugs_in"] >= 0.5) & d["valid"]
    return d


def to_sessions(days: pd.DataFrame, params: VehicleParams) -> pd.DataFrame:
    """Predicted plug-in days -> the standard sessions table.

    If a predicted session would start before the previous one has left (car believed
    still plugged in), the later one is dropped and the EARLIER departure kept: the safe
    choice, since that departure is the one the plan must be ready for.
    """
    s = days[days["plugs_in"]].sort_values(["vehicle_id", "arrival_utc"])
    keep, last_dep = [], {}
    for idx, r in s.iterrows():
        if r.vehicle_id in last_dep and r.arrival_utc < last_dep[r.vehicle_id]:
            continue
        keep.append(idx)
        last_dep[r.vehicle_id] = r.departure_utc
    s = s.loc[keep]
    return pd.DataFrame({
        "vehicle_id": s["vehicle_id"].to_numpy(),
        "arrival_utc": s["arrival_utc"].to_numpy(),
        "departure_utc": s["departure_utc"].to_numpy(),
        # what floor_learned will hold at departure: soc_min + trip / E_cap + reserve
        "soc_required": np.minimum(params.soc_max, params.soc_min + s["trip_kwh"].to_numpy()
                                   / params.e_cap_kwh + RESERVE),
        "trip_kwh_next": s["trip_kwh"].to_numpy(),
        "source": "habits_model",
    })


def predict_next_week(sessions_last_week: pd.DataFrame, week_start_utc, model=None,
                      params: VehicleParams = VehicleParams(), cfg: HabitsConfig = HabitsConfig(),
                      return_days: bool = False):
    """Predict next week's sessions for every vehicle in sessions_last_week.

    sessions_last_week: the Electric Nation schema (vehicle_id, arrival_utc, departure_utc,
        energy_kwh, trip_kwh_next), at least the 7 days before week_start. Anything not yet
        known at week_start is masked by build_features, so passing extra history is safe.
    week_start_utc: Monday 00:00 UK time, as a UTC timestamp.
    """
    model = model or load_model()
    ws = pd.Timestamp(week_start_utc).tz_convert(cfg.tz).tz_localize(None)
    if ws != ws.normalize() or ws.day_of_week != 0:
        raise ValueError("week_start_utc must be Monday 00:00 local time")
    weeks = pd.DataFrame({"vehicle_id": sessions_last_week["vehicle_id"].unique(), "week_start": ws})
    rows = build_features(sessions_last_week, weeks, cfg)
    days = day_predictions(rows, model.predict(rows), cfg.tz)
    sessions = to_sessions(days, params)
    return (sessions, days) if return_days else sessions


# ======================================================================= belief
def plan_inputs_from_predictions(days: pd.DataFrame, true_sessions: pd.DataFrame,
                                 truth_plugged: np.ndarray, index: pd.DatetimeIndex,
                                 params: VehicleParams, tz: str,
                                 always_ready: bool = False,   # False = standard (product default)
                                 mode: str = "max_savings", target_soc: float = 0.80):
    """Build the plan_inputs callback for run_daily_vs_reality (one vehicle).

    mode / target_soc: the app setting (daily.MODES). "guaranteed_charge" adds SoC >= target
    at every slot the plan believes the car is plugged in (including the session it is in
    right now, which is known), on top of everything below.

    At a re-plan at slot i the optimiser knows ONE fact of reality: whether the car is
    plugged in right now (and since when). Everything else is prediction:
      * plugged in now -> it stays until the departure predicted for the day it arrived
        (P10, early side), and must then hold that day's P90 trip + reserve.
        If that departure is unknown or less than 1 h away (late arrivals often come after
        their day's P10 departure, which includes short same-day sessions), we do NOT know
        when it leaves: assume it stays until the end of the window (the next 10:00 re-plan
        corrects this) and require the trip energy from the next slot on. The optimiser
        then charges to the requirement first and can only sell energy above it.
        (An earlier version assumed "leaves next slot": safe for selling, but then nothing
        charged until the next morning's re-plan and cars left nearly empty.)
      * always_ready=True: require the trip energy from the next slot on in EVERY session,
        not only at the predicted departure: safe against early departures, but gives up
        selling below the requirement (e.g. at the evening peak, recharged overnight).
      * later: the predicted sessions of later days (classifier says yes), from now on.
    """
    days = days.set_index("day").sort_index()
    week_max_trip = float(days["trip_kwh"].max())
    dt = pd.Timedelta(hours=params.dt_h)

    def plan_inputs(i: int, j: int):
        now = index[i]
        rows, after_day, hold = [], None, None       # hold = (last slot, required SoC)
        if truth_plugged[i]:
            cur = true_sessions[(true_sessions["arrival_utc"] <= now + dt)
                                & (true_sessions["departure_utc"] > now)]
            arr_day = cur["arrival_utc"].min().tz_convert(tz).tz_localize(None).normalize()
            if arr_day in days.index:
                dep, trip = days.at[arr_day, "departure_utc"], days.at[arr_day, "trip_kwh"]
            else:                                    # arrived before the predicted week
                dep, trip = pd.NaT, week_max_trip
            need = min(params.soc_max, params.soc_min + trip / params.e_cap_kwh + RESERVE)
            if pd.isna(dep) or dep <= now + pd.Timedelta(hours=1):
                rows.append((now, pd.NaT, trip))      # departure unknown: stays to window end
                hold = (j - i, need)
            else:
                rows.append((now, dep, trip))
                if always_ready:
                    hold = (int(np.floor((dep - now) / dt)), need)
            after_day = arr_day
        later = days[days["plugs_in"] & (days["arrival_utc"] >= now)]
        if after_day is not None:
            later = later[later.index > after_day]
        for _, r in later.iterrows():
            if rows and pd.isna(rows[-1][1]):
                break                                              # current session open-ended
            if not rows or r.arrival_utc >= rows[-1][1]:           # no overlap with the previous
                rows.append((r.arrival_utc, r.departure_utc, r.trip_kwh))
        s = pd.DataFrame(rows, columns=["arrival_utc", "departure_utc", "trip_kwh_next"])
        tr = trace_from_sessions(s, index[i:j], params.dt_h)
        # The plan does NOT simulate the predicted drain while the car is away (drive = 0):
        # the trip is protected by the departure floor (soc_min + trip / E_cap + reserve),
        # and the plan is redone from the REAL SoC when the car comes back. Simulating a big
        # P90 trip could also drive the plan's SoC below 0, which lp.py treats as infeasible.
        drive = np.zeros(j - i)
        floor = floor_for_mode(mode, tr.plugged, tr.departures, params, target_soc, RESERVE)
        if hold is not None:                         # require the trip energy from the next slot
            k, need = hold
            floor[1:k + 1] = np.maximum(floor[1:k + 1], need)
        return tr.plugged, drive, floor

    return plan_inputs
