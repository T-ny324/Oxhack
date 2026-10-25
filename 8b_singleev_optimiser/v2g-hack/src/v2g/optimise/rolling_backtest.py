"""Rolling daily backtest of the optimiser on per-issue price forecasts (plan ~38 h, act ~24 h).

For every issue in the window (10:00 Europe/London):
  1. take that issue's forecast periods for the model (price_inputs.forecast_prices);
  2. map to p_buy / p_sell;
  3. solve ``lp.solve_window`` from the car's current SoC with the profile's availability and the
     departure floor (``daily.floor_learned`` with the true trips: perfect habits for every model,
     so only the price forecast differs between runs);
  4. keep the decisions up to the next issue only (execution window, 46/48/50 periods);
  5. settle those periods on ACTUAL prices (missing actuals are excluded and counted);
  6. carry the SoC at the end of the execution window into the next solve.
``perfect_foresight`` runs the identical loop with the actuals as its forecast. The unmanaged
charger (``daily.run_unmanaged``) is settled the same way as the floor of the comparison.
Standby loss (``converter_spec.standby_w`` while plugged in) is charged to every strategy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .daily import floor_learned, integrate_soc, run_unmanaged, trace_from_sessions
from .lp import solve_window
from .price_inputs import (
    actual_prices,
    execution_length,
    forecast_prices,
    issue_targets,
    perfect_foresight_prices,
)


@dataclass
class World:
    """Everything shared by the models: time index, availability trace, actual prices, limits."""
    issues: pd.DatetimeIndex
    index: pd.DatetimeIndex          # UTC half-hours from the first issue to the last plan end
    trace: object                    # daily.Trace
    prices: pd.Series                # actual wholesale, UTC index, NaN where missing
    params: object                   # lp.VehicleParams
    tariff: object
    fcfg: object                     # root ForecastConfig (issue time, timezone)
    standby_kw: float
    soc0: float
    reserve: float
    series: str
    executed_until: int = 0          # index position where the last execution window ends


@dataclass
class ModelRun:
    model: str
    schedule: pd.DataFrame           # one row per executed period
    issues: pd.DataFrame             # one row per issue: solver, seconds, shortfall, plan/act sizes
    soc_end: float
    notes: list[str] = field(default_factory=list)


def build_world(issues, trace_sessions, prices, params, tariff, fcfg, standby_w, soc0, reserve, series) -> World:
    first = issues[0]
    last_plan_end = issue_targets(issues[-1], fcfg)[-1] + pd.Timedelta(hours=params.dt_h)
    index = pd.date_range(first, last_plan_end, freq=pd.Timedelta(hours=params.dt_h), inclusive="left", unit="ns")
    trace = trace_from_sessions(trace_sessions, index, params.dt_h)
    last_targets = issue_targets(issues[-1], fcfg)
    executed_until = int(index.get_loc(first)) + sum(
        execution_length(iss, issue_targets(iss, fcfg), fcfg) for iss in issues[:-1]
    ) + execution_length(issues[-1], last_targets, fcfg)
    return World(issues, index, trace, prices, params, tariff, fcfg, standby_w / 1000.0, soc0, reserve, series, executed_until)


def _settle(world: World, ts: pd.DatetimeIndex, p_ch: np.ndarray, p_dis: np.ndarray, plugged: np.ndarray) -> dict:
    """Per-period £ on actual prices (NaN where the actual is missing) and the standby charge."""
    buy_act, sell_act = actual_prices(world.prices, ts, world.tariff)
    dt = world.params.dt_h
    energy_gbp = (sell_act * p_dis - buy_act * p_ch) * dt / 1000.0
    standby_gbp = np.where(plugged, world.standby_kw * dt * buy_act / 1000.0, 0.0)
    return {"p_buy_actual": buy_act, "p_sell_actual": sell_act, "revenue_gbp": energy_gbp - standby_gbp, "standby_gbp": standby_gbp}


def run_model(model: str, forecasts: pd.DataFrame | None, world: World, perfect: bool = False) -> ModelRun:
    """The rolling loop for one model (``perfect=True`` ignores ``forecasts`` and plans on actuals)."""
    params, trace, index, fcfg = world.params, world.trace, world.index, world.fcfg
    soc = world.soc0
    rows, per_issue = [], []
    for issue in world.issues:
        targets = issue_targets(issue, fcfg)
        i = int(index.get_loc(issue))
        H = len(targets)
        if not (index[i : i + H] == targets).all():
            raise ValueError(f"index does not match the targets of issue {issue}")
        if perfect:
            buy_fc, sell_fc = perfect_foresight_prices(world.prices, targets, world.tariff)
        else:
            buy_fc, sell_fc = forecast_prices(forecasts, model, world.series, issue, targets, world.tariff)
        departures = [(d - i, e) for d, e in trace.departures if i < d <= i + H]
        floor = floor_learned(H, departures, params, reserve=world.reserve)
        t0 = time.perf_counter()
        res = solve_window(params, buy_fc, sell_fc, trace.plugged[i : i + H], trace.drive_kwh[i : i + H], floor, soc)
        seconds = time.perf_counter() - t0
        k = execution_length(issue, targets, fcfg)
        ts = targets[:k]
        settled = _settle(world, ts, res.p_ch[:k], res.p_dis[:k], trace.plugged[i : i + k])
        rows.append(
            pd.DataFrame(
                {
                    "model": model,
                    "issue_time_utc": issue,
                    "ts_utc": ts,
                    "plugged": trace.plugged[i : i + k],
                    "p_charge_kw": res.p_ch[:k],
                    "p_discharge_kw": res.p_dis[:k],
                    "p_signed_kw": res.p_ch[:k] - res.p_dis[:k],  # + = charging (grid -> vehicle)
                    "soc": res.soc[:k],  # at the START of each period
                    "drive_kwh": trace.drive_kwh[i : i + k],
                    "p_buy_forecast": buy_fc[:k],
                    "p_sell_forecast": sell_fc[:k],
                    **settled,
                }
            )
        )
        per_issue.append(
            {"model": model, "issue_time_utc": issue, "plan_periods": H, "act_periods": k, "solver": res.solver,
             "solve_seconds": seconds, "max_shortfall": float(res.shortfall[: k + 1].max()), "soc_start": soc, "soc_end": float(res.soc[k])}
        )
        soc = float(res.soc[k])
    schedule = pd.concat(rows, ignore_index=True)
    return ModelRun(model, schedule, pd.DataFrame(per_issue), soc_end=soc)


def run_unmanaged_baseline(world: World, name: str = "unmanaged") -> ModelRun:
    """The teammate's normal-charger baseline over the same executed span, settled identically."""
    T = world.executed_until
    base = run_unmanaged(world.params, world.trace, world.soc0, T)
    ts = world.index[:T]
    settled = _settle(world, ts, base["p_ch"], base["p_dis"], world.trace.plugged[:T])
    schedule = pd.DataFrame(
        {"model": name, "issue_time_utc": pd.to_datetime([pd.NaT] * T, utc=True), "ts_utc": ts, "plugged": world.trace.plugged[:T],
         "p_charge_kw": base["p_ch"], "p_discharge_kw": base["p_dis"], "p_signed_kw": base["p_ch"] - base["p_dis"],
         "soc": base["soc"][:T], "drive_kwh": world.trace.drive_kwh[:T], "p_buy_forecast": np.nan, "p_sell_forecast": np.nan, **settled}
    )
    schedule["issue_time_utc"] = schedule["issue_time_utc"].dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
    return ModelRun(name, schedule, pd.DataFrame(), soc_end=float(base["soc"][T]))


def check_physics(run: ModelRun, world: World, tol: float = 1e-6) -> dict:
    """§12 checks on an executed schedule: energy balance, SoC bounds, no simultaneous charge/discharge."""
    s, p = run.schedule, world.params
    soc_replay = integrate_soc(p, world.soc0, s["p_charge_kw"].to_numpy(), s["p_discharge_kw"].to_numpy(), s["drive_kwh"].to_numpy())
    balance = float(np.abs(soc_replay[:-1] - s["soc"].to_numpy()).max())
    return {
        "energy_balance_max_abs_soc_error": balance,
        "soc_min": float(s["soc"].min()),
        "soc_max": float(s["soc"].max()),
        "within_bounds": bool(s["soc"].min() >= p.soc_min - 1e-6 and s["soc"].max() <= p.soc_max + 1e-6),
        "simultaneous_periods": int(((s["p_charge_kw"] > tol) & (s["p_discharge_kw"] > tol)).sum()),
    }
