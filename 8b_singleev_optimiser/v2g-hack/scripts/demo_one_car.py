"""One car, one week: compare a normal charger with our optimiser under both app options.

    python scripts/demo_one_car.py            # writes artefacts/demo/one_car_week.png

Data: SYNTHETIC prices and trips (labelled), until real data and Tony's forecasts land.
Prices: the optimiser sees actual prices ("perfect forecast") for now; swap in the
forecast parquet later. Option B uses the true trips as its "prediction" for now.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from shared.schema import load_assumptions, load_spec  # noqa: E402
from shared.units import utc_index  # noqa: E402
from v2g.ingest.synthetic import synthetic_prices, synthetic_sessions  # noqa: E402
from v2g.optimise.daily import (bill, floor_learned, floor_set_minimum, params_from_config,  # noqa: E402
                                run_daily, run_unmanaged, trace_from_sessions)

START = "2025-11-03 10:00"     # a Monday, 10:00 UK (= UTC in November)
DATA_DAYS, WEEK_DAYS = 9, 7    # extra days so the last plan can still look 38 h ahead
USER_MIN = 0.60                # option A: driver's chosen minimum
OUT = ROOT / "artefacts" / "demo"


def main():
    spec, a = load_spec(), load_assumptions()
    params = params_from_config(spec, a)
    tz = a.timezone_local

    prices = synthetic_prices(START, DATA_DAYS, a.dt_h, tz, seed=3)
    index = pd.DatetimeIndex(prices["ts_utc"])
    wholesale = prices["price_wholesale_per_mwh"].to_numpy()
    buy = wholesale + a.tariff.import_adder_per_mwh          # £/MWh, what the owner pays
    sell = wholesale + a.tariff.export_adder_per_mwh         # £/MWh, what the owner earns

    sessions = synthetic_sessions(START, DATA_DAYS, 1, tz, a.battery.soc_departure_default, seed=3)
    trace = trace_from_sessions(sessions, index, a.dt_h)
    T = WEEK_DAYS * int(24 / a.dt_h)
    soc0 = a.battery.soc_initial

    def opt_a(i, j):
        return floor_set_minimum(trace.plugged[i:j], USER_MIN, params)

    def opt_b(i, j):
        return floor_learned(j - i, [(d - i, e) for d, e in trace.departures if i < d <= j], params)

    runs = {
        "unmanaged": run_unmanaged(params, trace, soc0, T),
        "A_set_minimum": run_daily(params, trace, buy, sell, opt_a, soc0, tz, execute_until=T),
        "B_learned_habits": run_daily(params, trace, buy, sell, opt_b, soc0, tz, execute_until=T),
    }
    bills = {k: bill(params, r, buy, sell) for k, r in runs.items()}
    base = bills["unmanaged"]["adj_bill"]
    summary = {}
    for k, b in bills.items():
        r = runs[k]
        deps = [d for d, _ in trace.departures if d < T]
        summary[k] = {**{x: round(v, 2) for x, v in b.items()},
                      "saving_vs_unmanaged_gbp_week": round(base - b["adj_bill"], 2),
                      "soc_at_departures": [round(float(r["soc"][d]), 3) for d in deps],
                      "solvers": sorted({d["solver"] for d in r["days"]}),
                      "max_shortfall": max([d["max_shortfall"] for d in r["days"]], default=0.0)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "one_car_week.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: {x: v[x] for x in ("bill", "adj_bill", "saving_vs_unmanaged_gbp_week",
                                             "export_kwh", "solvers", "max_shortfall")}
                      for k, v in summary.items()}, indent=2))
    plot(index[:T], buy[:T], sell[:T], trace, runs, params, T, tz)


def plot(index, buy, sell, trace, runs, params, T, tz):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    t = index.tz_convert(tz).tz_localize(None)
    fig, axes = plt.subplots(4, 1, figsize=(13, 10), sharex=True,
                             gridspec_kw={"height_ratios": [1.1, 1, 1, 1]})
    ax = axes[0]
    ax.step(t, buy, where="post", color="#3b5b92", lw=1.3, label="Buy price")
    ax.step(t, sell, where="post", color="#9aa9c7", lw=1.0, label="Sell price")
    ax.set_ylabel("£/MWh")
    ax.set_title("Prices (synthetic)", loc="left", fontsize=11)
    ax.legend(loc="upper right", frameon=False, ncol=2)
    titles = {"unmanaged": "Normal charger (baseline)",
              "A_set_minimum": "Option A: driver sets a 60 % minimum",
              "B_learned_habits": "Option B: learned habits (perfect prediction for now)"}
    for ax, (k, r) in zip(axes[1:], runs.items()):
        away = ~trace.plugged[:T]
        ax.fill_between(t, 0, 1, where=away, step="post", color="#eeeeee",
                        transform=ax.get_xaxis_transform(), label="Car away")
        p = r["p_ch"] - r["p_dis"]                               # + charging, − selling
        ax.bar(t, np.where(p > 0, p, 0), width=1 / 48, align="edge", color="#4c9a6a", label="Charge kW")
        ax.bar(t, np.where(p < 0, p, 0), width=1 / 48, align="edge", color="#c8553d", label="Sell kW")
        ax.set_ylabel("kW")
        ax.set_ylim(-params.p_dis_max_kw * 1.15, params.p_ch_max_kw * 1.15)
        ax2 = ax.twinx()
        ax2.plot(t.append(pd.DatetimeIndex([t[-1] + pd.Timedelta(minutes=30)])),
                 r["soc"] * 100, color="#222222", lw=1.4, label="SoC %")
        ax2.set_ylim(0, 100)
        ax2.set_ylabel("SoC %")
        ax.set_title(titles[k], loc="left", fontsize=11)
        if k == "unmanaged":
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, loc="lower left", frameon=False, ncol=4, fontsize=8)
    axes[-1].xaxis.set_major_locator(mdates.DayLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b"))
    for ax in axes:
        ax.spines[["top"]].set_visible(False)
        ax.grid(axis="y", color="#e6e6e6", lw=0.6)
    fig.tight_layout()
    fig.savefig(OUT / "one_car_week.png", dpi=130)


if __name__ == "__main__":
    main()
