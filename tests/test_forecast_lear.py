"""Tests for LEAR (stage S3, ladder step 2). Synthetic prices only; no network."""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from shared.schema import ForecastConfig
from v2g.forecast import baselines as bl
from v2g.forecast import evaluate as ev
from v2g.forecast import io as fio
from v2g.forecast import lear
from v2g.forecast import lear_data as ld
from v2g.forecast import lear_features as lf
from v2g.forecast import timing as tm

TZ = ZoneInfo("Europe/London")


def make_cfg(**lear_overrides) -> ForecastConfig:
    lear_cfg = {
        "calibration_windows_days": [28],
        "recalibrate_every_days": 1,
        "impute_max_gap_periods": 8,
        "lambda_selection": "aic",
        "n_jobs": 1,
        "write_component_models": False,
        **lear_overrides,
    }
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
        exog={
            "enabled_for_models": ["lear_x"],
            "variables": ["wind_da_mw", "demand_da_mw"],
            "exog_known_lag_minutes": 0,
            "include_residual_load": True,
            "sources": None,
        },
        validation_window={"start": "2025-04-01", "end": "2025-09-30"},
        guard={"lo": -500, "hi": 5000},
    )


CFG = make_cfg()


def utc(stamp: str) -> pd.Timestamp:
    return pd.Timestamp(stamp, tz="UTC").as_unit("ns")


def issue_on(d: date, cfg: ForecastConfig = CFG) -> pd.Timestamp:
    return tm.issue_times(d, d, cfg)[0]


def ar_day_matrix(
    start: date, n_days: int, rho=0.8, sigma=5.0, level=50.0, seed=0
) -> pd.DataFrame:
    """Each local slot follows its own AR(1): x_t = rho * x_{t-1} + noise."""
    rng = np.random.default_rng(seed)
    x = np.zeros((n_days, ld.SLOTS))
    x[0] = rng.normal(0.0, sigma, ld.SLOTS)
    for t in range(1, n_days):
        x[t] = rho * x[t - 1] + rng.normal(0.0, sigma, ld.SLOTS)
    index = pd.Index(
        [start + timedelta(days=i) for i in range(n_days)], name="local_date"
    )
    return pd.DataFrame(level + x, index=index, columns=range(ld.SLOTS))


def ar_prices(start: str, n_days: int, **kwargs) -> pd.Series:
    return ld.prices_from_day_matrix(
        ar_day_matrix(date.fromisoformat(start), n_days, **kwargs), CFG
    )


def local_hour_prices(start_utc: str, end_utc: str) -> pd.Series:
    idx = pd.date_range(
        start_utc, end_utc, freq="30min", tz="UTC", inclusive="left", unit="ns"
    )
    local = idx.tz_convert(TZ)
    return pd.Series(
        (local.hour + local.minute / 60.0).to_numpy(dtype="float64"),
        index=idx,
        name="actual",
    )


def write_prices_parquet(prices: pd.Series, path: Path) -> Path:
    pd.DataFrame(
        {
            "ts_utc": prices.index,
            "price_wholesale_per_mwh": prices.to_numpy(),
            "volume_mwh": 1000.0,
            "provider": "APXMIDP",
            "is_missing": np.isnan(prices.to_numpy()),
        }
    ).to_parquet(path, index=False)
    return path


# ------------------------------------------------------------------------ data


def test_impute_short_gaps_fills_only_short_interior_runs():
    s = pd.Series(
        np.arange(30, dtype="float64"),
        index=pd.date_range("2025-01-01", periods=30, freq="30min", tz="UTC"),
    )
    s.iloc[3:6] = np.nan  # 3 missing: fill
    s.iloc[12:22] = np.nan  # 10 missing: keep
    s.iloc[29] = np.nan  # trailing: nothing to interpolate towards
    filled, imputed = ld.impute_short_gaps(s, 8)
    np.testing.assert_allclose(filled.iloc[3:6], [3.0, 4.0, 5.0])
    assert imputed.iloc[3:6].all() and imputed.sum() == 3
    assert filled.iloc[12:22].isna().all()
    assert np.isnan(filled.iloc[29])
    assert filled.iloc[:3].tolist() == [0.0, 1.0, 2.0]


