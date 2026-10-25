"""Load the ``forecast:`` section of config/assumptions.yaml.

Every clock number used by stage S3 (issue time, timezone, known-price lag,
windows, quantile history, lead buckets) lives in that file and is validated by
:class:`shared.schema.ForecastConfig`. Nothing is hard-coded in the modules.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from shared.schema import ForecastConfig

DEFAULT_ASSUMPTIONS_PATH = Path("config/assumptions.yaml")
WINDOWS = ("smoke", "backtest")


def load_forecast_config(path: Path | str = DEFAULT_ASSUMPTIONS_PATH) -> ForecastConfig:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if "forecast" not in data:
        raise KeyError(f"{path} has no 'forecast' section")
    return ForecastConfig.model_validate(data["forecast"])


def window_dates(cfg: ForecastConfig, window: str) -> tuple[date, date]:
    """Inclusive local issue dates for a named window ('smoke' or 'backtest')."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")
    win = cfg.smoke_window if window == "smoke" else cfg.backtest_window
    return win.start, win.end
