"""One week of an executed schedule: forecast vs actual buy price, charge/discharge bars, SoC."""
from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def plot_week(schedule: pd.DataFrame, params, tz: str, start_local: str, out_path: Path | str, title: str) -> Path:
    s = schedule.copy()
    s["t"] = s["ts_utc"].dt.tz_convert(tz)
    start = pd.Timestamp(start_local, tz=tz)
    s = s[(s["t"] >= start) & (s["t"] < start + pd.Timedelta(days=7))]
    t = s["t"].dt.tz_localize(None)
    fig, axes = plt.subplots(3, 1, figsize=(13, 8.5), sharex=True, gridspec_kw={"height_ratios": [1.2, 1, 0.8]})
    ax = axes[0]
    ax.step(t, s["p_buy_actual"], where="post", color="#1F2328", lw=1.6, label="buy price, actual")
    ax.step(t, s["p_buy_forecast"], where="post", color="#D9731A", lw=1.4, label="buy price, forecast used by the plan")
    ax.set_ylabel("£/MWh")
    ax.set_title(title, loc="left")
    ax.legend(loc="upper right", frameon=False, ncol=2)
    ax = axes[1]
    away = ~s["plugged"].to_numpy()
    ax.fill_between(t, 0, 1, where=away, step="post", color="#eeeeee", transform=ax.get_xaxis_transform(), label="car away")
    p = s["p_signed_kw"].to_numpy()
    ax.bar(t, np.where(p > 0, p, 0), width=1 / 48, align="edge", color="#4c9a6a", label="charge kW (+)")
    ax.bar(t, np.where(p < 0, p, 0), width=1 / 48, align="edge", color="#c8553d", label="discharge kW (−)")
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_ylabel("kW (+ = charging)")
    ax.set_ylim(-params.p_dis_max_kw * 1.15, params.p_ch_max_kw * 1.15)
    ax.legend(loc="upper right", frameon=False, ncol=3)
    ax = axes[2]
    ax.plot(t, s["soc"] * 100, color="#1F4E99", lw=1.6)
    ax.axhline(params.soc_min * 100, color="#999", lw=0.8, ls="--")
    ax.axhline(params.soc_max * 100, color="#999", lw=0.8, ls="--")
    ax.fill_between(t, 0, 1, where=away, step="post", color="#eeeeee", transform=ax.get_xaxis_transform())
    ax.set_ylabel("SoC %")
    ax.set_ylim(0, 100)
    axes[-1].xaxis.set_major_locator(mdates.DayLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b"))
    for a in axes:
        a.spines[["top", "right"]].set_visible(False)
        a.grid(axis="y", color="#e6e6e6", lw=0.6)
    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
