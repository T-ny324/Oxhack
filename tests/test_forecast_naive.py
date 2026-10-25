"""Tests for the stage S3 naive baselines and shared forecast plumbing.

Synthetic prices only; no network, no files outside tmp_path.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import yaml

from shared.schema import Assumptions, ForecastConfig
from v2g.forecast import baselines as bl
from v2g.forecast import evaluate as ev
from v2g.forecast import io as fio
from v2g.forecast import timing as tm
from v2g.forecast.config import load_forecast_config, window_dates

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/London")
CFG = ForecastConfig(
    series="elexon_mid",
    issue_local_time="10:00",
    timezone="Europe/London",
    price_known_lag_periods=2,
    smoke_window={"start": "2025-11-03", "end": "2025-11-30"},
    backtest_window={"start": "2025-10-01", "end": "2026-09-30"},
    quantile_history_days=56,
    lead_buckets_h=[0, 6, 12, 24, 40],
    lear={
        "calibration_windows_days": [56, 84, 364, 728],
        "recalibrate_every_days": 1,
        "impute_max_gap_periods": 8,
        "lambda_selection": "aic",
        "n_jobs": -1,
        "write_component_models": False,
        "lambda_selection_by_model": {"lear": "aic", "lear_x": "aic"},
    },
    exog={
        "enabled_for_models": ["lear_x"],
        "variables": [
            "wind_da_mw",
            "embedded_wind_mw",
            "embedded_solar_mw",
            "demand_da_mw",
        ],
        "exog_known_lag_minutes": 0,
        "include_residual_load": True,
        "sources": ["elexon_ndf", "elexon_windfor", "neso_embedded_forecast"],
    },
    validation_window={"start": "2025-04-01", "end": "2025-09-30"},
    guard={"lo": -500, "hi": 5000},
)
HALF_HOUR = pd.Timedelta("30min")


def local(wall_clock: str) -> pd.Timestamp:
    """Europe/London wall-clock -> UTC Timestamp (unambiguous inputs only)."""
    return pd.Timestamp(wall_clock, tz=TZ).tz_convert("UTC").as_unit("ns")


def utc(stamp: str) -> pd.Timestamp:
    return pd.Timestamp(stamp, tz="UTC").as_unit("ns")


def make_prices(start="2025-07-01", end="2026-06-01", values=None, seed=0) -> pd.Series:
    """Synthetic actuals Series shaped like load_prices() output."""
    idx = pd.date_range(start, end, freq="30min", tz="UTC", inclusive="left", unit="ns")
    if values is None:
        values = 50.0 + np.random.default_rng(seed).normal(0.0, 10.0, len(idx))
    elif callable(values):
        values = values(idx)
    return pd.Series(np.asarray(values, dtype="float64"), index=idx, name="actual")


def local_hour_of_day(idx: pd.DatetimeIndex) -> np.ndarray:
    loc = idx.tz_convert(TZ)
    return (loc.hour + loc.minute / 60.0).to_numpy(dtype="float64")


def write_prices_parquet(prices: pd.Series, path: Path) -> Path:
    df = pd.DataFrame(
        {
            "ts_utc": prices.index,
            "price_wholesale_per_mwh": prices.to_numpy(),
            "volume_mwh": 1000.0,
            "provider": "APXMIDP",
            "is_missing": np.isnan(prices.to_numpy()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


# ---------------------------------------------------------------------- config


def test_assumptions_yaml_has_a_valid_forecast_section():
    cfg = load_forecast_config(REPO / "config" / "assumptions.yaml")
    assert cfg == CFG
    with open(REPO / "config" / "assumptions.yaml", encoding="utf-8") as fh:
        Assumptions.model_validate(yaml.safe_load(fh))  # whole file still validates
    assert window_dates(cfg, "smoke") == (date(2025, 11, 3), date(2025, 11, 30))
    assert window_dates(cfg, "backtest") == (date(2025, 10, 1), date(2026, 9, 30))
    assert window_dates(cfg, "validation") == (date(2025, 4, 1), date(2025, 9, 30))


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        ForecastConfig(**{**CFG.model_dump(), "lead_buckets_h": [0, 12, 6]})
    with pytest.raises(ValueError):
        ForecastConfig(**{**CFG.model_dump(), "timezone": "Mars/Olympus"})
    with pytest.raises(ValueError):
        ForecastConfig(**{**CFG.model_dump(), "issue_local_time": "10am"})


# ---------------------------------------------------------------------- timing


def test_issue_times_follow_local_clock_bst_and_gmt():
    issues = tm.issue_times("2025-07-01", "2025-07-01", CFG)
    assert str(issues.dtype) == "datetime64[ns, UTC]"
    assert issues[0] == utc("2025-07-01T09:00")  # BST: 10:00 local = 09:00 UTC
    assert tm.issue_times("2025-12-01", "2025-12-01", CFG)[0] == utc(
        "2025-12-01T10:00"
    )  # GMT
    month = tm.issue_times("2025-10-01", "2025-10-31", CFG)
    assert len(month) == 31
    assert month.is_monotonic_increasing
    assert month[24] == utc("2025-10-25T09:00") and month[25] == utc(
        "2025-10-26T10:00"
    )  # clock change


@pytest.mark.parametrize(
    "issue_local_date, expected",
    [
        ("2025-11-10", 76),  # ordinary GMT day
        ("2025-10-25", 78),  # next day is the autumn clock change (25 h)
        ("2026-03-28", 74),  # next day is the spring clock change (23 h)
        ("2025-10-26", 76),  # issued on the change day itself
    ],
)
def test_target_times_lengths(issue_local_date, expected):
    issue = tm.issue_times(issue_local_date, issue_local_date, CFG)[0]
    targets = tm.target_times(issue, CFG)
    assert len(targets) == expected
    assert targets[0] == issue
    assert ((targets[1:] - targets[:-1]) == HALF_HOUR).all()
    # wall-clock midnight closing the next local day (naive arithmetic, then localise)
    next_day_end = (
        (pd.Timestamp(issue_local_date) + pd.Timedelta(days=2))
        .tz_localize(TZ)
        .tz_convert("UTC")
    )
    assert targets[-1] == next_day_end - HALF_HOUR


def test_known_cutoff_lead_bucket_and_block():
    issue = utc("2025-11-10T10:00")
    assert tm.known_cutoff(issue, CFG) == utc("2025-11-10T09:00")
    targets = tm.target_times(issue, CFG)
    lead = tm.lead_time_hours(pd.Series([issue] * len(targets)), pd.Series(targets))
    assert lead.iloc[0] == 0.0 and lead.iloc[-1] == 37.5
    buckets = tm.lead_bucket(lead, CFG)
    assert (
        buckets.iloc[0] == "0-6h"
        and buckets.iloc[12] == "6-12h"
        and buckets.iloc[-1] == "24-40h"
    )
    assert buckets.value_counts().to_dict() == {
        "0-6h": 12,
        "6-12h": 12,
        "12-24h": 24,
        "24-40h": 28,
    }
    block = tm.target_block(pd.Series([issue] * len(targets)), pd.Series(targets), CFG)
    assert block.value_counts().to_dict() == {"A": 28, "B": 48}
    with pytest.raises(ValueError):
        tm.lead_bucket(pd.Series([41.0]), CFG)


def test_local_to_utc_dst_rules():
    out = tm.local_to_utc(
        pd.DatetimeIndex(["2026-03-29 01:30", "2025-10-26 01:30", "2025-07-01 13:00"]),
        CFG,
    )
    assert out[0] == utc("2026-03-29T00:30")  # nonexistent -> one hour earlier
    assert out[1] == utc("2025-10-26T00:30")  # ambiguous -> first (BST) occurrence
    assert out[2] == utc("2025-07-01T12:00")
    assert str(out.dtype) == "datetime64[ns, UTC]"


# ------------------------------------------------------------------- baselines


def ref_for(
    points: pd.DataFrame, issue: pd.Timestamp, target: pd.Timestamp
) -> pd.Timestamp:
    row = points[
        (points["issue_time_utc"] == issue) & (points["target_time_utc"] == target)
    ]
    assert len(row) == 1, (issue, target)
    return row["ref_time_utc"].iloc[0]


@pytest.mark.parametrize("model", bl.MODELS)
def test_no_leakage_across_a_dst_month(model):
    prices = make_prices()
    issues = tm.issue_times("2025-10-01", "2025-10-31", CFG)  # includes 2025-10-26
    points = bl.forecast_points(model, issues, prices, CFG)
    cutoff = tm.known_cutoff(points["issue_time_utc"], CFG)
    assert (points["ref_time_utc"] <= cutoff).all()
    assert points["p50"].notna().all()
    np.testing.assert_allclose(
        points["p50"], prices.reindex(points["ref_time_utc"]).to_numpy()
    )
    assert len(points) == 30 * 76 + 78
    assert list(points.columns) == bl.POINT_COLUMNS


def test_naive_weekly_references_same_local_time_seven_days_earlier():
    prices = make_prices()
    issue = tm.issue_times("2025-11-12", "2025-11-12", CFG)[0]
    points = bl.forecast_points("naive_weekly", pd.DatetimeIndex([issue]), prices, CFG)
    assert ref_for(points, issue, local("2025-11-12 15:00")) == local(
        "2025-11-05 15:00"
    )
    assert ref_for(points, issue, local("2025-11-13 03:00")) == local(
        "2025-11-06 03:00"
    )


def test_naive_daily_weekday_rule():
    prices = make_prices()
    wed = tm.issue_times("2025-11-12", "2025-11-12", CFG)[0]  # Wednesday
    pts = bl.forecast_points("naive_daily", pd.DatetimeIndex([wed]), prices, CFG)
    # Block A, Wed target -> previous day (Tue), before the cutoff
    assert ref_for(pts, wed, local("2025-11-12 15:00")) == local("2025-11-11 15:00")
    # Block B, Thu 15:00 -> Wed 15:00 is after the 09:00 cutoff -> the day before that
    assert ref_for(pts, wed, local("2025-11-13 15:00")) == local("2025-11-11 15:00")
    # Block B, Thu 03:00 -> Wed 03:00 is before the cutoff, so it stands
    assert ref_for(pts, wed, local("2025-11-13 03:00")) == local("2025-11-12 03:00")

    fri = tm.issue_times("2025-11-14", "2025-11-14", CFG)[0]
    pts = bl.forecast_points("naive_daily", pd.DatetimeIndex([fri]), prices, CFG)
    assert ref_for(pts, fri, local("2025-11-15 12:00")) == local(
        "2025-11-08 12:00"
    )  # Saturday -> 7 days

    sun = tm.issue_times("2025-11-16", "2025-11-16", CFG)[0]
    pts = bl.forecast_points("naive_daily", pd.DatetimeIndex([sun]), prices, CFG)
    assert ref_for(pts, sun, local("2025-11-16 12:00")) == local(
        "2025-11-09 12:00"
    )  # Sunday -> 7 days
    assert ref_for(pts, sun, local("2025-11-17 12:00")) == local(
        "2025-11-10 12:00"
    )  # Monday -> 7 days

    mon = tm.issue_times("2025-11-17", "2025-11-17", CFG)[0]
    pts = bl.forecast_points("naive_daily", pd.DatetimeIndex([mon]), prices, CFG)
    assert ref_for(pts, mon, local("2025-11-18 12:00")) == local(
        "2025-11-16 12:00"
    )  # Tue -> Mon (late) -> Sun


def test_missing_reference_steps_back_seven_days_and_never_yields_nan():
    prices = make_prices()
    prices[local("2025-11-05 15:00")] = np.nan  # weekly reference of Wed 12 Nov 15:00
    prices[local("2025-11-11 15:00")] = (
        np.nan
    )  # daily reference of Wed 12 Nov 15:00 (and Thu 13 Nov 15:00)
    wed = tm.issue_times("2025-11-12", "2025-11-12", CFG)[0]

    weekly = bl.forecast_points("naive_weekly", pd.DatetimeIndex([wed]), prices, CFG)
    assert ref_for(weekly, wed, local("2025-11-12 15:00")) == local("2025-10-29 15:00")
    daily = bl.forecast_points("naive_daily", pd.DatetimeIndex([wed]), prices, CFG)
    assert ref_for(daily, wed, local("2025-11-12 15:00")) == local("2025-11-04 15:00")
    assert ref_for(daily, wed, local("2025-11-13 15:00")) == local("2025-11-04 15:00")
    for pts in (weekly, daily):
        assert pts["p50"].notna().all()
        assert (pts["ref_time_utc"] <= tm.known_cutoff(wed, CFG)).all()


def test_reference_missing_for_five_weeks_raises():
    prices = make_prices()
    for weeks in range(
        1, 6
    ):  # same LOCAL time each week (naive arithmetic, then localise)
        prices[
            (
                pd.Timestamp("2025-11-12 15:00") - pd.Timedelta(days=7 * weeks)
            ).tz_localize(TZ)
        ] = np.nan
    wed = tm.issue_times("2025-11-12", "2025-11-12", CFG)[0]
    with pytest.raises(ValueError, match="no usable reference"):
        bl.forecast_points("naive_weekly", pd.DatetimeIndex([wed]), prices, CFG)


def test_naive_weekly_reproduces_local_hour_across_autumn_dst_change():
    prices = make_prices(values=local_hour_of_day)
    issues = tm.issue_times(
        "2025-10-20", "2025-11-05", CFG
    )  # clocks go back 2025-10-26
    points = bl.forecast_points("naive_weekly", issues, prices, CFG)
    expected = local_hour_of_day(pd.DatetimeIndex(points["target_time_utc"]))
    np.testing.assert_array_equal(points["p50"].to_numpy(), expected)


def test_dst_edge_cases_in_reference_lookup():
    prices = make_prices()
    # Target Sun 2026-04-05 01:30 local; 7 days earlier 01:30 does not exist (spring forward) -> 00:30 local
    sat = tm.issue_times("2026-04-04", "2026-04-04", CFG)[0]
    pts = bl.forecast_points("naive_weekly", pd.DatetimeIndex([sat]), prices, CFG)
    assert ref_for(pts, sat, local("2026-04-05 01:30")) == utc("2026-03-29T00:30")
    # Target Sun 2025-11-02 01:30 local; 7 days earlier is ambiguous -> first (BST) occurrence
    sat = tm.issue_times("2025-11-01", "2025-11-01", CFG)[0]
    pts = bl.forecast_points("naive_weekly", pd.DatetimeIndex([sat]), prices, CFG)
    assert ref_for(pts, sat, local("2025-11-02 01:30")) == utc("2025-10-26T00:30")


# ---------------------------------------------------------- validator / writer


def valid_frame(
    model="naive_weekly", issue_dates=("2025-11-10", "2025-11-11"), with_q=True
) -> pd.DataFrame:
    issues = tm.issue_times(issue_dates[0], issue_dates[-1], CFG)
    frame = bl.build_issue_target_frame(issues, CFG)
    frame["model"], frame["series"], frame["p50"] = model, "elexon_mid", 50.0
    if with_q:
        frame["q10"], frame["q90"] = 45.0, 55.0
    return frame[[c for c in fio.CONTRACT_COLUMNS if c in frame.columns]]


def test_validator_accepts_a_correct_frame_with_or_without_quantiles():
    fio.validate_forecasts(valid_frame(), CFG)
    fio.validate_forecasts(valid_frame(with_q=False), CFG)


def test_validator_rejects_contract_violations():
    with pytest.raises(ValueError, match="duplicate"):
        fio.validate_forecasts(pd.concat([valid_frame(), valid_frame().iloc[:1]]), CFG)

    naive = valid_frame()
    naive["target_time_utc"] = naive["target_time_utc"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="datetime64\\[ns, UTC\\]"):
        fio.validate_forecasts(naive, CFG)

    bad_q = valid_frame()
    bad_q.loc[3, "q10"] = 60.0
    with pytest.raises(ValueError, match="q10 <= p50 <= q90"):
        fio.validate_forecasts(bad_q, CFG)

    with pytest.raises(ValueError, match="target periods missing"):
        fio.validate_forecasts(valid_frame().drop(index=5), CFG)

    extra = valid_frame().iloc[:1].copy()
    extra["target_time_utc"] = extra["target_time_utc"] + pd.Timedelta(days=3)
    with pytest.raises(ValueError, match="unexpected"):
        fio.validate_forecasts(pd.concat([valid_frame(), extra]), CFG)

    nan_p50 = valid_frame()
    nan_p50.loc[0, "p50"] = np.nan
    with pytest.raises(ValueError, match="p50 contains NaN"):
        fio.validate_forecasts(nan_p50, CFG)

    wrong_issue = valid_frame()
    wrong_issue["issue_time_utc"] = wrong_issue["issue_time_utc"] + pd.Timedelta(
        hours=1
    )
    with pytest.raises(ValueError, match="not at 10:00"):
        fio.validate_forecasts(wrong_issue, CFG)

    with pytest.raises(ValueError, match="unexpected columns"):
        fio.validate_forecasts(
            valid_frame().assign(ref_time_utc=utc("2025-01-01T00:00")), CFG
        )


def test_validator_enforces_the_plausibility_guard():
    too_high = valid_frame()
    too_high.loc[4, "q90"] = 12_000.0
    with pytest.raises(ValueError, match="plausibility guard"):
        fio.validate_forecasts(too_high, CFG)
    too_low = valid_frame(with_q=False)
    too_low.loc[2, "p50"] = -750.0
    with pytest.raises(ValueError, match="plausibility guard"):
        fio.validate_forecasts(too_low, CFG)
    edge = valid_frame()
    edge.loc[0, "q10"], edge.loc[0, "p50"], edge.loc[0, "q90"] = (
        -500.0,
        0.0,
        5000.0,
    )  # bounds are inclusive
    fio.validate_forecasts(edge, CFG)
    with pytest.raises(ValueError, match="guard.hi"):
        ForecastConfig(**{**CFG.model_dump(), "guard": {"lo": 100, "hi": 100}})


def test_write_forecasts_upserts_by_model(tmp_path):
    path = tmp_path / "price_quantiles.parquet"
    fio.write_forecasts(valid_frame("naive_weekly"), path, CFG)
    fio.write_forecasts(valid_frame("naive_daily"), path, CFG)
    both = fio.load_forecasts(path)
    assert both["model"].value_counts().to_dict() == {
        "naive_daily": 152,
        "naive_weekly": 152,
    }
    assert both[fio.KEY_COLUMNS].equals(
        both[fio.KEY_COLUMNS].sort_values(fio.KEY_COLUMNS).reset_index(drop=True)
    )

    replacement = valid_frame("naive_daily")
    replacement[["q10", "p50", "q90"]] = [65.0, 70.0, 75.0]
    fio.write_forecasts(replacement, path, CFG)
    after = fio.load_forecasts(path)
    assert after["model"].value_counts().to_dict() == {
        "naive_daily": 152,
        "naive_weekly": 152,
    }
    assert (after.loc[after["model"] == "naive_daily", "p50"] == 70.0).all()
    assert (after.loc[after["model"] == "naive_weekly", "p50"] == 50.0).all()
    assert len(fio.load_forecasts(path, model="naive_weekly")) == 152
    assert str(after["issue_time_utc"].dtype) == "datetime64[ns, UTC]"


# -------------------------------------------------------------------- quantiles


def test_quantiles_use_only_errors_known_at_the_cutoff():
    prices = make_prices(values=lambda idx: np.full(len(idx), 50.0))
    issues = tm.issue_times("2025-11-10", "2025-11-11", CFG)
    points = bl.build_issue_target_frame(issues, CFG).assign(
        model="naive_weekly", series="elexon_mid", p50=50.0
    )
    second_cutoff = tm.known_cutoff(issues[1], CFG)
    # Actuals after the second issue's cutoff are wildly off; they are not known at that issue.
    prices[prices.index > second_cutoff] = 1000.0

    out = bl.add_quantiles(points, prices, CFG)
    first, second = (out[out["issue_time_utc"] == iss] for iss in issues)
    assert first[["q10", "q90"]].isna().all().all()  # warm-up: no history yet
    assert (second["q10"] == 50.0).all() and (
        second["q90"] == 50.0
    ).all()  # known errors are all zero


def test_quantile_coverage_on_stationary_series_is_about_80_percent(tmp_path):
    prices = make_prices("2025-08-01", "2026-01-01", seed=1)
    out = tmp_path / "pq.parquet"
    result = bl.run(
        ["naive_weekly"],
        "2025-11-01",
        "2025-12-15",
        CFG,
        prices_path=write_prices_parquet(prices, tmp_path / "prices.parquet"),
        out_path=out,
    )
    assert result[["q10", "q90"]].notna().all().all()
    assert (result["q10"] <= result["p50"]).all() and (
        result["p50"] <= result["q90"]
    ).all()
    actual = fio.lookup_prices(prices, result["target_time_utc"])
    coverage = np.mean((result["q10"] <= actual) & (actual <= result["q90"]))
    assert 0.72 <= coverage <= 0.88, coverage
    scores = ev.score(fio.load_forecasts(out), prices, CFG)
    overall = scores[(scores["slice_type"] == "overall")].iloc[0]
    assert 0.72 <= overall["coverage"] <= 0.88
    assert overall["rmae"] == pytest.approx(1.0)


# ------------------------------------------------------------------ end to end


def test_run_writes_contract_rows_and_evaluate_scores_them(tmp_path):
    prices = make_prices("2025-08-01", "2026-01-01")
    prices_path = write_prices_parquet(prices, tmp_path / "prices.parquet")
    out = tmp_path / "price_quantiles.parquet"
    result = bl.run(
        list(bl.MODELS),
        "2025-11-10",
        "2025-11-14",
        CFG,
        prices_path=prices_path,
        out_path=out,
    )
    assert result.groupby("model").size().to_dict() == {
        "naive_daily": 5 * 76,
        "naive_weekly": 5 * 76,
    }
    assert "ref_time_utc" in result.columns

    written = fio.load_forecasts(out)
    assert list(written.columns) == fio.CONTRACT_COLUMNS
    fio.validate_forecasts(written, CFG)
    assert written["issue_time_utc"].nunique() == 5

    scores = ev.run(
        "2025-11-10",
        "2025-11-14",
        CFG,
        forecasts_path=out,
        prices_path=prices_path,
        out_path=tmp_path / "eval.csv",
    )
    assert set(scores["model"]) == set(bl.MODELS)
    assert set(scores["slice_type"]) == {"overall", "block", "lead_bucket"}
    assert set(scores.loc[scores["slice_type"] == "block", "slice"]) == {"A", "B"}
    assert set(scores.loc[scores["slice_type"] == "lead_bucket", "slice"]) == set(
        tm.bucket_labels(CFG)
    )
    assert (tmp_path / "eval.csv").exists()
    weekly = scores[
        (scores["model"] == "naive_weekly") & (scores["slice_type"] == "overall")
    ].iloc[0]
    assert weekly["rmae"] == pytest.approx(1.0) and weekly["n"] == 5 * 76
    table = ev.show_issue(written, prices, "2025-11-12", CFG)
    assert len(table) == 76 and {
        "actual",
        "naive_daily.p50",
        "naive_weekly.q90",
    } <= set(table.columns)
