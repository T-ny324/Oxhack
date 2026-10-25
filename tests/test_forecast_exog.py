"""Tests for the as-of exogenous selector and its use in LEAR features (synthetic, no network)."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from shared.schema import ForecastConfig
from v2g.forecast import exog as ex
from v2g.forecast import lear
from v2g.forecast import lear_data as ld
from v2g.forecast import lear_features as lf
from v2g.forecast import timing as tm

TZ = ZoneInfo("Europe/London")
VARS = ["wind_da_mw", "embedded_wind_mw", "embedded_solar_mw", "demand_da_mw"]


def make_cfg(**overrides) -> ForecastConfig:
    exog = {
        "enabled_for_models": ["lear_x"],
        "variables": VARS,
        "exog_known_lag_minutes": 0,
        "include_residual_load": True,
        "sources": None,
    }
    exog.update(overrides.pop("exog", {}))
    lear_cfg = {
        "calibration_windows_days": [28],
        "recalibrate_every_days": 1,
        "impute_max_gap_periods": 8,
        "lambda_selection": "aic",
        "n_jobs": 1,
        "write_component_models": False,
        "lambda_selection_by_model": {},
    }
    lear_cfg.update(overrides.pop("lear", {}))
    return ForecastConfig(
        series="elexon_mid",
        issue_local_time="10:00",
        timezone="Europe/London",
        price_known_lag_periods=2,
        smoke_window={"start": "2025-11-03", "end": "2025-11-30"},
        backtest_window={"start": "2025-10-01", "end": "2026-09-30"},
        quantile_history_days=14,
        lead_buckets_h=[0, 6, 12, 24, 40],
        lear=lear_cfg,
        exog=exog,
        validation_window={"start": "2025-04-01", "end": "2025-09-30"},
        **overrides,
    )


CFG = make_cfg()


def utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC").as_unit("ns")


def issue_on(d: date, cfg: ForecastConfig = CFG) -> pd.Timestamp:
    return tm.issue_times(d, d, cfg)[0]


def synthetic_vintages(
    start: str, end: str, cfg: ForecastConfig = CFG, seed: int = 0
) -> pd.DataFrame:
    """Hourly publications (hh:12) covering the next 48 h per variable, value = f(target, made)."""
    rng = np.random.default_rng(seed)
    made_times = pd.date_range(
        start, end, freq="h", tz="UTC", inclusive="left"
    ) + pd.Timedelta(minutes=12)
    rows = []
    level = {
        "wind_da_mw": 8000.0,
        "embedded_wind_mw": 3000.0,
        "embedded_solar_mw": 2000.0,
        "demand_da_mw": 30000.0,
    }
    for made in made_times:
        first = made.ceil("30min")
        targets = pd.date_range(
            first, first + pd.Timedelta(hours=48), freq="30min", inclusive="left"
        )
        hours = targets.hour + targets.minute / 60.0
        for var in VARS:
            base = (
                level[var]
                + 500.0 * np.sin(hours / 24.0 * 2 * np.pi)
                + rng.normal(0, 50.0, len(targets))
            )
            # later vintages are different, so "latest" is observable
            value = base + (made.value // 10**9 % 7919) * 0.001
            rows.append(
                pd.DataFrame(
                    {
                        "source": "synthetic",
                        "variable": var,
                        "target_time_utc": targets,
                        "forecast_made_utc": made,
                        "value_mw": value,
                        "capacity_mw": np.nan,
                    }
                )
            )
    return pd.concat(rows, ignore_index=True)


TABLE = synthetic_vintages("2025-10-20", "2025-11-20")


@pytest.fixture(scope="module")
def vintages() -> ex.ExogVintages:
    return ex.ExogVintages(TABLE, CFG)


# ------------------------------------------------------------------ as-of logic


def test_snapshot_uses_latest_vintage_at_or_before_issue_and_ignores_later_ones(
    vintages,
):
    issue = issue_on(date(2025, 11, 10))  # 10:00 GMT; latest publication is 09:12
    snap = vintages.asof_snapshot(issue)
    table = TABLE
    target = utc("2025-11-10T15:00")
    sub = table[
        (table["variable"] == "demand_da_mw") & (table["target_time_utc"] == target)
    ]
    usable = sub[sub["forecast_made_utc"] <= issue].sort_values("forecast_made_utc")
    assert usable["forecast_made_utc"].iloc[-1] == utc("2025-11-10T09:12")
    assert snap.values.loc[target, "demand_da_mw"] == pytest.approx(
        usable["value_mw"].iloc[-1]
    )
    later = sub[sub["forecast_made_utc"] > issue]["value_mw"]
    assert not np.isclose(later, snap.values.loc[target, "demand_da_mw"]).any()
    assert snap.values.index[0] == utc("2025-11-10T00:00") and snap.values.index[
        -1
    ] == utc("2025-11-11T23:30")
    assert not snap.missing.any().any()
    assert list(snap.values.columns) == [*VARS, "residual_load"]
    expected_residual = (
        snap.values["demand_da_mw"]
        - snap.values["wind_da_mw"]
        - snap.values["embedded_wind_mw"]
        - snap.values["embedded_solar_mw"]
    )
    pd.testing.assert_series_equal(
        snap.values["residual_load"], expected_residual, check_names=False
    )


def test_known_lag_moves_the_cutoff_back():
    lagged = ex.ExogVintages(
        synthetic_vintages("2025-11-08", "2025-11-12"),
        make_cfg(exog={"exog_known_lag_minutes": 60}),
    )
    issue = issue_on(date(2025, 11, 10))
    target = utc("2025-11-10T15:00")
    table = synthetic_vintages("2025-11-08", "2025-11-12")
    sub = table[
        (table["variable"] == "wind_da_mw") & (table["target_time_utc"] == target)
    ]
    expected = (
        sub[sub["forecast_made_utc"] <= issue - pd.Timedelta(minutes=60)]
        .sort_values("forecast_made_utc")["value_mw"]
        .iloc[-1]
    )
    assert lagged.asof_snapshot(issue).values.loc[
        target, "wind_da_mw"
    ] == pytest.approx(expected)


def test_fallback_to_older_vintage_and_missing_flag():
    table = synthetic_vintages("2025-11-08", "2025-11-12")
    issue = issue_on(date(2025, 11, 10))
    # remove every publication after 2025-11-09 06:00 -> tomorrow's late periods come from older vintages or are missing
    cut = table[table["forecast_made_utc"] <= utc("2025-11-09T06:12")]
    snap = ex.ExogVintages(cut, CFG).asof_snapshot(issue)
    covered = snap.values["demand_da_mw"].notna()
    assert covered.loc[
        utc("2025-11-10T12:00")
    ]  # within 48 h of the 06:12 publication on the 9th
    assert not covered.loc[
        utc("2025-11-11T12:00")
    ]  # beyond every remaining vintage's horizon
    assert snap.missing.loc[utc("2025-11-11T12:00"), "demand_da_mw"]
    assert snap.missing.loc[utc("2025-11-11T12:00"), "residual_load"]
    last_covered = snap.values["demand_da_mw"].last_valid_index()
    assert last_covered == utc(
        "2025-11-11T06:00"
    )  # 06:12 on the 9th + 48 h, last full half-hour


def test_slot_layout_has_fixed_length_on_46_and_50_period_days():
    v = ex.ExogVintages(synthetic_vintages("2025-10-20", "2025-11-20"), CFG)
    spring = ex.ExogVintages(synthetic_vintages("2026-03-20", "2026-04-05"), CFG)
    n = len(v.series) * 2 * 48
    assert len(v.feature_names) == n == 5 * 96
    for vint, d in [
        (v, date(2025, 10, 25)),
        (v, date(2025, 10, 26)),
        (v, date(2025, 11, 10)),
        (spring, date(2026, 3, 28)),
        (spring, date(2026, 3, 29)),
    ]:
        layout = vint.slot_layout(issue_on(d))
        assert layout.shape == (n,) and np.isfinite(layout).all(), d
    assert (
        v.feature_names[0] == "wind_da_d0_s00"
        and v.feature_names[95] == "wind_da_d1_s47"
    )
    assert v.feature_names[-1] == "residual_load_d1_s47"
    # autumn day d+1 (2025-10-26 for an issue on the 25th): repeated slots 2/3 are averages of two periods
    snap = v.asof_snapshot(issue_on(date(2025, 10, 25)))
    series = snap.values["demand_da_mw"]
    layout = v.slot_layout(issue_on(date(2025, 10, 25)))
    demand_d1 = layout[3 * 96 + 48 : 3 * 96 + 96]
    both = series.loc[
        [utc("2025-10-26T00:00"), utc("2025-10-26T01:00")]
    ]  # 01:00 BST and 01:00 GMT
    assert demand_d1[2] == pytest.approx(both.mean())


def test_actuals_guard_refuses_tables_without_vintages():
    table = synthetic_vintages("2025-11-08", "2025-11-12").drop(
        columns="forecast_made_utc"
    )
    with pytest.raises(ValueError, match="forecast_made_utc"):
        ex.ExogVintages(table, CFG)
    prices = ld.prices_from_day_matrix(
        pd.DataFrame(
            50.0,
            index=pd.Index([date(2025, 11, 1) + timedelta(days=i) for i in range(12)]),
            columns=range(48),
        ),
        CFG,
    )
    data = ld.prepare(prices, CFG)
    with pytest.raises(ValueError, match="forecast_made_utc"):
        lf.build_features(
            issue_on(date(2025, 11, 10)),
            data.day_matrix,
            data.prices_raw,
            CFG,
            exog=table,
        )
    with pytest.raises(TypeError):
        lf.build_features(
            issue_on(date(2025, 11, 10)),
            data.day_matrix,
            data.prices_raw,
            CFG,
            exog=object(),
        )
    with pytest.raises(ValueError, match="missing from the table"):
        ex.ExogVintages(
            synthetic_vintages("2025-11-08", "2025-11-12")[
                lambda t: t["variable"] != "demand_da_mw"
            ],
            CFG,
        )


# ---------------------------------------------------------- features / leakage


def ar_prices(start: str, n_days: int, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    x = np.zeros((n_days, 48))
    x[0] = rng.normal(0, 5, 48)
    for t in range(1, n_days):
        x[t] = 0.8 * x[t - 1] + rng.normal(0, 5, 48)
    index = pd.Index(
        [date.fromisoformat(start) + timedelta(days=i) for i in range(n_days)],
        name="local_date",
    )
    return ld.prices_from_day_matrix(
        pd.DataFrame(50.0 + x, index=index, columns=range(48)), CFG
    )


def test_exog_features_appended_through_the_hook_and_not_price_transformed():
    prices = ar_prices("2025-09-01", 90)
    data = ld.prepare(prices, CFG)
    v = ex.ExogVintages(synthetic_vintages("2025-10-20", "2025-11-20"), CFG)
    issue = issue_on(date(2025, 11, 10))
    plain, names_plain = lf.build_features(issue, data.day_matrix, data.prices_raw, CFG)
    with_x, names_x = lf.build_features(
        issue, data.day_matrix, data.prices_raw, CFG, exog=v
    )
    assert (
        names_x[: len(names_plain)] == names_plain
        and names_x[len(names_plain) :] == v.feature_names
    )
    np.testing.assert_array_equal(with_x[: len(plain)], plain)
    np.testing.assert_array_equal(with_x[len(plain) :], v.slot_layout(issue))
    mask = lf.price_feature_mask(names_x)
    assert (
        mask[: 19 + 5 * 48].all() and not mask[19 + 5 * 48 :].any()
    )  # exog columns are z-scored only


def test_perturbation_after_issue_leaves_lear_x_features_and_predictions_unchanged():
    d = date(2025, 11, 12)
    issue = issue_on(d)
    cutoff = tm.known_cutoff(issue, CFG)
    prices = ar_prices("2025-08-01", 130)
    table = synthetic_vintages("2025-09-25", "2025-11-20")

    prices_p = prices.copy()
    prices_p[prices_p.index > cutoff] += 1000.0
    table_p = table.copy()
    table_p.loc[table_p["forecast_made_utc"] > issue, "value_mw"] += 10_000.0

    data_a, data_b = ld.prepare(prices, CFG), ld.prepare(prices_p, CFG)
    exog_a, exog_b = ex.ExogVintages(table, CFG), ex.ExogVintages(table_p, CFG)
    fa, _ = lf.build_features(
        issue, data_a.day_matrix, data_a.prices_raw, CFG, exog=exog_a
    )
    fb, _ = lf.build_features(
        issue, data_b.day_matrix, data_b.prices_raw, CFG, exog=exog_b
    )
    np.testing.assert_array_equal(fa, fb)

    fc_a = lear.LearForecaster(data_a, CFG, model_name="lear_x", exog=exog_a)
    fc_b = lear.LearForecaster(data_b, CFG, model_name="lear_x", exog=exog_b)
    pred_a = fc_a.forecast_issue(d, fc_a.fit(d))
    pred_b = fc_b.forecast_issue(d, fc_b.fit(d))
    pd.testing.assert_frame_equal(pred_a, pred_b)
    assert len(fc_a.names) == 266 + 5 * 96 and np.isfinite(pred_a["p50"]).all()


def test_lear_x_rows_with_missing_exog_are_dropped_in_training_and_zero_filled_at_prediction(
    caplog,
):
    d = date(2025, 11, 12)
    prices = ar_prices("2025-08-01", 130)
    table = synthetic_vintages(
        "2025-10-20", "2025-11-20"
    )  # no vintages before 2025-10-20
    data = ld.prepare(prices, CFG)
    fc = lear.LearForecaster(
        data, CFG, model_name="lear_x", exog=ex.ExogVintages(table, CFG)
    )
    fits = fc.fit(
        d
    )  # training dates 2025-10-15..2025-11-10: the first days lack exog and are dropped
    assert fits[0].n_train.max() < 28 and fits[0].n_train.min() >= lear.MIN_TRAIN_ROWS
    # a day whose exog is entirely missing: predictions still finite and the gap is logged
    with caplog.at_level("WARNING"):
        out = fc.forecast_issue(date(2025, 10, 18), fits)
    assert np.isfinite(out["p50"]).all() and "NaN features replaced" in caplog.text
