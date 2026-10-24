from __future__ import annotations

from datetime import datetime


def to_kwh_from_kw(power_kw: float, hours: float) -> float:
    return power_kw * hours


def kw_to_gw(power_kw: float) -> float:
    return power_kw / 1_000_000


def currency_per_mwh_to_p_per_kwh(price_per_mwh: float) -> float:
    """GB-style conversion: 1 p/kWh = 10 £/MWh."""
    return price_per_mwh / 10.0


def utc_to_local_display(ts: datetime, timezone_name: str) -> datetime:
    """Placeholder helper; real timezone conversion should use zoneinfo."""
    return ts
