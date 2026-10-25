"""Habits model: one definition of every metric, shared by baseline (step 4), models
(step 5) and the final test report (step 6).

Every predictor returns a DataFrame aligned with the dataset rows, with columns
    p_plugs_in      probability (or 0/1) that a session starts that day
    pred_plugin_h   plug-in time,  hours after local midnight
    pred_plugout_h  departure,     hours after local midnight (> 24 = a later day)
    pred_trip_kwh   energy for the trip(s) after the departure

Metrics on "departure days" = days the car really plugged in (the time/energy targets
only exist then). Classification is scored separately on all days.
  shortfall          the car would leave with LESS energy than needed: actual trip energy
                     > predicted + 1 kWh, OR actual departure > 15 min EARLIER than
                     predicted (the plan had not finished charging). Tolerances are config
                     values (cfg.shortfall_tol_kwh / _h); smaller misses are noise. Target ~0.
  late_plugin        actual plug-in later than predicted (plan counted on a car not yet there)
  pinball            quantile loss at the configured quantile (lower = better)
  coverage           share of actuals on the SAFE side of the prediction (target = quantile
                     for energy/plug-in, 1 - quantile for departure; 0.9 with w = 9)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .features import HabitsConfig

ROOT = Path(__file__).resolve().parents[3]
ART = ROOT / "artefacts" / "habits"

PRED = {"y_plugin_h": "pred_plugin_h", "y_plugout_h": "pred_plugout_h", "y_trip_kwh": "pred_trip_kwh"}


def pinball(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    """Mean quantile loss: under-predictions cost tau, over-predictions cost 1 - tau."""
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


def score(df: pd.DataFrame, pred: pd.DataFrame, cfg: HabitsConfig = HabitsConfig()) -> dict:
    """All metrics for one predictor on one split (df and pred share the index)."""
    out = {}
    # ---- plugs in today? (all days)
    y, p = df["y_plugs_in"].to_numpy(), pred["p_plugs_in"].to_numpy()
    yhat = p >= 0.5
    out["plug_accuracy"] = float(np.mean(yhat == y))
    out["plug_precision"] = float(y[yhat].mean()) if yhat.any() else np.nan
    out["plug_recall"] = float(yhat[y == 1].mean())
    # predicted a plug-in that never came: the plan counts on a car that is not there
    out["plug_false_alarm_rate"] = float(yhat[y == 0].mean())
    out["plug_brier"] = float(np.mean((p - y) ** 2))
    out["plug_auc"] = float(roc_auc_score(y, p)) if len(np.unique(p)) > 1 else 0.5

    # ---- departure days with a known trip energy
    m = (df["y_plugs_in"] == 1) & df["y_trip_kwh"].notna()
    d, pr = df[m], pred[m]
    e_short = d["y_trip_kwh"].to_numpy() - pr["pred_trip_kwh"].to_numpy()       # > 0 = too little
    t_early = pr["pred_plugout_h"].to_numpy() - d["y_plugout_h"].to_numpy()     # > 0 = left earlier
    e_bad, t_bad = e_short > cfg.shortfall_tol_kwh, t_early > cfg.shortfall_tol_h
    out["n_departures"] = int(m.sum())
    out["shortfall_rate"] = float((e_bad | t_bad).mean())
    out["energy_short_rate"] = float(e_bad.mean())
    out["early_departure_rate"] = float(t_bad.mean())
    out["mean_shortfall_kwh"] = float(e_short[e_bad].mean()) if e_bad.any() else 0.0
    out["mean_early_departure_h"] = float(t_early[t_bad].mean()) if t_bad.any() else 0.0
    out["late_plugin_rate"] = float((d["y_plugin_h"].to_numpy() > pr["pred_plugin_h"].to_numpy()).mean())

    # ---- pinball loss and coverage per target, at the configured quantiles
    for tgt, tau in cfg.quantiles.items():
        yy, qq = d[tgt].to_numpy(), pr[PRED[tgt]].to_numpy()
        short_name = tgt[2:]
        out[f"pinball_{short_name}"] = pinball(yy, qq, tau)
        safe = (yy <= qq) if tau > 0.5 else (yy >= qq)
        out[f"coverage_{short_name}"] = float(safe.mean())
    # how much "caution" a predictor buys: mean predicted trip energy vs mean actual
    out["mean_pred_trip_kwh"] = float(pr["pred_trip_kwh"].mean())
    out["mean_actual_trip_kwh"] = float(d["y_trip_kwh"].mean())
    return out


def table(results: dict[str, dict], cols: list[str] | None = None) -> pd.DataFrame:
    """{predictor name: score()} -> a readable comparison table (one row per predictor)."""
    t = pd.DataFrame(results).T
    return t[cols] if cols else t


HEADLINE = ["shortfall_rate", "energy_short_rate", "early_departure_rate", "mean_shortfall_kwh",
            "mean_early_departure_h", "late_plugin_rate", "pinball_trip_kwh", "coverage_trip_kwh",
            "pinball_plugout_h", "coverage_plugout_h", "pinball_plugin_h", "coverage_plugin_h",
            "plug_auc", "plug_brier", "plug_accuracy", "plug_recall", "plug_false_alarm_rate",
            "mean_pred_trip_kwh"]


# ============================================================ step 6: cost of caution
def cost_of_caution(df: pd.DataFrame, preds: dict[str, pd.DataFrame], sessions: pd.DataFrame,
                    n_vehicles: int = 63, cfg: HabitsConfig = HabitsConfig()) -> pd.DataFrame:
    """Run a sample of test vehicle-weeks through the optimiser and settle the bills.

    For each sampled (vehicle, week), Monday 10:00 -> next Monday 10:00 local:
      unmanaged    normal charger: plug in, charge flat out (reference bill)
      perfect      run_daily with the TRUE week ("perfect prediction" of habits)
      <predictor>  run_daily_vs_reality: plans with that predictor's habits (re-planning
                   at 10:00 and on every real arrival), reality follows the true week;
                   the trip energy is required at the predicted departure
      <predictor>_always_ready   same, but the trip energy is required throughout every
                   session (safe against early departures, sells only above it)
    Prices: data/processed/prices.parquet if present, else synthetic (labelled). The
    optimiser sees actual prices in every run, so only the habits differ.
    Returns one row per (vehicle-week, strategy): saving vs unmanaged (£), departures,
    shortfalls (> cfg.shortfall_tol_kwh) and kWh exported.
    """
    from shared.schema import load_assumptions, load_spec
    from shared.units import utc_index
    from v2g.ingest.synthetic import synthetic_prices
    from v2g.optimise.daily import (bill, departure_check, floor_learned, params_from_config,
                                    run_daily, run_daily_vs_reality, run_unmanaged,
                                    trace_from_sessions)
    from .predict import day_predictions, plan_inputs_from_predictions

    a = load_assumptions()
    params, tz, dt = params_from_config(load_spec(), a), a.timezone_local, a.dt_h
    prices_path = ROOT / "data" / "processed" / "prices.parquet"
    real_prices = pd.read_parquet(prices_path) if prices_path.exists() else None
    T = int(7 * 24 / dt)

    # one random eligible week per test vehicle whose true trips are all known
    test = df[df["split"] == "test"]
    rng = np.random.default_rng(cfg.seed)
    picks = []
    for vid in rng.permutation(np.sort(test["vehicle_id"].unique())):
        for ws in rng.permutation(np.sort(test.loc[test["vehicle_id"] == vid, "week_start"].unique())):
            start = (pd.Timestamp(ws) + pd.Timedelta(hours=10)).tz_localize(tz).tz_convert("UTC")
            vs = sessions[(sessions["vehicle_id"] == vid)
                          & (sessions["departure_utc"] > start - pd.Timedelta(days=2))
                          & (sessions["arrival_utc"] < start + pd.Timedelta(days=9))]
            if len(vs) and vs["trip_kwh_next"].notna().all():
                picks.append((vid, pd.Timestamp(ws), start, vs))
                break
        if len(picks) == n_vehicles:
            break

    # A "trip" bigger than the usable battery means the car charged somewhere else (or the
    # proxy accumulated several trips over zero-energy plug-ins): no strategy could serve it
    # from home, so reality is capped at the usable battery energy.
    usable_kwh = (params.soc_max - params.soc_min) * params.e_cap_kwh
    out, skipped, capped = [], [], 0
    for k, (vid, ws, start, vs) in enumerate(picks):
        index = utc_index(start.tz_localize(None), start.tz_localize(None) + pd.Timedelta(days=9), dt)
        capped += int((vs["trip_kwh_next"] > usable_kwh).sum())
        vs = vs.assign(trip_kwh_next=vs["trip_kwh_next"].clip(upper=usable_kwh))
        truth = trace_from_sessions(vs, index, dt)
        if real_prices is not None:
            w = real_prices.set_index("ts_utc").reindex(index)["price_wholesale_per_mwh"].to_numpy()
            price_src = "real"
        else:
            w = synthetic_prices(start.tz_localize(None), 9, dt, tz, seed=k)["price_wholesale_per_mwh"].to_numpy()
            price_src = "synthetic"
        buy, sell = w + a.tariff.import_adder_per_mwh, w + a.tariff.export_adder_per_mwh
        soc0 = a.battery.soc_initial

        def perfect_floor(i, j):
            return floor_learned(j - i, [(d - i, e) for d, e in truth.departures if i < d <= j], params)

        runs = {"unmanaged": run_unmanaged(params, truth, soc0, T)}
        try:
            runs["perfect"] = run_daily(params, truth, buy, sell, perfect_floor, soc0, tz, execute_until=T)
        except RuntimeError:
            # even perfect knowledge can't plan this week (a trip the car can't charge for in
            # time drives the plan below 0 %): skipped for EVERY strategy, and reported
            skipped.append((vid, str(ws.date())))
            continue
        for name, pred in preds.items():
            m = (df["vehicle_id"] == vid) & (df["week_start"] == ws)
            days = day_predictions(df[m], pred.loc[df.index[m]], tz)
            for ready in (False, True):
                pi = plan_inputs_from_predictions(days, vs, truth.plugged, index, params, tz,
                                                  always_ready=ready)
                runs[name + ("_always_ready" if ready else "")] = run_daily_vs_reality(
                    params, truth, buy, sell, pi, soc0, tz, execute_until=T)
        ref = bill(params, runs["unmanaged"], buy, sell)["adj_bill"]
        for name, r in runs.items():
            deps = r.get("departures") or departure_check(params, truth, r["soc"], T)
            short = [x["short_kwh"] for x in deps if x["short_kwh"] > cfg.shortfall_tol_kwh]
            b = bill(params, r, buy, sell)
            out.append({"vehicle_id": vid, "week_start": ws, "strategy": name, "prices": price_src,
                        "saving_gbp": ref - b["adj_bill"], "export_kwh": b["export_kwh"],
                        "departures": len(deps), "shortfalls": len(short),
                        "short_kwh_total": float(sum(short)),
                        "min_soc": float(r["soc"].min()), "max_soc": float(r["soc"].max())})
    res = pd.DataFrame(out)
    res.attrs.update({"skipped_infeasible": skipped, "trips_capped_at_usable": capped,
                      "usable_kwh": usable_kwh})
    return res


def summarise_caution(runs: pd.DataFrame) -> pd.DataFrame:
    g = runs.groupby("strategy")
    t = pd.DataFrame({
        "saving_gbp_per_week": g["saving_gbp"].mean(),
        "export_kwh_per_week": g["export_kwh"].mean(),
        "departures": g["departures"].sum(),
        "shortfall_rate": g["shortfalls"].sum() / g["departures"].sum(),
        "mean_shortfall_kwh": g["short_kwh_total"].sum() / g["shortfalls"].sum().replace(0, np.nan),
    })
    perfect = t.loc["perfect", "saving_gbp_per_week"]
    t["cost_of_caution_gbp_per_week"] = perfect - t["saving_gbp_per_week"]
    t["share_of_perfect_savings"] = t["saving_gbp_per_week"] / perfect
    return t


def main():
    """Step 6: final report on the held-out TEST vehicles (models fitted on train + val)."""
    import json
    import time

    import joblib

    from .baseline import predict_baseline
    from .features import OUT as DATASET
    from .model import REG_TARGETS, fit_model

    cfg = HabitsConfig()
    df = pd.read_parquet(DATASET)
    fit, test = df[df["split"] != "test"], df[df["split"] == "test"]
    t0 = time.time()
    lgbm = joblib.load(ART / "habits_model.joblib")["model"]           # chosen in step 5
    linear, _ = fit_model(fit, cfg, {"classifier": "linear", **{t: "linear" for t in REG_TARGETS}})
    preds = {"baseline": predict_baseline(test, fit), "linear": linear.predict(test),
             "lgbm": lgbm.predict(test)}
    res = {k: score(test, p, cfg) for k, p in preds.items()}
    print(f"fitted + scored on test in {time.time() - t0:.0f}s")

    t0 = time.time()
    sessions = pd.read_parquet(ROOT / "data" / "processed" / "sessions.parquet")
    runs = cost_of_caution(df, {"baseline": preds["baseline"], "lgbm": preds["lgbm"]}, sessions, cfg=cfg)
    caution = summarise_caution(runs)
    print(f"cost-of-caution simulation ({runs['vehicle_id'].nunique()} vehicle-weeks, "
          f"{runs['prices'].iloc[0]} prices) in {time.time() - t0:.0f}s; {runs.attrs}")

    ART.mkdir(parents=True, exist_ok=True)
    runs.to_csv(ART / "cost_of_caution_runs.csv", index=False)
    with open(ART / "test_report.json", "w") as f:
        json.dump({"test_metrics": res, "cost_of_caution": caution.round(4).to_dict(orient="index"),
                   "simulation_notes": runs.attrs,
                   "config": {"asym_weight_w": cfg.asym_weight_w, "shortfall_tol_kwh": cfg.shortfall_tol_kwh,
                              "shortfall_tol_h": cfg.shortfall_tol_h}}, f, indent=2, default=str)
    with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
        print(table(res, HEADLINE).T)
        print(caution.T)


if __name__ == "__main__":
    main()
