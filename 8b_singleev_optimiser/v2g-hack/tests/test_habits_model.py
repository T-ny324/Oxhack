"""Habits model (step 5): loss, quantile mapping, shortfall definition, model output shape."""
import numpy as np
import pandas as pd
import pytest

from v2g.habits.evaluate import pinball, score
from v2g.habits.features import FEATURES, HabitsConfig
from v2g.habits.model import REG_TARGETS, fit_model


def test_weight_maps_to_quantiles():
    q = HabitsConfig(asym_weight_w=9).quantiles
    assert q["y_trip_kwh"] == pytest.approx(0.9)       # energy too low is dangerous
    assert q["y_plugout_h"] == pytest.approx(0.1)      # departure too late is dangerous
    assert q["y_plugin_h"] == pytest.approx(0.9)       # plug-in too early is dangerous


def test_pinball_is_asymmetric():
    # at tau = 0.9 under-predicting by 1 costs 0.9, over-predicting by 1 costs 0.1 (= w = 9)
    assert pinball(np.array([1.0]), np.array([0.0]), 0.9) == pytest.approx(0.9)
    assert pinball(np.array([0.0]), np.array([1.0]), 0.9) == pytest.approx(0.1)


def test_shortfall_counts_only_misses_beyond_tolerance():
    df = pd.DataFrame({"y_plugs_in": [1, 1, 1, 1], "y_trip_kwh": [10.0, 10.0, 10.0, 10.0],
                       "y_plugout_h": [31.0] * 4, "y_plugin_h": [18.0] * 4})
    pred = pd.DataFrame({"p_plugs_in": [0.9, 0.9, 0.9, 0.1],
                         "pred_trip_kwh": [9.5, 8.0, 10.0, 10.0],       # 0.5 kWh short: tolerated
                         "pred_plugout_h": [31.0, 31.0, 31.1, 31.5],   # 0.1 h tolerated, 0.5 h not
                         "pred_plugin_h": [18.0] * 4})
    r = score(df, pred)
    assert r["energy_short_rate"] == pytest.approx(0.25)
    assert r["early_departure_rate"] == pytest.approx(0.25)
    assert r["shortfall_rate"] == pytest.approx(0.5)


def test_model_predicts_standard_frame():
    rng = np.random.default_rng(0)
    n = 600
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=FEATURES)
    df["dow"], df["month"] = rng.integers(0, 7, n), rng.integers(1, 13, n)
    df["y_plugs_in"] = (rng.random(n) < 0.5).astype(int)
    df["y_plugin_h"], df["y_plugout_h"] = rng.normal(18, 2, n), rng.normal(31, 2, n)
    df["y_trip_kwh"] = rng.gamma(2, 4, n)
    for kind in ("linear", "lgbm"):
        m, _ = fit_model(df, HabitsConfig(), {"classifier": kind, **{t: kind for t in REG_TARGETS}})
        p = m.predict(df)
        assert list(p.columns) == ["p_plugs_in", "pred_plugin_h", "pred_plugout_h", "pred_trip_kwh"]
        assert p["p_plugs_in"].between(0, 1).all() and (p["pred_trip_kwh"] >= 0).all()
        # a P90 energy model should sit above most actuals in-sample
        assert (df["y_trip_kwh"] <= p["pred_trip_kwh"]).mean() > 0.75
