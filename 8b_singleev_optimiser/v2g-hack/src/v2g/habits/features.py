"""Habits model (Option B), step 3: sessions table -> supervised dataset.

    python -m v2g.habits.features     # writes data/processed/habits_dataset.parquet

One row per vehicle per LOCAL day. A prediction is made once a week, at Monday 00:00
local ("week_start"), for the 7 days that follow, using ONLY what was known by then:
the 7 days before week_start. The same code builds features for training and for
predict_next_week (step 7), so there is no train/serve mismatch.

Times are hours since local midnight of the target day (wall clock, Europe/London), so a
07:30 departure is 7.5 in winter and in summer. Departures can be on a later day: 08:30
the next morning is 32.5. local_hours_to_utc() converts back, handling DST.

The day's "main" session is the LONGEST session that starts that day: for a commuter
that is the overnight plug-in, not a 20-minute top-up. Targets per day:
    y_plugs_in   does a session start today?                       (all rows)
    y_plugin_h   main session's arrival, hours after midnight      (plug-in days only)
    y_plugout_h  main session's departure, hours after midnight    (plug-in days only)
    y_trip_kwh   main session's trip_kwh_next: energy needed for the trip(s) before the
                 next charge (see electric_nation.add_trip_proxy)  (plug-in days only)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from v2g.ingest.electric_nation import IngestConfig

ROOT = Path(__file__).resolve().parents[3]
SESSIONS = ROOT / "data" / "processed" / "sessions.parquet"
OUT = ROOT / "data" / "processed" / "habits_dataset.parquet"
H = pd.Timedelta(hours=1)

# England & Wales bank holidays (gov.uk) covering the trial. Hard-coded: no network needed.
UK_BANK_HOLIDAYS = pd.to_datetime([
    "2017-01-02", "2017-04-14", "2017-04-17", "2017-05-01", "2017-05-29", "2017-08-28",
    "2017-12-25", "2017-12-26",
    "2018-01-01", "2018-03-30", "2018-04-02", "2018-05-07", "2018-05-28", "2018-08-27",
    "2018-12-25", "2018-12-26",
    "2019-01-01", "2019-04-19", "2019-04-22", "2019-05-06", "2019-05-27", "2019-08-26",
    "2019-12-25", "2019-12-26",
])


@dataclass(frozen=True)
class HabitsConfig:
    tz: str = "Europe/London"
    # A gap with no sessions longer than this is "unobserved" (charger offline / out of use),
    # not behaviour: 99 % of gaps are < 13.3 days; the few > 14 d hold ~25k vehicle-days.
    max_away_days: int = 14
    test_vehicle_frac: float = 0.2              # whole vehicles held out (unseen drivers)
    val_week_frac: float = 0.2                  # last ~20 % of weeks of training vehicles
    seed: int = 0
    # Asymmetric loss: a dangerous error costs w times a safe one. Weight w <-> quantile
    # w / (w + 1) on the side where being LOW is dangerous, 1 / (w + 1) where HIGH is.
    asym_weight_w: float = 9.0
    # A shortfall only counts if it matters: > 1 kWh missing or leaving > 15 min early
    # (the strict "any amount" version flags trivial misses; agreed headline definition).
    shortfall_tol_kwh: float = 1.0
    shortfall_tol_h: float = 0.25

    @property
    def quantiles(self) -> dict[str, float]:
        hi, lo = self.asym_weight_w / (self.asym_weight_w + 1), 1 / (self.asym_weight_w + 1)
        return {"y_trip_kwh": hi,      # energy predicted too LOW strands the driver -> P90
                "y_plugout_h": lo,     # departure predicted too LATE -> P10
                "y_plugin_h": hi}      # plug-in predicted too EARLY -> P90


TARGETS = ["y_plugs_in", "y_plugin_h", "y_plugout_h", "y_trip_kwh"]


# ===================================================================== time helpers
def to_local_naive(ts_utc: pd.Series, tz: str) -> pd.Series:
    """UTC -> local wall-clock time without tz (so differences are wall-clock hours)."""
    return ts_utc.dt.tz_convert(tz).dt.tz_localize(None)


def local_midnight_utc(day: pd.Series | pd.Timestamp, tz: str):
    """Local midnight of a (naive) day -> UTC. Midnight is never ambiguous in the UK."""
    if isinstance(day, pd.Timestamp):
        return day.tz_localize(tz).tz_convert("UTC")
    return day.dt.tz_localize(tz).dt.tz_convert("UTC")


def local_hours_to_utc(day: pd.Series, hours: pd.Series, tz: str, side: str) -> pd.Series:
    """(local day, hours after its midnight) -> UTC, DST-safe.

    Wall-clock times can be ambiguous (01:30 happens twice when clocks go back) or not exist
    (01:30 is skipped when they go forward). We resolve them on the SAFE side for the target:
      side="early": first occurrence / jump back  -> use for departures (late is dangerous)
      side="late" : second occurrence / jump ahead -> use for plug-ins (early is dangerous)
    """
    wall = pd.to_datetime(day) + pd.to_timedelta(hours, unit="h")
    early = side == "early"
    amb = np.full(len(wall), early)                     # True = DST (first) occurrence
    loc = wall.dt.tz_localize(tz, ambiguous=amb,
                              nonexistent="shift_backward" if early else "shift_forward")
    return loc.dt.tz_convert("UTC")


# ===================================================================== day table
def day_table(sessions: pd.DataFrame, tz: str, min_charge_kwh: float) -> pd.DataFrame:
    """Sessions -> one row per (vehicle, local day with >= 1 plug-in) describing that day.

    Also records WHEN each value becomes known, so features can be masked at a cutoff:
      dep_utc        main session's departure     (known once the car has left)
      trip_known_utc departure of the session whose charge defines trip_kwh_next
      energy_known_utc last departure among the day's sessions (energy totals final)
    """
    s = sessions.sort_values(["vehicle_id", "arrival_utc"]).reset_index(drop=True)
    # When does trip_kwh_next become known? When the session supplying it (the next one that
    # took energy, same rule as the ingest) has finished. Same shift/bfill trick as ingest.
    charged_dep = s["departure_utc"].where(s["energy_kwh"] >= min_charge_kwh)
    s["trip_known_utc"] = (charged_dep.groupby(s["vehicle_id"]).shift(-1)
                           .groupby(s["vehicle_id"]).bfill())
    arr_loc = to_local_naive(s["arrival_utc"], tz)
    s["day"] = arr_loc.dt.normalize()
    s["plugin_h"] = (arr_loc - s["day"]) / H
    s["plugout_h"] = (to_local_naive(s["departure_utc"], tz) - s["day"]) / H
    s["dur_h"] = s["plugout_h"] - s["plugin_h"]

    g = s.groupby(["vehicle_id", "day"])
    main = s.loc[g["dur_h"].idxmax()].set_index(["vehicle_id", "day"])
    d = pd.DataFrame({
        "n_sessions": g.size(),
        "energy_day": g["energy_kwh"].sum(),            # energy CHARGED in sessions starting today
        "energy_known_utc": g["departure_utc"].max(),
        "plugin_h": main["plugin_h"], "plugout_h": main["plugout_h"],
        "trip_kwh": main["trip_kwh_next"],
        "dep_utc": main["departure_utc"], "trip_known_utc": main["trip_known_utc"],
    })
    return d.reset_index()


def observed_days(sessions: pd.DataFrame, cfg: HabitsConfig) -> pd.DataFrame:
    """All (vehicle, local day) we can trust: inside the vehicle's active period and not
    inside a no-session gap longer than max_away_days (charger probably offline)."""
    s = sessions.sort_values(["vehicle_id", "arrival_utc"])
    arr_day = to_local_naive(s["arrival_utc"], cfg.tz).dt.normalize()
    dep_day = to_local_naive(s["departure_utc"], cfg.tz).dt.normalize()
    rows = []
    for vid, idx in s.groupby("vehicle_id").groups.items():
        a, d = arr_day[idx].to_numpy(), dep_day[idx].to_numpy()
        days = pd.date_range(a.min(), d.max(), freq="D")
        ok = np.ones(len(days), bool)
        nxt_a = a[1:]
        for dep, na in zip(d[:-1], nxt_a):                # gap = [dep day + 1, next arrival day - 1]
            if (na - dep) / np.timedelta64(1, "D") - 1 > cfg.max_away_days:
                ok[(days > dep) & (days < na)] = False
        rows.append(pd.DataFrame({"vehicle_id": vid, "day": days, "observed": ok}))
    return pd.concat(rows, ignore_index=True)


# ===================================================================== features
def _prior_week_view(days: pd.DataFrame, weeks: pd.DataFrame, tz: str) -> pd.DataFrame:
    """For each (vehicle, week_start): the 7 prior days, with values not yet known at
    week_start masked to NaN. This is the ONLY place the cutoff is applied."""
    k = pd.DataFrame({"k": range(1, 8)})
    v = weeks[["vehicle_id", "week_start"]].merge(k, how="cross")
    v["day"] = v["week_start"] - pd.to_timedelta(v["k"], unit="D")
    v = v.merge(days, on=["vehicle_id", "day"], how="left")
    cutoff = local_midnight_utc(v["week_start"], tz)
    v["plugs_in"] = v["n_sessions"].notna().astype(float)
    v["n_sessions"] = v["n_sessions"].fillna(0)
    # still plugged in at week_start: we know it arrived, not when it will leave
    v["still_plugged"] = (v["dep_utc"] > cutoff).astype(float)
    v.loc[~(v["dep_utc"] <= cutoff), "plugout_h"] = np.nan
    v.loc[~(v["trip_known_utc"] <= cutoff), "trip_kwh"] = np.nan
    v.loc[~(v["energy_known_utc"] <= cutoff), "energy_day"] = np.nan
    # energy charged on a day with no plug-in is a known 0, not unknown
    v.loc[v["plugs_in"] == 0, "energy_day"] = 0.0
    v["weekday"] = v["day"].dt.day_of_week
    return v


def build_features(sessions: pd.DataFrame, weeks: pd.DataFrame, cfg: HabitsConfig,
                   min_charge_kwh: float = IngestConfig().min_charge_kwh) -> pd.DataFrame:
    """weeks: (vehicle_id, week_start = naive local Monday). Returns one row per
    (vehicle, week_start, day in that week) with features from the prior week only."""
    days = day_table(sessions, cfg.tz, min_charge_kwh)
    pw = _prior_week_view(days, weeks, cfg.tz)

    # --- last-week aggregates (over the 7 prior days)
    g = pw.groupby(["vehicle_id", "week_start"])
    agg = pd.DataFrame({
        "pw_plugin_days": g["plugs_in"].sum(),
        "pw_sessions": g["n_sessions"].sum(),
        "pw_weekday_plugin_days": pw[pw["weekday"] < 5].groupby(["vehicle_id", "week_start"])["plugs_in"].sum(),
        "pw_plugin_h_mean": g["plugin_h"].mean(), "pw_plugin_h_std": g["plugin_h"].std(),
        "pw_plugin_h_median": g["plugin_h"].median(),
        "pw_plugout_h_mean": g["plugout_h"].mean(), "pw_plugout_h_std": g["plugout_h"].std(),
        "pw_plugout_h_median": g["plugout_h"].median(),
        "pw_trip_kwh_mean": g["trip_kwh"].mean(), "pw_trip_kwh_std": g["trip_kwh"].std(),
        "pw_trip_kwh_median": g["trip_kwh"].median(),
        "pw_trip_kwh_max": g["trip_kwh"].max(),
        "pw_energy_kwh": g["energy_day"].sum(min_count=1),
        "still_plugged_at_start": g["still_plugged"].max(),
    }).reset_index()
    agg["pw_weekday_plugin_days"] = agg["pw_weekday_plugin_days"].fillna(0)

    # --- target rows: 7 days of each week, plus "same weekday last week" (day - 7)
    t = weeks[["vehicle_id", "week_start"]].merge(pd.DataFrame({"j": range(7)}), how="cross")
    t["day"] = t["week_start"] + pd.to_timedelta(t["j"], unit="D")
    t = t.drop(columns="j")
    lw = pw[["vehicle_id", "week_start", "day", "plugs_in", "plugin_h", "plugout_h",
             "trip_kwh", "energy_day", "still_plugged"]].copy()
    lw["day"] = lw["day"] + pd.Timedelta(days=7)
    lw.columns = ["vehicle_id", "week_start", "day"] + ["lw_" + c for c in lw.columns[3:]]
    t = t.merge(lw, on=["vehicle_id", "week_start", "day"], how="left").merge(
        agg, on=["vehicle_id", "week_start"], how="left")

    # --- calendar
    t["dow"] = t["day"].dt.day_of_week
    t["is_weekend"] = (t["dow"] >= 5).astype(int)
    t["bank_holiday"] = t["day"].isin(UK_BANK_HOLIDAYS).astype(int)
    t["lw_bank_holiday"] = (t["day"] - pd.Timedelta(days=7)).isin(UK_BANK_HOLIDAYS).astype(int)
    t["month"] = t["day"].dt.month

    # --- targets (the truth for the target day; never used as features)
    tgt = days[["vehicle_id", "day", "plugin_h", "plugout_h", "trip_kwh"]].rename(
        columns={"plugin_h": "y_plugin_h", "plugout_h": "y_plugout_h", "trip_kwh": "y_trip_kwh"})
    t = t.merge(tgt, on=["vehicle_id", "day"], how="left")
    t["y_plugs_in"] = t["y_plugin_h"].notna().astype(int)
    return t


FEATURES = [  # model inputs, in order (everything else is an ID, a target or the split)
    "dow", "is_weekend", "bank_holiday", "lw_bank_holiday", "month",
    "lw_plugs_in", "lw_plugin_h", "lw_plugout_h", "lw_trip_kwh", "lw_energy_day", "lw_still_plugged",
    "pw_plugin_days", "pw_sessions", "pw_weekday_plugin_days",
    "pw_plugin_h_mean", "pw_plugin_h_std", "pw_plugin_h_median",
    "pw_plugout_h_mean", "pw_plugout_h_std", "pw_plugout_h_median",
    "pw_trip_kwh_mean", "pw_trip_kwh_std", "pw_trip_kwh_median", "pw_trip_kwh_max",
    "pw_energy_kwh", "still_plugged_at_start",
]


# ===================================================================== dataset + splits
def eligible_weeks(sessions: pd.DataFrame, cfg: HabitsConfig) -> pd.DataFrame:
    """(vehicle, Monday) where the prior week AND the target week are fully observed."""
    obs = observed_days(sessions, cfg)
    obs["week_start"] = obs["day"] - pd.to_timedelta(obs["day"].dt.day_of_week, unit="D")
    wk = obs.groupby(["vehicle_id", "week_start"])["observed"].agg(["sum", "size"])
    full = wk[(wk["sum"] == 7) & (wk["size"] == 7)].reset_index()[["vehicle_id", "week_start"]]
    prev = full.assign(week_start=full["week_start"] + pd.Timedelta(days=7))
    return full.merge(prev, on=["vehicle_id", "week_start"])     # week AND the one before


def assign_splits(df: pd.DataFrame, cfg: HabitsConfig) -> pd.Series:
    """test = whole held-out vehicles; val = last weeks of the training vehicles; else train."""
    rng = np.random.default_rng(cfg.seed)
    vids = np.sort(df["vehicle_id"].unique())
    test_v = set(rng.choice(vids, size=int(round(cfg.test_vehicle_frac * len(vids))), replace=False))
    is_test = df["vehicle_id"].isin(test_v)
    # time cut so ~val_week_frac of the training vehicles' rows fall after it
    cut = df.loc[~is_test, "week_start"].quantile(1 - cfg.val_week_frac)
    cut = cut - pd.to_timedelta(cut.day_of_week, unit="D")              # snap to a Monday
    split = np.where(is_test, "test", np.where(df["week_start"] >= cut, "val", "train"))
    return pd.Series(split, index=df.index, name="split")


def build_dataset(sessions: pd.DataFrame, cfg: HabitsConfig = HabitsConfig()) -> tuple[pd.DataFrame, dict]:
    weeks = eligible_weeks(sessions, cfg)
    df = build_features(sessions, weeks, cfg)
    df["split"] = assign_splits(df, cfg)
    stats = {"config": asdict(cfg), "rows": len(df)}
    for name, part in df.groupby("split"):
        stats[name] = {"rows": len(part), "vehicles": int(part["vehicle_id"].nunique()),
                       "weeks": int(part["week_start"].nunique()),
                       "first_day": str(part["day"].min().date()), "last_day": str(part["day"].max().date()),
                       "plug_in_rate": round(float(part["y_plugs_in"].mean()), 3)}
    return df, stats


def main():
    df, stats = build_dataset(pd.read_parquet(SESSIONS))
    df.to_parquet(OUT, index=False)
    with open(OUT.with_name("habits_dataset_report.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))
    print(f"wrote {OUT} ({len(df)} rows)")


if __name__ == "__main__":
    main()
