"""Habits model, step 4: the "same as last week" baseline (no ML). The number to beat.

    python -m v2g.habits.baseline     # scores on validation -> artefacts/habits/

For each target, copy what the car did on the SAME WEEKDAY last week. When that is
unknown (no plug-in that day, or not yet known at week start), fall back to the median
over last week, then to the training-set median (a car with no history at all).

A second variant, "last week's median", skips the same-weekday copy. It shows how much
the weekday pattern itself is worth.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .evaluate import HEADLINE, score, table
from .features import OUT as DATASET, HabitsConfig

ROOT = Path(__file__).resolve().parents[3]
ART = ROOT / "artefacts" / "habits"

# target -> (same weekday last week, last week's median)
SOURCES = {"pred_plugin_h": ("lw_plugin_h", "pw_plugin_h_median"),
           "pred_plugout_h": ("lw_plugout_h", "pw_plugout_h_median"),
           "pred_trip_kwh": ("lw_trip_kwh", "pw_trip_kwh_median")}
TARGET_OF = {"pred_plugin_h": "y_plugin_h", "pred_plugout_h": "y_plugout_h", "pred_trip_kwh": "y_trip_kwh"}


def predict_baseline(df: pd.DataFrame, train: pd.DataFrame, same_weekday: bool = True) -> pd.DataFrame:
    """Baseline predictions for df. `train` only supplies the last-resort global medians."""
    out = pd.DataFrame(index=df.index)
    if same_weekday:
        out["p_plugs_in"] = df["lw_plugs_in"].astype(float)              # 1 if it plugged in, else 0
    else:
        out["p_plugs_in"] = (df["pw_plugin_days"] >= 4).astype(float)    # median of 7 yes/no days
    for col, (lw, med) in SOURCES.items():
        global_med = train.loc[train["y_plugs_in"] == 1, TARGET_OF[col]].median()
        first = df[lw] if same_weekday else df[med]
        out[col] = first.fillna(df[med]).fillna(global_med)
    return out


def main():
    cfg = HabitsConfig()
    df = pd.read_parquet(DATASET)
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]
    res = {"baseline_same_weekday": score(val, predict_baseline(val, train), cfg),
           "baseline_week_median": score(val, predict_baseline(val, train, same_weekday=False), cfg)}
    ART.mkdir(parents=True, exist_ok=True)
    with open(ART / "baseline_val_metrics.json", "w") as f:
        json.dump(res, f, indent=2)
    with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
        print(table(res, HEADLINE).T)


if __name__ == "__main__":
    main()
