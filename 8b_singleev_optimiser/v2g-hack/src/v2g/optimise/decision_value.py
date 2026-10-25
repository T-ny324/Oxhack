"""Aggregate the rolling backtest: £ per model, decision value against perfect foresight.

Per model (12 months of executed periods, settled on actual prices):
  net_revenue_gbp      = sum of (export income - import cost - standby) over periods with an actual
                         price + end-energy credit (leftover energy valued at the mean import price,
                         the same adjustment as daily.bill)
  saving_vs_unmanaged  = net_revenue - net_revenue(unmanaged charger)
  decision_value_gbp   = net_revenue(perfect foresight) - net_revenue(model)   (£ lost to forecast error)
  pct_of_perfect       = saving / saving(perfect foresight) * 100   (shares of a negative bill are not
                         meaningful, so the share is taken on the saving over the unmanaged charger)
  energy charged / discharged (kWh) and equivalent full cycles = (charged + discharged) / (2 E_cap)
  excluded_periods     = executed periods whose actual price was missing (not counted, not filled)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .rolling_backtest import ModelRun, World

SCHEDULE_COLUMNS = ["model", "issue_time_utc", "ts_utc", "plugged", "p_charge_kw", "p_discharge_kw", "p_signed_kw", "soc", "drive_kwh",
                    "p_buy_actual", "p_sell_actual", "p_buy_forecast", "p_sell_forecast", "revenue_gbp", "standby_gbp"]


def model_totals(run: ModelRun, world: World) -> dict:
    s, p = run.schedule, world.params
    dt = p.dt_h
    known = s["revenue_gbp"].notna()
    mean_buy = float(np.nanmean(s["p_buy_actual"]))
    credit = (run.soc_end - world.soc0) * p.e_cap_kwh * mean_buy / p.eta_ch / 1000.0
    charged = float(s["p_charge_kw"].sum() * dt)
    discharged = float(s["p_discharge_kw"].sum() * dt)
    return {
        "net_revenue_gbp": float(s.loc[known, "revenue_gbp"].sum() + credit),
        "import_cost_gbp": float((s.loc[known, "p_buy_actual"] * s.loc[known, "p_charge_kw"]).sum() * dt / 1000.0),
        "export_income_gbp": float((s.loc[known, "p_sell_actual"] * s.loc[known, "p_discharge_kw"]).sum() * dt / 1000.0),
        "standby_cost_gbp": float(s.loc[known, "standby_gbp"].sum()),
        "end_energy_credit_gbp": float(credit),
        "energy_charged_kwh": charged,
        "energy_discharged_kwh": discharged,
        "equivalent_cycles": (charged + discharged) / (2.0 * p.e_cap_kwh),
        "excluded_periods": int((~known).sum()),
        "executed_periods": len(s),
        "issues": int(run.issues["issue_time_utc"].nunique()) if len(run.issues) else 0,
        "solve_seconds_total": float(run.issues["solve_seconds"].sum()) if len(run.issues) else 0.0,
        "solve_seconds_mean": float(run.issues["solve_seconds"].mean()) if len(run.issues) else 0.0,
        "milp_issues": int((run.issues["solver"].str.contains("MILP")).sum()) if len(run.issues) else 0,
        "max_shortfall_soc": float(run.issues["max_shortfall"].max()) if len(run.issues) else 0.0,
    }


def summarise(runs: dict[str, ModelRun], world: World, perfect_name: str, unmanaged_name: str = "unmanaged") -> pd.DataFrame:
    rows = {name: model_totals(run, world) for name, run in runs.items()}
    perfect, base = rows[perfect_name]["net_revenue_gbp"], rows[unmanaged_name]["net_revenue_gbp"]
    perfect_saving = perfect - base
    for r in rows.values():
        r["saving_vs_unmanaged_gbp"] = r["net_revenue_gbp"] - base
        r["decision_value_gbp"] = perfect - r["net_revenue_gbp"]
        r["pct_of_perfect_saving"] = 100.0 * r["saving_vs_unmanaged_gbp"] / perfect_saving if perfect_saving > 0 else float("nan")
    table = pd.DataFrame(rows).T
    table.index.name = "model"
    return table.sort_values("net_revenue_gbp", ascending=False)


def write_outputs(runs: dict[str, ModelRun], table: pd.DataFrame, out_dir: Path | str, extra: dict | None = None) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, run in runs.items():
        run.schedule[SCHEDULE_COLUMNS].to_parquet(out_dir / f"{name}.parquet", index=False)
    payload = {"per_model": json.loads(table.to_json(orient="index")), **(extra or {})}
    path = out_dir / "revenue_per_vehicle.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


def format_table(table: pd.DataFrame) -> str:
    cols = ["net_revenue_gbp", "saving_vs_unmanaged_gbp", "pct_of_perfect_saving", "decision_value_gbp",
            "energy_charged_kwh", "energy_discharged_kwh", "equivalent_cycles", "excluded_periods", "solve_seconds_mean"]
    show = table[cols].copy()
    for c in ("net_revenue_gbp", "saving_vs_unmanaged_gbp", "decision_value_gbp"):
        show[c] = show[c].map(lambda v: f"{v:,.2f}")
    show["pct_of_perfect_saving"] = show["pct_of_perfect_saving"].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "")
    for c in ("energy_charged_kwh", "energy_discharged_kwh"):
        show[c] = show[c].map(lambda v: f"{v:,.0f}")
    show["equivalent_cycles"] = show["equivalent_cycles"].map(lambda v: f"{v:.1f}")
    show["solve_seconds_mean"] = show["solve_seconds_mean"].map(lambda v: f"{1000 * v:.0f} ms")
    show["excluded_periods"] = show["excluded_periods"].astype(int)
    return show.rename(columns={"net_revenue_gbp": "net £/yr", "saving_vs_unmanaged_gbp": "saving vs unmanaged £", "pct_of_perfect_saving": "% of perfect saving",
                                "decision_value_gbp": "decision value £", "energy_charged_kwh": "charged kWh", "energy_discharged_kwh": "discharged kWh",
                                "equivalent_cycles": "cycles", "excluded_periods": "excluded", "solve_seconds_mean": "solve/issue"}).to_string()
