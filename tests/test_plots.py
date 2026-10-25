"""Smoke tests for the notebook plot helpers on a tiny synthetic forecast/price frame (no files, no network)."""

from __future__ import annotations

from datetime import date

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest
from matplotlib.figure import Figure

from shared.schema import ForecastConfig
from v2g.forecast import plots as pl
from v2g.forecast import timing as tm

CFG = ForecastConfig(
    series="elexon_mid",
    issue_local_time="10:00",
    timezone="Europe/London",
    price_known_lag_periods=2,
    smoke_window={"start": "2025-11-03", "end": "2025-11-30"},
    backtest_window={"start": "2025-10-01", "end": "2026-09-30"},
    quantile_history_days=14,
    lead_buckets_h=[0, 6, 12, 24, 40],
    lear={
        "calibration_windows_days": [28],
        "recalibrate_every_days": 1,
        "impute_max_gap_periods": 8,
        "lambda_selection": "aic",
        "n_jobs": 1,
        "write_component_models": False,
        "lambda_selection_by_model": {},
    },
    exog={
        "enabled_for_models": ["lear_x"],
        "variables": ["wind_da_mw"],
        "exog_known_lag_minutes": 0,
        "include_residual_load": False,
        "sources": None,
    },
    validation_window={"start": "2025-04-01", "end": "2025-09-30"},
    guard={"lo": -500, "hi": 5000},
)
START, END = date(2026, 3, 20), date(
    2026, 4, 2
)  # includes the 2026-03-28 issue (74 targets)


def synthetic() -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(0)
    issues = tm.issue_times(START, END, CFG)
    prices = pd.Series(
        60.0
        + 25 * np.sin(np.arange(48 * 20) / 48 * 2 * np.pi)
        + rng.normal(0, 8, 48 * 20),
        index=pd.date_range(
            "2026-03-19", periods=48 * 20, freq="30min", tz="UTC", unit="ns"
        ),
        name="actual",
    )
    prices.iloc[300:310] -= 120  # negative prices
    prices.iloc[500] = 400  # a spike
    rows = []
    for model, noise in [
        ("naive_weekly", 20.0),
        ("naive_daily", 15.0),
        ("lear", 10.0),
        ("lear_x", 7.0),
    ]:
        for issue in issues:
            targets = tm.target_times(issue, CFG)
            actual = prices.reindex(targets).to_numpy()
            p50 = actual + rng.normal(0, noise, len(targets))
            rows.append(
                pd.DataFrame(
                    {
                        "model": model,
                        "series": "elexon_mid",
                        "issue_time_utc": issue,
                        "target_time_utc": targets,
                        "p50": p50,
                        "q10": p50 - 1.3 * noise,
                        "q90": p50 + 1.3 * noise,
                    }
                )
            )
    forecasts = pd.concat(rows, ignore_index=True)
    for col in ("issue_time_utc", "target_time_utc"):
        forecasts[col] = forecasts[col].astype("datetime64[ns, UTC]")
    return forecasts, prices


@pytest.fixture(scope="module")
def data() -> pl.BacktestData:
    forecasts, prices = synthetic()
    return pl.from_frames(forecasts, prices, CFG, START, END)


def test_from_frames_adds_display_columns_and_keeps_model_order(data):
    assert data.models == pl.MODEL_ORDER and data.missing_models == []
    assert {
        "actual",
        "lead_h",
        "block",
        "hour_of_day",
        "month",
        "lead_hour",
        "regime",
        "target_local",
    } <= set(data.frame.columns)
    assert str(data.frame["target_local"].dt.tz) == "Europe/London"
    assert set(data.frame["regime"]) <= set(pl.REGIME_LABELS)
    assert "synthetic" not in pl.describe(data) and "scored rows" in pl.describe(data)


def test_missing_models_are_skipped_with_a_note():
    forecasts, prices = synthetic()
    data = pl.from_frames(
        forecasts[forecasts["model"] != "lear_x"], prices, CFG, START, END
    )
    assert data.models == [
        "naive_weekly",
        "naive_daily",
        "lear",
    ] and data.missing_models == ["lear_x"]
    assert "lear_x" in pl.describe(data)
    assert isinstance(pl.plot_mae_bars(data), Figure)


def test_summary_and_calibration_tables(data):
    table = pl.summary_table(data)
    assert list(table.index) == [pl.MODEL_LABELS[m] for m in pl.MODEL_ORDER]
    assert table[
        "MAE (£/MWh)"
    ].is_monotonic_decreasing  # noise levels were chosen in that order
    assert table.loc[
        pl.MODEL_LABELS["naive_weekly"], "rMAE vs naive weekly"
    ] == pytest.approx(1.0)
    cal = pl.calibration_table(data)
    assert cal["inside P10–P90"].between(50, 100).all()
    dm = pl.dm_matrix(data)
    assert np.isnan(np.diag(dm.to_numpy(dtype="float64"))).all()
    assert dm.loc["lear_x", "naive_weekly"] < 0.05


@pytest.mark.parametrize(
    "fn",
    [
        pl.plot_mae_bars,
        pl.plot_block_mae,
        pl.plot_mae_by_time_of_day,
        pl.plot_mae_by_month,
        pl.plot_mae_by_price_regime,
        pl.plot_calibration,
        pl.plot_dm_matrix,
        pl.plot_mae_by_lead,
        pl.plot_width_by_lead,
        pl.plot_pred_vs_actual,
    ],
)
def test_figure_helpers_return_figures(data, fn):
    fig = fn(data)
    assert isinstance(fig, Figure) and len(fig.axes) >= 1
    matplotlib.pyplot.close(fig)


def test_issue_selection_and_issue_plots(data, tmp_path):
    typical = pl.typical_issue(data)
    assert typical in set(data.frame["issue_time_utc"])
    contrasting = pl.contrasting_issues(data)
    assert len(contrasting) == 4
    assert any(k.startswith("28 Mar 2026") for k in contrasting)
    fig = pl.plot_issue(data, typical)
    assert isinstance(fig, Figure)
    grid = pl.plot_issue_grid(data, contrasting)
    assert len([a for a in grid.axes if a.has_data()]) == 4
    path = pl.save(fig, "test_issue", tmp_path)
    assert path.exists() and path.stat().st_size > 1000
    matplotlib.pyplot.close("all")


def test_convergence_uses_both_issues(data):
    target_day = date(2026, 3, 25)
    earlier, later = pl.convergence_pair(data, target_day)
    assert (earlier["block"] == "B").all() and (later["block"] == "A").all()
    assert len(earlier) == 48 and len(later) == 28
    summary = pl.convergence_summary(data)
    assert list(summary.index) == [
        "issued the day before (Block B)",
        "issued the same day (Block A)",
    ]
    assert np.isfinite(summary).all()
    assert isinstance(pl.plot_convergence(data, target_day), Figure)
    matplotlib.pyplot.close("all")
