"""S0 Units and conventions (CONTEXT.md §6.3). Convert at ingest/display only.

Prices are £/MWh internally; 1 p/kWh = 10 £/MWh. The owner view displays p/kWh, converted
here and nowhere else. Power kW, energy kWh, SoC and efficiency are fractions 0–1.
Sign convention: + = grid → vehicle (charging), − = vehicle → grid (exporting).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def p_per_kwh_to_per_mwh(x):
    """p/kWh → £/MWh (1 p/kWh = 10 £/MWh)."""
    return np.asarray(x, dtype=float) * 10.0


def per_mwh_to_p_per_kwh(x):
    """£/MWh → p/kWh, for display in the owner view."""
    return np.asarray(x, dtype=float) / 10.0


def per_mwh_to_per_kwh(x):
    """£/MWh → £/kWh (inside the optimiser, where energy is in kWh)."""
    return np.asarray(x, dtype=float) / 1000.0


def signed_power(p_ch_kw, p_dis_kw):
    """Combine non-negative charge / discharge powers into the signed convention:
    + = charging (grid → vehicle), − = exporting (vehicle → grid)."""
    return np.asarray(p_ch_kw, dtype=float) - np.asarray(p_dis_kw, dtype=float)


def utc_index(start, end, dt_h: float) -> pd.DatetimeIndex:
    """Gap-free UTC index [start, end) at dt_h resolution. DST-safe because it is UTC."""
    return pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                         freq=pd.Timedelta(hours=dt_h), inclusive="left")