def test_day_matrix_dst_conventions_and_partial_days():
    assert ld.day_length_hours(date(2025, 10, 26), CFG) == 25
    assert ld.day_length_hours(date(2026, 3, 29), CFG) == 23
    assert ld.day_length_hours(date(2025, 11, 10), CFG) == 24

    prices = local_hour_prices("2025-10-20T00:00Z", "2025-11-03T00:00Z")
    # second (GMT) occurrences of local 01:00/01:30 on the autumn change day: make them visibly different
    prices[utc("2025-10-26T01:00")] += 1.0
    prices[utc("2025-10-26T01:30")] += 1.0
    m = ld.build_day_matrix(prices, CFG)
    assert list(m.columns) == list(range(48))
    np.testing.assert_allclose(
        m.loc[date(2025, 10, 21)].to_numpy(), np.arange(48) / 2.0
    )
    assert m.loc[date(2025, 10, 26), 2] == pytest.approx(1.5)  # mean of 1.0 and 2.0
    assert m.loc[date(2025, 10, 26), 3] == pytest.approx(2.0)  # mean of 1.5 and 2.5
    # first local day starts at 00:00 BST = 23:00 UTC the day before, so slots 0 and 1 are absent on a 24 h day: stay NaN
    assert (
        m.loc[date(2025, 10, 20), [0, 1]].isna().all()
        and m.loc[date(2025, 10, 20), 2] == 1.0
    )

    spring = ld.build_day_matrix(
        local_hour_prices("2026-03-25T00:00Z", "2026-04-02T00:00Z"), CFG
    )
    assert spring.loc[date(2026, 3, 29), 2] == pytest.approx(
        1.0
    )  # interpolated between slot 1 (0.5) and slot 4 (2.0)
    assert spring.loc[date(2026, 3, 29), 3] == pytest.approx(1.5)
    assert spring.loc[date(2026, 3, 27)].notna().all()


def test_prices_from_day_matrix_round_trips():
    matrix = ar_day_matrix(date(2025, 10, 20), 14)
    prices = ld.prices_from_day_matrix(matrix, CFG)
    assert len(prices) == 13 * 48 + 50  # includes the 25 h day
    back = ld.build_day_matrix(prices, CFG)
    np.testing.assert_allclose(back.to_numpy(), matrix.to_numpy())


# -------------------------------------------------------------------- features


def test_features_have_fixed_layout_across_dst_days():
    assert (
        lf.issue_slot(CFG) == 20
        and lf.last_known_slot(CFG) == 18
        and len(lf.model_keys(CFG)) == 76
    )
    autumn = ld.prepare(ar_prices("2025-07-01", 200), CFG)
    spring = ld.prepare(ar_prices("2026-01-01", 120), CFG)
    cases = [
        (autumn, date(2025, 10, 26)),
        (autumn, date(2025, 11, 10)),
        (spring, date(2026, 3, 29)),
    ]
    for data, d in cases:
        vec, names = lf.build_features(
            issue_on(d), data.day_matrix, data.prices_raw, CFG
        )
        assert len(vec) == len(names) == 19 + 5 * 48 + 7
        assert np.isfinite(vec).all()
        assert (
            names[:2] == ["today_s00", "today_s01"]
            and names[18] == "today_s18"
            and names[19] == "dm1_s00"
        )
        assert names[-7:] == [f"dow_{n}" for n in lf.DAY_NAMES]
        np.testing.assert_allclose(
            vec[:19], data.day_matrix.loc[d].to_numpy()[:19]
        )  # gap-free data: same as the matrix
        for k, offset in zip(lf.PAST_DAYS, range(19, 19 + 5 * 48, 48)):
            np.testing.assert_allclose(
                vec[offset : offset + 48],
                data.day_matrix.loc[d - timedelta(days=k)].to_numpy(),
            )
        assert vec[-7:].sum() == 1.0 and vec[-7 + d.weekday()] == 1.0
    assert lf.price_feature_mask(names).sum() == 19 + 5 * 48


