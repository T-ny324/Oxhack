"""Tests for the S3 -> S5 integration (price adapter, rolling backtest, accounting).

Synthetic forecasts and prices only; the teammate's optimiser is loaded through its bridge.
Lives in the repository root because only this environment can import both packages.
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from v2g.forecast import timing as tm
from v2g.forecast.config import load_forecast_config

REPO = Path(__file__).resolve().parents[1]
SUB = REPO / "8b_singleev_optimiser" / "v2g-hack"
TZ = ZoneInfo("Europe/London")
FCFG = load_forecast_config(REPO / "config" / "assumptions.yaml")


def _bridge():
    spec = importlib.util.spec_from_file_location(
        "ev_optimiser_bridge", SUB / "src" / "v2g" / "optimise" / "bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load()


OPT = _bridge()
A = OPT.schema.load_assumptions(SUB / "config" / "assumptions.yaml")
SPEC = OPT.schema.load_spec(REPO / "config" / "converter_spec.yaml")
PARAMS = OPT.daily.params_from_config(SPEC, A)
BT = A.forecast_backtest
MODELS = ["good", "noisy", "naive_like"]


def utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC").as_unit("ns")


def synthetic_prices(
    start: str, end: str, seed: int = 0, negative_days: tuple[str, ...] = ()
) -> pd.Series:
    """Actual wholesale: daily shape + noise, with optional negative mornings and a few missing periods."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq="30min", tz="UTC", inclusive="left", unit="ns")
    local = idx.tz_convert(TZ)
    h = (local.hour + local.minute / 60).to_numpy()
    price = (
        70
        - 30 * (h < 6)
        + 70 * ((h >= 16.5) & (h < 19.5))
        - 15 * ((h >= 11) & (h < 15))
        + rng.normal(0, 6, len(idx))
    )
    s = pd.Series(price, index=idx, name="actual")
    for day in negative_days:
        mask = (local.date == date.fromisoformat(day)) & (h >= 1) & (h < 5)
        s[mask] = -40.0
    return s


