"""Unit conversions and conventions (§6.3). Convert at ingest only.

Sign convention: + = grid -> vehicle (charging), - = vehicle -> grid.
Prices: currency/MWh internally. Time: UTC internally.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def p_per_kwh_to_per_mwh(x):
    """1 p/kWh = 10 £/MWh."""
    return np.asarray(x) * 10.0


def per_mwh_to_per_kwh(x):
    return np.asarray(x) / 1000.0


def signed_power(p_ch_kw, p_dis_kw):
    """Combine non-negative charge/discharge into the signed convention (+ = charging)."""
    return np.asarray(p_ch_kw) - np.asarray(p_dis_kw)


def support_gw(p_signed_kw):
    """Fleet 'support' number: discharge reported as a positive GW. The ONE place this flip happens."""
    return np.clip(-np.asarray(p_signed_kw), 0, None) / 1e6


def utc_index(start, end, dt_h: float) -> pd.DatetimeIndex:
    """Gap-free UTC index [start, end) at dt_h resolution. DST-safe because it is UTC."""
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                        freq=pd.Timedelta(hours=dt_h), inclusive="left")
    return idx


def slot_of_week(index: pd.DatetimeIndex, dt_h: float, tz: str) -> np.ndarray:
    """Local-time slot-of-week (0 .. 7*24/dt_h - 1). Habits live in local time, so a DST
    change shifts the UTC slot of a 07:30 departure; this keeps it on the same local slot."""
    local = index.tz_convert(tz)
    per_day = int(round(24 / dt_h))
    minutes = local.hour * 60 + local.minute
    return (local.dayofweek * per_day + (minutes // int(dt_h * 60))).to_numpy()