def test_features_and_predictions_unchanged_when_prices_after_cutoff_change():
    d = date(2025, 11, 12)
    issue = issue_on(d)
    cutoff = tm.known_cutoff(issue, CFG)
    base = ar_prices("2025-07-01", 160)
    base[cutoff] = (
        np.nan
    )  # the last known period is missing: imputation must not look past the cutoff
    perturbed = base.copy()
    perturbed[perturbed.index > cutoff] += 1000.0

    data_a, data_b = ld.prepare(base, CFG), ld.prepare(perturbed, CFG)
    fa, names_a = lf.build_features(issue, data_a.day_matrix, data_a.prices_raw, CFG)
    fb, names_b = lf.build_features(issue, data_b.day_matrix, data_b.prices_raw, CFG)
    assert names_a == names_b
    np.testing.assert_array_equal(fa, fb)
    assert np.isfinite(fa).all()

    fc_a, fc_b = lear.LearForecaster(data_a, CFG), lear.LearForecaster(data_b, CFG)
    pred_a = fc_a.forecast_issue(d, fc_a.fit(d))
    pred_b = fc_b.forecast_issue(d, fc_b.fit(d))
    pd.testing.assert_frame_equal(pred_a, pred_b)


def test_training_set_only_uses_issues_whose_targets_are_known():
    d = date(2025, 11, 12)
    dates = lear.LearForecaster.training_dates(d, 28)
    assert len(dates) == 28
    assert max(dates) == d - timedelta(days=2)
    assert dates == sorted(dates) and all(
        (b - a).days == 1 for a, b in itertools.pairwise(dates)
    )
    cutoff = tm.known_cutoff(issue_on(d), CFG)
    last_target = tm.target_times(issue_on(max(dates)), CFG)[-1]
    assert last_target < cutoff
    # the matrix rows used for training targets never go beyond d - 1
    data = ld.prepare(ar_prices("2025-07-01", 160), CFG)
    y = lf.training_targets(data.day_matrix, max(dates), CFG)
    assert len(y) == 76 and np.isfinite(y).all()


# ------------------------------------------------------------------- transform


def test_price_transform_round_trip_and_training_only_scope():
    train = np.array([10.0, 50.0, 80.0, -30.0, 2000.0])
    t = lear.PriceTransform.fit(train)
    assert t.med == 50.0
    assert t.mad == pytest.approx(np.median(np.abs(train - 50.0)) / 0.6745)
    p = np.array([-50.0, 0.0, 49.99, 50.0, 1500.0, 10000.0])
    np.testing.assert_allclose(t.inverse(t.forward(p)), p, rtol=0, atol=1e-9)
    assert (
        lear.PriceTransform.fit(np.array([5.0, 5.0, 5.0])).mad == 1.0
    )  # degenerate window guard

    data = ld.prepare(ar_prices("2025-07-01", 160), CFG)
    fc = lear.LearForecaster(data, CFG)
    d = date(2025, 11, 12)
    first = fc.training_dates(d, 28)[0]
    fitted = fc._transform_window(first, d)
    idx = data.day_matrix.index
    rows = data.day_matrix.loc[
        (idx >= first - timedelta(days=7)) & (idx <= d - timedelta(days=1))
    ]
    assert fitted.med == np.median(rows.to_numpy())
    data.day_matrix.loc[
        idx >= d
    ] += 10_000.0  # anything from the test day on must not move the transform
    assert fc._transform_window(first, d) == fitted


# ----------------------------------------------------------------- predictions