def synthetic_forecasts(
    prices: pd.Series, issues: pd.DatetimeIndex, seed: int = 1
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for issue in issues:
        targets = tm.target_times(issue, FCFG)
        actual = prices.reindex(targets).interpolate(limit_direction="both").to_numpy()
        for model, noise in (("good", 4.0), ("noisy", 25.0), ("naive_like", 0.0)):
            if model == "naive_like":  # copies the price 24 h earlier, like naive_daily
                p50 = (
                    prices.reindex(targets - pd.Timedelta(days=1))
                    .interpolate(limit_direction="both")
                    .to_numpy()
                )
            else:
                p50 = actual + rng.normal(0, noise, len(targets))
            rows.append(
                pd.DataFrame(
                    {
                        "model": model,
                        "series": "elexon_mid",
                        "issue_time_utc": issue,
                        "target_time_utc": targets,
                        "p50": p50,
                        "q10": p50 - 10,
                        "q90": p50 + 10,
                    }
                )
            )
    out = pd.concat(rows, ignore_index=True)
    for col in ("issue_time_utc", "target_time_utc"):
        out[col] = out[col].astype("datetime64[ns, UTC]")
    return out


def make_world(start: str, end: str, prices: pd.Series):
    issues = tm.issue_times(start, end, FCFG)
    sessions = OPT.availability.sessions_from_profile(
        BT.profile,
        date.fromisoformat(start) - timedelta(days=1),
        date.fromisoformat(end) + timedelta(days=2),
        A.timezone_local,
    )
    return OPT.rolling_backtest.build_world(
        issues,
        sessions,
        prices,
        PARAMS,
        A.tariff,
        FCFG,
        SPEC.standby_w,
        A.battery.soc_initial,
        BT.floor_reserve,
        "elexon_mid",
    )


@pytest.fixture(scope="module")
def month():
    prices = synthetic_prices("2025-10-18", "2025-11-20", negative_days=("2025-11-05",))
    prices[utc("2025-11-08T14:00")] = (
        np.nan
    )  # a missing actual inside an execution window
    prices[utc("2025-11-08T14:30")] = np.nan
    world = make_world(
        "2025-10-24", "2025-11-14", prices
    )  # spans the autumn clock change
    forecasts = synthetic_forecasts(prices, world.issues)
    runs = {m: OPT.rolling_backtest.run_model(m, forecasts, world) for m in MODELS}
    runs["perfect_foresight"] = OPT.rolling_backtest.run_model(
        "perfect_foresight", None, world, perfect=True
    )
    runs["unmanaged"] = OPT.rolling_backtest.run_unmanaged_baseline(world)
    return world, forecasts, runs


# ------------------------------------------------------------------ price adapter


def test_forecast_prices_select_one_issue_on_the_target_index():
    prices = synthetic_prices("2025-10-18", "2025-11-20")
    issues = tm.issue_times("2025-10-24", "2025-10-27", FCFG)
    forecasts = synthetic_forecasts(prices, issues)
    for issue, expected in zip(issues, (76, 78, 76, 76)):
        targets = OPT.price_inputs.issue_targets(issue, FCFG)
        assert len(targets) == expected and targets.equals(tm.target_times(issue, FCFG))
        buy, sell = OPT.price_inputs.forecast_prices(
            forecasts, "good", "elexon_mid", issue, targets, A.tariff
        )
        own = (
            forecasts[
                (forecasts["model"] == "good") & (forecasts["issue_time_utc"] == issue)
            ]
            .set_index("target_time_utc")["p50"]
            .reindex(targets)
            .to_numpy()
        )
        np.testing.assert_allclose(buy, own + A.tariff.import_adder_per_mwh)
        np.testing.assert_allclose(sell, own + A.tariff.export_adder_per_mwh)
        assert len(buy) == expected
    spring = tm.issue_times("2026-03-28", "2026-03-28", FCFG)[0]
    assert len(OPT.price_inputs.issue_targets(spring, FCFG)) == 74


def test_execution_window_is_46_48_or_50_periods():
    for day, expected in (("2025-11-10", 48), ("2025-10-25", 50), ("2026-03-28", 46)):
        issue = tm.issue_times(day, day, FCFG)[0]
        assert (
            OPT.price_inputs.execution_length(
                issue, OPT.price_inputs.issue_targets(issue, FCFG), FCFG
            )
            == expected
        )


def test_later_issues_never_leak_into_an_issue_and_missing_issue_raises():
    prices = synthetic_prices("2025-10-18", "2025-11-20")
    issues = tm.issue_times("2025-11-03", "2025-11-06", FCFG)
    forecasts = synthetic_forecasts(prices, issues)
    issue = issues[1]
    targets = OPT.price_inputs.issue_targets(issue, FCFG)
    before = OPT.price_inputs.forecast_prices(
        forecasts, "good", "elexon_mid", issue, targets, A.tariff
    )
    perturbed = forecasts.copy()
    perturbed.loc[
        perturbed["issue_time_utc"] > issue, "p50"
    ] += 10_000.0  # overlapping targets, later issues
    after = OPT.price_inputs.forecast_prices(
        perturbed, "good", "elexon_mid", issue, targets, A.tariff
    )
    np.testing.assert_array_equal(before[0], after[0])
    with pytest.raises(OPT.price_inputs.MissingForecastIssue):
        OPT.price_inputs.forecast_prices(
            forecasts[forecasts["issue_time_utc"] != issue],
            "good",
            "elexon_mid",
            issue,
            targets,
            A.tariff,
        )
    with pytest.raises(OPT.price_inputs.MissingForecastIssue, match="lacks"):
        OPT.price_inputs.forecast_prices(
            forecasts.iloc[5:],
            "good",
            "elexon_mid",
            issues[0],
            OPT.price_inputs.issue_targets(issues[0], FCFG),
            A.tariff,
        )
    world = make_world("2025-11-03", "2025-11-06", prices)
    with pytest.raises(OPT.price_inputs.MissingForecastIssue):
        OPT.rolling_backtest.run_model(
            "good", forecasts[forecasts["issue_time_utc"] != issue], world
        )


# ------------------------------------------------------------------- leakage


def test_decisions_do_not_depend_on_actual_prices_after_the_issue():
    prices = synthetic_prices("2025-10-18", "2025-11-20")
    issues = tm.issue_times("2025-11-03", "2025-11-05", FCFG)
    forecasts = synthetic_forecasts(prices, issues)
    world_a = make_world("2025-11-03", "2025-11-05", prices)
    run_a = OPT.rolling_backtest.run_model("good", forecasts, world_a)
    perturbed = prices.copy()
    perturbed[perturbed.index > issues[0]] += 500.0
    world_b = make_world("2025-11-03", "2025-11-05", perturbed)
    run_b = OPT.rolling_backtest.run_model("good", forecasts, world_b)
    for col in ("p_charge_kw", "p_discharge_kw", "soc"):
        np.testing.assert_array_equal(
            run_a.schedule[col].to_numpy(), run_b.schedule[col].to_numpy()
        )
    assert not np.allclose(
        run_a.schedule["revenue_gbp"], run_b.schedule["revenue_gbp"]
    )  # settlement does change
    pf_a = OPT.rolling_backtest.run_model("pf", None, world_a, perfect=True)
    pf_b = OPT.rolling_backtest.run_model("pf", None, world_b, perfect=True)
    assert not np.array_equal(
        pf_a.schedule["p_charge_kw"], pf_b.schedule["p_charge_kw"]
    ) or not np.array_equal(
        pf_a.schedule["p_discharge_kw"], pf_b.schedule["p_discharge_kw"]
    )


# ------------------------------------------------------- physics and ordering


def test_schedules_are_physically_consistent(month):
    world, _, runs = month
    for name, run in runs.items():
        checks = OPT.rolling_backtest.check_physics(run, world)
        assert checks["energy_balance_max_abs_soc_error"] < 1e-6, name
        assert checks["within_bounds"], (name, checks)
        assert checks["simultaneous_periods"] == 0, name
        if name != "unmanaged":
            assert (
                run.issues["max_shortfall"] < 1e-6
            ).all(), name  # departure floor met in every issue
    # negative prices were present and triggered the MILP path somewhere
    assert (runs["good"].issues["solver"].str.contains("MILP")).any()


def test_departure_soc_met_in_every_case(month):
    world, _, runs = month
    T = world.executed_until
    for name in ("good", "noisy", "perfect_foresight"):
        soc = runs[name].schedule["soc"].to_numpy()
        for d, trip in world.trace.departures:
            if d < T:
                need = PARAMS.soc_min + trip / PARAMS.e_cap_kwh
                assert soc[d] >= need - 1e-6, (name, d, soc[d], need)


def test_revenue_ordering_perfect_ge_models_ge_unmanaged(month):
    world, _, runs = month
    table = OPT.decision_value.summarise(runs, world, "perfect_foresight")
    rev = table["net_revenue_gbp"]
    for m in MODELS:
        assert rev["perfect_foresight"] >= rev[m] - 1e-6, m
        assert rev[m] >= rev["unmanaged"] - 1e-6, m
    assert rev["good"] > rev["noisy"]
    assert table.loc["perfect_foresight", "decision_value_gbp"] == pytest.approx(0.0)
    assert table.loc["perfect_foresight", "pct_of_perfect_saving"] == pytest.approx(
        100.0
    )
    assert (
        table["excluded_periods"].drop("unmanaged") == 2
    ).all()  # the two NaN actuals in an executed window
    assert np.isfinite(table["net_revenue_gbp"]).all()


def test_soc_carries_over_between_issues_and_execution_windows_tile(month):
    world, _, runs = month
    run = runs["good"]
    issues = run.issues
    np.testing.assert_allclose(
        issues["soc_start"].iloc[1:].to_numpy(), issues["soc_end"].iloc[:-1].to_numpy()
    )
    s = run.schedule
    assert s["ts_utc"].is_monotonic_increasing and s["ts_utc"].is_unique
    assert (s["ts_utc"].diff().dropna() == pd.Timedelta("30min")).all()
    assert issues["act_periods"].tolist() == [
        50 if d == date(2025, 10, 25) else 48
        for d in issues["issue_time_utc"].dt.tz_convert(TZ).dt.date
    ]
    assert s["ts_utc"].iloc[0] == world.issues[0] and len(s) == world.executed_until


def test_outputs_written_with_contracted_columns(month, tmp_path):
    world, _, runs = month
    table = OPT.decision_value.summarise(runs, world, "perfect_foresight")
    path = OPT.decision_value.write_outputs(runs, table, tmp_path)
    assert path.name == "revenue_per_vehicle.json" and path.exists()
    sched = pd.read_parquet(tmp_path / "good.parquet")
    assert list(sched.columns) == OPT.decision_value.SCHEDULE_COLUMNS
    assert str(sched["ts_utc"].dtype) == "datetime64[ns, UTC]"
    assert (
        sched["p_signed_kw"] == sched["p_charge_kw"] - sched["p_discharge_kw"]
    ).all()
    fig = OPT.schedule_plot.plot_week(
        runs["good"].schedule,
        PARAMS,
        A.timezone_local,
        "2025-11-03",
        tmp_path / "week.png",
        "test",
    )
    assert fig.exists() and fig.stat().st_size > 10_000
