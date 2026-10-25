"""One real car, one week: predict its habits, schedule charging/selling, check against reality.

    python scripts/test_one_car.py                                  # an example test-set car
    python scripts/test_one_car.py --vehicle UK-GFX-ENA0225 --week 2018-11-05
    python scripts/test_one_car.py --list                           # cars + weeks you can pick

Steps (the product loop, on Electric Nation data):
  1. take the car's sessions from the 7 days before Monday 00:00 (UK time);
  2. predict_next_week() -> next week's plug-in windows and trip energies (habits model);
  3. plan with those predictions, re-planning at 10:00 and whenever the car plugs in,
     while reality follows what the car really did that week (run_daily_vs_reality);
  4. compare with a normal charger and with perfect knowledge of the week.
Writes artefacts/one_car/<vehicle>_<week>.{csv,json}. Prices: data/processed/prices.parquet
if present, else synthetic (labelled).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from shared.schema import load_assumptions, load_spec  # noqa: E402
from shared.units import utc_index  # noqa: E402
from v2g.habits.features import OUT as DATASET  # noqa: E402
from v2g.habits.predict import load_model, plan_inputs_from_predictions, predict_next_week  # noqa: E402
from v2g.ingest.synthetic import synthetic_prices  # noqa: E402
from v2g.optimise.daily import (bill, departure_check, floor_learned, params_from_config,  # noqa: E402
                                run_daily, run_daily_vs_reality, run_unmanaged, trace_from_sessions)

SESSIONS = ROOT / "data" / "processed" / "sessions.parquet"
OUT = ROOT / "artefacts" / "one_car"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vehicle", help="ChargerID, e.g. UK-GFX-ENA0225")
    ap.add_argument("--week", help="Monday (local date) to predict, e.g. 2018-11-05")
    ap.add_argument("--always-ready", action="store_true", help="hold trip energy all session")
    ap.add_argument("--list", action="store_true", help="list test-set cars and weeks, then exit")
    args = ap.parse_args()

    df = pd.read_parquet(DATASET)
    test = df[df["split"] == "test"]
    if args.list:
        weeks = test.groupby("vehicle_id")["week_start"].agg(["min", "max", "nunique"])
        print("Test-set cars (never seen in training). Any Monday in range with --week works:")
        print(weeks.rename(columns={"nunique": "weeks"}).to_string())
        return
    # default: the test-set car with the most weeks, in the middle of its data
    vid = args.vehicle or test.groupby("vehicle_id")["week_start"].nunique().idxmax()
    car_weeks = df.loc[df["vehicle_id"] == vid, "week_start"].drop_duplicates().sort_values()
    if car_weeks.empty and not args.week:
        sys.exit(f"no eligible weeks for {vid}; try --list")
    ws = pd.Timestamp(args.week) if args.week else car_weeks.iloc[len(car_weeks) // 2]
    if ws.day_of_week != 0:
        sys.exit(f"--week must be a Monday, got {ws.date()} ({ws.day_name()})")
    if vid not in set(test["vehicle_id"]):
        print(f"note: {vid} is not a test-set car, so the model may have trained on it")

    a = load_assumptions()
    params, tz, dt = params_from_config(load_spec(), a), a.timezone_local, a.dt_h
    sessions = pd.read_parquet(SESSIONS)
    car = sessions[sessions["vehicle_id"] == vid]
    if car.empty:
        sys.exit(f"unknown vehicle {vid}; try --list")
    ws_utc = ws.tz_localize(tz).tz_convert("UTC")

    # ---- 1-2. last week's sessions -> predicted next week
    last_week = car[(car["arrival_utc"] >= ws_utc - pd.Timedelta(days=8)) & (car["arrival_utc"] < ws_utc)]
    if last_week.empty:
        sys.exit(f"{vid} has no sessions in the week before {ws.date()}; pick another week (--list)")
    pred, days = predict_next_week(last_week, ws_utc, model=load_model(), params=params, return_days=True)
    show = pred.assign(arrival=pred["arrival_utc"].dt.tz_convert(tz).dt.strftime("%a %d %b %H:%M"),
                       departure=pred["departure_utc"].dt.tz_convert(tz).dt.strftime("%a %d %b %H:%M"))
    print(f"\n{vid}: predicted sessions for the week of {ws.date()} (UK time; P90 arrival, P10 departure, P90 trip)")
    print(show[["arrival", "departure", "trip_kwh_next", "soc_required"]].round(2).to_string(index=False))

    # ---- 3. simulate Monday 10:00 -> next Monday 10:00 against what really happened
    start = (ws + pd.Timedelta(hours=10)).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    index = utc_index(start, start + pd.Timedelta(days=9), dt)            # +2 days look-ahead
    T = int(7 * 24 / dt)
    usable = (params.soc_max - params.soc_min) * params.e_cap_kwh
    real = car[(car["departure_utc"] > index[0] - pd.Timedelta(days=2)) & (car["arrival_utc"] < index[-1])]
    real = real.assign(trip_kwh_next=real["trip_kwh_next"].fillna(0).clip(upper=usable))
    truth = trace_from_sessions(real, index, dt)

    prices_path = ROOT / "data" / "processed" / "prices.parquet"
    if prices_path.exists():
        w = pd.read_parquet(prices_path).set_index("ts_utc").reindex(index)["price_wholesale_per_mwh"].to_numpy()
        price_src = "real"
    else:
        w = synthetic_prices(start, 9, dt, tz, seed=0)["price_wholesale_per_mwh"].to_numpy()
        price_src = "SYNTHETIC"
    buy, sell = w + a.tariff.import_adder_per_mwh, w + a.tariff.export_adder_per_mwh
    soc0 = a.battery.soc_initial

    def perfect_floor(i, j):
        return floor_learned(j - i, [(d - i, e) for d, e in truth.departures if i < d <= j], params)

    pi = plan_inputs_from_predictions(days, real, truth.plugged, index, params, tz,
                                      always_ready=args.always_ready)
    runs = {"normal charger": run_unmanaged(params, truth, soc0, T),
            "habits model": run_daily_vs_reality(params, truth, buy, sell, pi, soc0, tz, execute_until=T)}
    try:
        runs["perfect knowledge"] = run_daily(params, truth, buy, sell, perfect_floor, soc0, tz, execute_until=T)
    except RuntimeError:
        print("(perfect-knowledge run infeasible for this week: a trip the car could not charge for)")

    # ---- 4. results
    ref = bill(params, runs["normal charger"], buy, sell)["adj_bill"]
    summary = {}
    for name, r in runs.items():
        b = bill(params, r, buy, sell)
        deps = r.get("departures") or departure_check(params, truth, r["soc"], T)
        summary[name] = {"saving_gbp_vs_normal": round(ref - b["adj_bill"], 2),
                         "bill_gbp": round(b["adj_bill"], 2), "export_kwh": round(b["export_kwh"], 1),
                         "departures": len(deps),
                         "shortfalls_over_1kwh": sum(d["short_kwh"] > 1 for d in deps),
                         "worst_shortfall_kwh": round(max([d["short_kwh"] for d in deps], default=0), 1)}
    print(f"\nWeek result ({price_src} prices, {params.e_cap_kwh:.0f} kWh battery, "
          f"{'always-ready' if args.always_ready else 'standard'} mode):")
    print(pd.DataFrame(summary).T.to_string())

    r = runs["habits model"]
    sched = pd.DataFrame({"time_uk": index[:T].tz_convert(tz).strftime("%a %d %b %H:%M"),
                          "car_plugged_in": truth.plugged[:T], "buy_gbp_per_mwh": buy[:T].round(1),
                          "charge_kw": r["p_ch"].round(2), "sell_kw": r["p_dis"].round(2),
                          "soc_start": r["soc"][:T].round(3)})
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{vid}_{ws.date()}"
    sched.to_csv(OUT / f"{stem}.csv", index=False)
    (OUT / f"{stem}.json").write_text(json.dumps({"vehicle_id": vid, "week": str(ws.date()), "prices": price_src,
                                                  "summary": summary}, indent=2))
    print(f"\nHalf-hourly schedule: {OUT / (stem + '.csv')}")


if __name__ == "__main__":
    main()