@pytest.mark.parametrize(
    "start, n_days, issue_date, expected",
    [
        ("2025-07-01", 160, date(2025, 10, 25), 78),  # next day is the autumn change
        ("2025-07-01", 160, date(2025, 11, 10), 76),
        ("2026-01-01", 120, date(2026, 3, 28), 74),  # next day is the spring change
    ],
)
def test_each_issue_gets_exactly_its_target_times(start, n_days, issue_date, expected):
    cfg = make_cfg(
        calibration_windows_days=[56]
    )  # n = 56 < 266 features: exercises the noise-variance path
    data = ld.prepare(ar_prices(start, n_days), cfg)
    fc = lear.LearForecaster(data, cfg)
    fits = fc.fit(issue_date)
    out = fc.forecast_issue(issue_date, fits)
    assert len(out) == expected
    pd.testing.assert_index_equal(
        pd.DatetimeIndex(out["target_time_utc"]),
        tm.target_times(issue_on(issue_date), cfg),
        check_names=False,
        check_freq=False,
    )
    assert np.isfinite(out["p50"]).all() and np.isfinite(out["p50_w56"]).all()
    fit = fits[0]
    assert (
        fit.n_train.max() <= 56
        and np.isfinite(fit.alpha).all()
        and (fit.alpha > 0).all()
    )
    assert (fit.nnz <= fit.coef.shape[1]).all()
    if (
        expected == 78
    ):  # both occurrences of a repeated local slot share one model and one p50
        dup = out.groupby(["block", "slot"])["p50"].agg(["size", "nunique"])
        assert (dup["size"] == 2).sum() == 2 and (
            dup.loc[dup["size"] == 2, "nunique"] == 1
        ).all()


def test_signal_recovery_beats_naive_daily_and_selects_yesterday_same_slot():
    # An n > p window with BIC. On this low-signal synthetic series AIC keeps ~80 of 266
    # features at n = 364 and the noise they add cancels the gain (a property of the
    # criterion, not of the wiring: the true feature is still selected on every slot).
    prices = ar_prices("2024-09-01", 440, rho=0.8, sigma=5.0, seed=3)
    cfg = make_cfg(calibration_windows_days=[364], lambda_selection="bic")
    data = ld.prepare(prices, cfg)
    fc = lear.LearForecaster(data, cfg)
    first, last = date(2025, 11, 1), date(2025, 11, 14)
    fits = fc.fit(first)
    rows = pd.concat(
        [
            fc.forecast_issue(first + timedelta(days=i), fits)
            for i in range((last - first).days + 1)
        ]
    )
    rows["actual"] = fio.lookup_prices(prices, rows["target_time_utc"])
    naive = bl.forecast_points(
        "naive_daily", tm.issue_times(first, last, cfg), prices, cfg
    )
    naive["actual"] = fio.lookup_prices(prices, naive["target_time_utc"])
    mae_lear = (rows["actual"] - rows["p50"]).abs().mean()
    mae_naive = (naive["actual"] - naive["p50"]).abs().mean()
    assert mae_lear < 0.85 * mae_naive, (mae_lear, mae_naive)

    def dm1_selected(fit) -> float:
        return float(
            np.mean(
                [
                    fit.coef[fc.key_index[("A", s)], fc.names.index(f"dm1_s{s:02d}")]
                    != 0
                    for s in range(lf.issue_slot(cfg), 48)
                ]
            )
        )

    assert dm1_selected(fits[0]) >= 0.7
    assert fits[0].nnz.mean() < 30  # BIC keeps the model sparse
    fits_aic = lear.LearForecaster(data, make_cfg(calibration_windows_days=[364])).fit(
        first
    )
    assert (
        dm1_selected(fits_aic[0]) >= 0.7
    )  # AIC over-selects but still finds yesterday's same slot
    assert fits_aic[0].nnz.mean() > fits[0].nnz.mean()


def test_runs_are_deterministic_and_parallel_matches_serial():
    data = ld.prepare(ar_prices("2025-07-01", 160), CFG)
    d = date(2025, 11, 12)
    fc1 = lear.LearForecaster(data, CFG)
    out1 = fc1.forecast_issue(d, fc1.fit(d))
    fc2 = lear.LearForecaster(data, CFG)
    out2 = fc2.forecast_issue(d, fc2.fit(d))
    pd.testing.assert_frame_equal(out1, out2)
    fc_par = lear.LearForecaster(data, make_cfg(n_jobs=2))
    out_par = fc_par.forecast_issue(d, fc_par.fit(d))
    pd.testing.assert_frame_equal(out1, out_par)


# ------------------------------------------------------------------ end to end


