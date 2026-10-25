"""Habits model, step 5: classifiers + quantile regressors with an asymmetric loss.

    python -m v2g.habits.model     # fit, compare on validation, save to artefacts/habits/

Errors are not symmetric: the dangerous ones strand the driver. Training with the
pinball (quantile) loss at quantile tau = w / (w + 1) makes an under-prediction cost w
times an over-prediction, so the model learns a deliberately cautious prediction:
    trip energy  -> P90 (too LOW is dangerous)
    departure    -> P10 (too LATE is dangerous)
    plug-in      -> P90 (too EARLY is dangerous)
tau comes from HabitsConfig.asym_weight_w (default w = 9).

Ladder (each fitted on the training split, compared on validation):
  plugs in today?  logistic regression | LightGBM classifier
  times / energy   linear quantile regression | LightGBM objective="quantile"
The linear quantile model is fitted on a 10k-row sample: its solver scales badly
(20k rows = ~20 s per target on an M1); 10k rows is plenty for ~30 coefficients.
LightGBM uses every row and handles missing features natively.

The best classifier and the best model per target (by validation Brier score / pinball
loss) are refitted on train + val and saved; the test split stays untouched for step 6.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, QuantileRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .baseline import predict_baseline
from .evaluate import HEADLINE, score, table
from .features import FEATURES, OUT as DATASET, HabitsConfig

ROOT = Path(__file__).resolve().parents[3]
ART = ROOT / "artefacts" / "habits"
LINEAR_SAMPLE = 10_000
LGB_PARAMS = dict(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=50,
                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
REG_TARGETS = {"y_plugin_h": "pred_plugin_h", "y_plugout_h": "pred_plugout_h", "y_trip_kwh": "pred_trip_kwh"}


def _linear_prep():
    """Linear models can't take NaN or treat weekday as a category: impute with the training
    median + a 'was missing' flag (missing is informative: e.g. no plug-in last week), scale,
    and one-hot the weekday and month."""
    cat = ["dow", "month"]
    num = [f for f in FEATURES if f not in cat]
    return ColumnTransformer([
        ("num", make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler()), num),
        ("cat", OneHotEncoder(handle_unknown="ignore"), cat),
    ])


def fit_classifier(kind: str, train: pd.DataFrame, seed: int):
    X, y = train[FEATURES], train["y_plugs_in"]
    if kind == "linear":
        return make_pipeline(_linear_prep(), LogisticRegression(max_iter=1000)).fit(X, y)
    return lgb.LGBMClassifier(**LGB_PARAMS, random_state=seed).fit(X, y)


def fit_quantile(kind: str, train: pd.DataFrame, target: str, tau: float, seed: int):
    d = train[(train["y_plugs_in"] == 1) & train[target].notna()]   # targets exist on plug-in days
    if kind == "linear":
        d = d.sample(min(LINEAR_SAMPLE, len(d)), random_state=seed)
        m = make_pipeline(_linear_prep(), QuantileRegressor(quantile=tau, alpha=0.0, solver="highs"))
        return m.fit(d[FEATURES], d[target])
    return lgb.LGBMRegressor(objective="quantile", alpha=tau, **LGB_PARAMS,
                             random_state=seed).fit(d[FEATURES], d[target])


class HabitsModel:
    """A classifier + one quantile regressor per target -> the standard prediction frame."""

    def __init__(self, classifier, regressors: dict, names: dict, cfg: HabitsConfig):
        self.classifier, self.regressors, self.names, self.cfg = classifier, regressors, names, cfg

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[FEATURES]
        out = pd.DataFrame(index=df.index)
        out["p_plugs_in"] = self.classifier.predict_proba(X)[:, 1]
        for tgt, col in REG_TARGETS.items():
            out[col] = self.regressors[tgt].predict(X)
        out["pred_trip_kwh"] = out["pred_trip_kwh"].clip(lower=0)    # energy can't be negative
        return out


def fit_model(train: pd.DataFrame, cfg: HabitsConfig, kinds: dict) -> tuple[HabitsModel, dict]:
    """kinds: {"classifier": "linear"|"lgbm", target: "linear"|"lgbm", ...}. Returns the model
    and the fit time of each part (seconds)."""
    secs = {}
    t = time.time()
    clf = fit_classifier(kinds["classifier"], train, cfg.seed)
    secs["classifier"] = time.time() - t
    regs = {}
    for tgt, tau in cfg.quantiles.items():
        t = time.time()
        regs[tgt] = fit_quantile(kinds[tgt], train, tgt, tau, cfg.seed)
        secs[tgt] = time.time() - t
    return HabitsModel(clf, regs, kinds, cfg), secs


def main():
    cfg = HabitsConfig()
    df = pd.read_parquet(DATASET)
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]

    # ---- 1. fit both rungs of the ladder on train, score everything on validation
    results, secs, ladder = {}, {}, {}
    results["baseline_same_weekday"] = score(val, predict_baseline(val, train), cfg)
    for kind in ("linear", "lgbm"):
        kinds = {"classifier": kind, **{t: kind for t in REG_TARGETS}}
        ladder[kind], secs[kind] = fit_model(train, cfg, kinds)
        results[kind] = score(val, ladder[kind].predict(val), cfg)
        print(f"fitted {kind}: " + ", ".join(f"{k} {v:.1f}s" for k, v in secs[kind].items()))

    # ---- 2. choose per component on validation: Brier for the classifier, pinball per target
    pick = {"classifier": min(ladder, key=lambda k: results[k]["plug_brier"])}
    for tgt in REG_TARGETS:
        pick[tgt] = min(ladder, key=lambda k: results[k][f"pinball_{tgt[2:]}"])
    chosen = HabitsModel(ladder[pick["classifier"]].classifier,
                         {t: ladder[pick[t]].regressors[t] for t in REG_TARGETS}, pick, cfg)
    results["chosen"] = score(val, chosen.predict(val), cfg)

    # ---- 3. refit the chosen recipe on train + val (test stays untouched) and save
    final, _ = fit_model(df[df["split"] != "test"], cfg, pick)
    ART.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": final, "features": FEATURES, "config": asdict(cfg), "picked": pick},
                ART / "habits_model.joblib")
    with open(ART / "config.json", "w") as f:
        json.dump({"config": asdict(cfg), "quantiles": cfg.quantiles, "picked": pick,
                   "lgb_params": LGB_PARAMS, "linear_sample": LINEAR_SAMPLE,
                   "fit_seconds": secs, "features": FEATURES}, f, indent=2)
    with open(ART / "val_metrics.json", "w") as f:
        json.dump(results, f, indent=2)
    # LightGBM feature importance (gain), for the write-up
    imp = {t: pd.Series(ladder["lgbm"].regressors[t].booster_.feature_importance("gain"), index=FEATURES)
           .sort_values(ascending=False).head(5).round(0).to_dict() for t in REG_TARGETS}
    imp["classifier"] = pd.Series(ladder["lgbm"].classifier.booster_.feature_importance("gain"),
                                  index=FEATURES).sort_values(ascending=False).head(5).round(0).to_dict()
    with open(ART / "lgbm_top_features.json", "w") as f:
        json.dump(imp, f, indent=2)

    print("picked:", pick)
    with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
        print(table(results, HEADLINE).T)


if __name__ == "__main__":
    # Run main() from the IMPORTED module, not this __main__ copy, so the saved model is
    # pickled as v2g.habits.model.HabitsModel and loads from anywhere.
    from v2g.habits.model import main as _main
    _main()
