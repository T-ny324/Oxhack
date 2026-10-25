"""Rolling decision-value backtest of the optimiser on the S3 price forecasts.

    python 8b_singleev_optimiser/v2g-hack/scripts/forecast_backtest.py --window smoke|backtest [--start --end] [--models a,b]
    make optimise-backtest            (from the repository root)

Runs lear_x, lear, naive_daily, naive_weekly and perfect_foresight through the identical
plan-38h / act-24h loop (src/v2g/optimise/rolling_backtest.py) plus the unmanaged-charger
baseline, settles everything on actual prices, and writes
    artefacts/schedules/<model>.parquet, artefacts/schedules/revenue_per_vehicle.json,
    artefacts/figures/optimiser_<model>_week.png
Must run from the repository root environment (it imports the forecast package); see
src/v2g/optimise/bridge.py for why the optimiser is loaded under an alias.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

SUB_ROOT = Path(__file__).resolve().parents[1]              # .../8b_singleev_optimiser/v2g-hack
REPO_ROOT = SUB_ROOT.parents[1]                             # repository root
for p in (REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd

from v2g.forecast.config import load_forecast_config
from v2g.forecast.io import load_forecasts, load_prices
from v2g.forecast.timing import issue_times


def load_bridge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ev_optimiser_bridge", SUB_ROOT / "src" / "v2g" / "optimise" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", choices=["smoke", "backtest"], default="smoke")
    ap.add_argument("--start", help="first local issue date (overrides the window)")
    ap.add_argument("--end", help="last local issue date, inclusive")
    ap.add_argument("--models", help="comma-separated forecast models (default from config)")
    ap.add_argument("--out-dir", help="override forecast_backtest.out_dir")
    ap.add_argument("--plot-week", help="local Monday to plot for each model (default: first full week)")
    ap.add_argument("--plot-models", default="lear_x", help="comma-separated models to plot")
    args = ap.parse_args(argv)

    opt = load_bridge()
    a = opt.schema.load_assumptions(SUB_ROOT / "config" / "assumptions.yaml")
    bt = a.forecast_backtest
    if bt is None:
        sys.exit("config/assumptions.yaml has no forecast_backtest section")
    spec = opt.schema.load_spec(REPO_ROOT / bt.converter_spec_path)
    fcfg = load_forecast_config(REPO_ROOT / "config" / "assumptions.yaml")
    if fcfg.timezone != a.timezone_local:
        sys.exit(f"timezone mismatch: forecast {fcfg.timezone} vs optimiser {a.timezone_local}")
    params = opt.daily.params_from_config(spec, a)

    win = bt.smoke_window if args.window == "smoke" else bt.window
    start = date.fromisoformat(args.start or win.start)
    end = date.fromisoformat(args.end or win.end)
    models = [m.strip() for m in (args.models.split(",") if args.models else bt.models)]
    forecast_path = REPO_ROOT / bt.forecast_path
    if not forecast_path.exists():
        forecast_path = REPO_ROOT / bt.forecast_fallback_path
    forecasts = load_forecasts(forecast_path, model=models)
    prices = load_prices(REPO_ROOT / bt.prices_path)
    issues = issue_times(start, end, fcfg)
    sessions = opt.availability.sessions_from_profile(bt.profile, start - pd.Timedelta(days=1), end + pd.Timedelta(days=2), a.timezone_local)
    world = opt.rolling_backtest.build_world(issues, sessions, prices, params, a.tariff, fcfg, spec.standby_w, a.battery.soc_initial, bt.floor_reserve, bt.series)
    print(f"{len(issues)} issues {start}..{end}; forecasts from {forecast_path.relative_to(REPO_ROOT)}; "
          f"profile {bt.profile.kind}; battery {params.e_cap_kwh:.0f} kWh; adders {a.tariff.import_adder_per_mwh:+}/{a.tariff.export_adder_per_mwh:+} £/MWh")

    runs, t_all = {}, time.perf_counter()
    for model in models:
        t0 = time.perf_counter()
        runs[model] = opt.rolling_backtest.run_model(model, forecasts, world)
        print(f"  {model:18s} {time.perf_counter() - t0:6.1f}s  mean solve {1000 * runs[model].issues['solve_seconds'].mean():.0f} ms/issue")
    t0 = time.perf_counter()
    runs[bt.perfect_foresight_name] = opt.rolling_backtest.run_model(bt.perfect_foresight_name, None, world, perfect=True)
    print(f"  {bt.perfect_foresight_name:18s} {time.perf_counter() - t0:6.1f}s")
    runs["unmanaged"] = opt.rolling_backtest.run_unmanaged_baseline(world)

    table = opt.decision_value.summarise(runs, world, bt.perfect_foresight_name)
    physics = {name: opt.rolling_backtest.check_physics(run, world) for name, run in runs.items()}
    out_dir = REPO_ROOT / (args.out_dir or bt.out_dir)
    extra = {"window": {"start": str(start), "end": str(end)}, "issues": len(issues), "forecast_file": str(forecast_path.relative_to(REPO_ROOT)),
             "profile": bt.profile.model_dump(), "tariff_adders_per_mwh": a.tariff.model_dump(), "battery_kwh": params.e_cap_kwh,
             "standby_w": spec.standby_w, "physics_checks": physics, "runtime_seconds": round(time.perf_counter() - t_all, 1)}
    path = opt.decision_value.write_outputs(runs, table, out_dir, extra)
    print("\n" + opt.decision_value.format_table(table))
    print("\nphysics: " + json.dumps({k: {"balance": f"{v['energy_balance_max_abs_soc_error']:.1e}", "bounds": v["within_bounds"], "simultaneous": v["simultaneous_periods"]} for k, v in physics.items()}))
    print(f"written: {path} (+ one parquet per model); total runtime {time.perf_counter() - t_all:.1f}s")

    week = args.plot_week or str(issues[0].tz_convert(a.timezone_local).date() + pd.Timedelta(days=(7 - issues[0].tz_convert(a.timezone_local).weekday()) % 7))
    for model in [m.strip() for m in args.plot_models.split(",") if m.strip() in runs]:
        fig_path = REPO_ROOT / bt.figures_dir / f"optimiser_{model}_week.png"
        opt.schedule_plot.plot_week(runs[model].schedule, params, a.timezone_local, week, fig_path,
                                    f"{model}: executed schedule, week of {week} (plan on forecast, settle on actual)")
        print(f"figure: {fig_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