def test_run_upserts_next_to_naive_rows_and_evaluates_with_dm(tmp_path):
    cfg = make_cfg(recalibrate_every_days=7)
    prices = ar_prices("2025-07-01", 150)
    prices_path = write_prices_parquet(prices, tmp_path / "prices.parquet")
    out = tmp_path / "price_quantiles.parquet"
    bl.run(
        ["naive_daily", "naive_weekly"],
        "2025-11-10",
        "2025-11-14",
        cfg,
        prices_path=prices_path,
        out_path=out,
    )

    result = lear.run(
        "2025-11-10",
        "2025-11-14",
        cfg,
        prices_path=prices_path,
        out_path=out,
        write_components=True,
        diagnostics_path=tmp_path / "diag.parquet",
        top_features_path=tmp_path / "top.csv",
        feature_groups_path=tmp_path / "groups.csv",
    )
    written = fio.load_forecasts(out)
    fio.validate_forecasts(written, cfg)
    assert written["model"].value_counts().to_dict() == {
        "lear": 380,
        "lear_w28": 380,
        "naive_daily": 380,
        "naive_weekly": 380,
    }
    lear_rows = written[written["model"] == "lear"]
    assert np.isfinite(lear_rows["p50"]).all()
    assert (lear_rows["q10"] <= lear_rows["p50"]).all() and (
        lear_rows["p50"] <= lear_rows["q90"]
    ).all()
    assert result.n_fits == 4  # 21 warm-up days + 5 in-window, refit every 7 days
    assert result.runtime_s > 0 and set(result.points["block"]) == {"A", "B"}

    diag = pd.read_parquet(tmp_path / "diag.parquet")
    assert list(diag.columns) == [
        "fit_date",
        "window_days",
        "block",
        "slot",
        "alpha",
        "nnz",
        "n_train",
    ]
    assert len(diag) == result.n_fits * 76 and np.isfinite(diag["alpha"]).all()
    top = pd.read_csv(tmp_path / "top.csv")
    assert top.groupby("block").size().to_dict() == {"A": 15, "B": 15}
    assert top["selection_rate"].between(0, 1).all()
    groups = pd.read_csv(tmp_path / "groups.csv")
    same = groups[groups["metric"] == "same_slot_selection_rate"]
    assert set(same["group"]) == {"today", "dm1", "dm2", "dm3", "dm6", "dm7"}
    assert same["value"].dropna().between(0, 1).all()
    assert np.isnan(
        same.loc[(same["block"] == "A") & (same["group"] == "today"), "value"]
    ).all()  # no Block A slot is known today
    assert (groups.loc[groups["metric"] == "mean_selected_count", "value"] >= 0).all()
    assert lear.format_feature_groups(result.feature_groups).startswith(
        "  "
    ) or "same-slot" in lear.format_feature_groups(result.feature_groups)

    scores = ev.run(
        "2025-11-10",
        "2025-11-14",
        cfg,
        forecasts_path=out,
        prices_path=prices_path,
        out_path=tmp_path / "eval.csv",
    )
    assert {"lear", "lear_w28", "naive_daily", "naive_weekly"} <= set(scores["model"])
    dm = scores.attrs["dm"]
    assert set(dm["model"]) == {"lear", "lear_w28"}
    assert set(dm["benchmark"]) == {"naive_daily", "naive_weekly", "lear"}
    assert not ((dm["model"] == "lear") & (dm["benchmark"] == "lear")).any()
    identical = (dm["model"] == "lear_w28") & (
        dm["benchmark"] == "lear"
    )  # one window: same forecasts
    assert np.isnan(
        dm.loc[identical, "dm_stat"]
    ).all()  # zero loss differential -> undefined statistic
    assert np.isfinite(dm.loc[~identical, "dm_stat"]).all()
    assert dm.loc[~identical, "p_value"].between(0, 1).all()
    assert (tmp_path / "eval_dm.csv").exists()
    table = ev.show_issue(written, prices, "2025-11-12", cfg)
    assert {"lear.p50", "naive_daily.p50", "actual"} <= set(table.columns) and len(
        table
    ) == 76
