"""Load the ``ingest:`` section of config/assumptions.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from shared.schema import ExogIngestConfig

DEFAULT_ASSUMPTIONS_PATH = Path("config/assumptions.yaml")


def load_exog_ingest_config(
    path: Path | str = DEFAULT_ASSUMPTIONS_PATH,
) -> ExogIngestConfig:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    try:
        section = data["ingest"]["exog"]
    except (KeyError, TypeError) as exc:
        raise KeyError(f"{path} has no 'ingest.exog' section") from exc
    return ExogIngestConfig.model_validate(section)
