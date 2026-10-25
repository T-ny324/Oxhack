"""Choose the lasso criterion per model on the VALIDATION window, never on the backtest.

Run
---
::

    python -m v2g.forecast.validate [--models lear,lear_x] [--criteria aic,bic] [--recalibrate-every 7]

For every model x criterion the LEAR pipeline is run over
``forecast.validation_window`` into a separate parquet
(``artefacts/forecasts/price_quantiles_validation.parquet``) under the model
name ``<model>__<criterion>``, scored with the shared evaluator, and the
criterion with the lower validation MAE is chosen per model. The table is
written to ``artefacts/forecasts/validation_summary.csv`` and the choices are
printed as the ``forecast.lear.lambda_selection_by_model`` snippet to freeze
in config/assumptions.yaml. Exogenous features follow
``forecast.exog.enabled_for_models`` for the base model name.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from shared.schema import ForecastConfig
from v2g.forecast import evaluate as ev
from v2g.forecast import lear
from v2g.forecast.config import (
    DEFAULT_ASSUMPTIONS_PATH,
    load_forecast_config,
    window_dates,
)
from v2g.forecast.io import DEFAULT_PRICES_PATH, load_forecasts, load_prices

log = logging.getLogger(__name__)

DEFAULT_VALIDATION_PATH = Path("artefacts/forecasts/price_quantiles_validation.parquet")
DEFAULT_SUMMARY_PATH = Path("artefacts/forecasts/validation_summary.csv")
CRITERIA = ("aic", "bic")


def candidate_name(model: str, criterion: str) -> str:
    return f"{model}__{criterion}"


def run(
    cfg: ForecastConfig,
    *,
    models: list[str],
    criteria: list[str] | None = None,
    recalibrate_every: int | None = None,
    prices_path: Path | str = DEFAULT_PRICES_PATH,
    out_path: Path | str = DEFAULT_VALIDATION_PATH,
    summary_path: Path | str | None = DEFAULT_SUMMARY_PATH,
    skip_existing: bool = False,
) -> pd.DataFrame:
    criteria = list(criteria or CRITERIA)
    start, end = window_dates(cfg, "validation")
    runtimes = {}
    existing = (
        set(load_forecasts(out_path)["model"].unique())
        if (skip_existing and Path(out_path).exists())
        else set()
    )
    for model in models:
        for criterion in criteria:
            name = candidate_name(model, criterion)
            if name in existing:
                log.info("%s already in %s, skipping", name, out_path)
                continue
            result = lear.run(
                start,
                end,
                cfg,
                prices_path=prices_path,
                out_path=out_path,
                recalibrate_every=recalibrate_every,
                lambda_selection=criterion,
                model_name=name,
                exog=model in cfg.exog.enabled_for_models,
                diagnostics_path=None,
                top_features_path=None,
                feature_groups_path=None,
            )
            runtimes[name] = result.runtime_s
            log.info("%s: %.0fs", name, result.runtime_s)
    forecasts = ev.filter_window(load_forecasts(out_path), start, end, cfg)
    scores = ev.score(forecasts, load_prices(prices_path), cfg)
    overall = scores[scores["slice_type"] == "overall"].copy()
    overall[["model", "criterion"]] = overall["model"].str.rsplit(
        "__", n=1, expand=True
    )
    overall["runtime_s"] = overall.apply(
        lambda r: runtimes.get(
            candidate_name(r["model"], r["criterion"]), float("nan")
        ),
        axis=1,
    )
    overall = overall[
        overall["model"].isin(models) & overall["criterion"].isin(criteria)
    ]
    best = overall.loc[overall.groupby("model")["mae"].idxmin(), ["model", "criterion"]]
    overall["chosen"] = overall.set_index(["model", "criterion"]).index.isin(
        list(best.itertuples(index=False, name=None))
    )
    table = (
        overall[
            [
                "model",
                "criterion",
                "n",
                "mae",
                "rmse",
                "pinball",
                "coverage",
                "runtime_s",
                "chosen",
            ]
        ]
        .sort_values(["model", "criterion"])
        .reset_index(drop=True)
    )
    if summary_path is not None:
        Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(summary_path, index=False)
    return table


def format_summary(table: pd.DataFrame, cfg: ForecastConfig) -> str:
    start, end = window_dates(cfg, "validation")
    show = table.copy()
    for col in ("mae", "rmse", "pinball"):
        show[col] = show[col].map(lambda v: f"{v:.2f}")
    show["coverage"] = show["coverage"].map(lambda v: f"{100 * v:.1f}%")
    show["runtime_s"] = show["runtime_s"].map(
        lambda v: f"{v:.0f}" if pd.notna(v) else ""
    )
    chosen = table[table["chosen"]].set_index("model")["criterion"].to_dict()
    snippet = "\n".join(f"      {m}: {c}" for m, c in sorted(chosen.items()))
    return (
        f"Validation {start}..{end} (criterion chosen by lower MAE; frozen before any backtest):\n"
        + show.to_string(index=False)
        + "\n\nFreeze in config/assumptions.yaml under forecast.lear:\n    lambda_selection_by_model:\n"
        + snippet
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="python -m v2g.forecast.validate", description=__doc__.split("\n\n")[0]
    )
    p.add_argument("--models", default="lear,lear_x")
    p.add_argument("--criteria", default=",".join(CRITERIA))
    p.add_argument("--recalibrate-every", type=int, default=7)
    p.add_argument("--prices", default=str(DEFAULT_PRICES_PATH))
    p.add_argument("--out", default=str(DEFAULT_VALIDATION_PATH))
    p.add_argument("--summary", default=str(DEFAULT_SUMMARY_PATH))
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="reuse candidates already in the validation parquet",
    )
    p.add_argument("--config", default=str(DEFAULT_ASSUMPTIONS_PATH))
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_forecast_config(args.config)
    table = run(
        cfg,
        models=[m.strip() for m in args.models.split(",")],
        criteria=[c.strip() for c in args.criteria.split(",")],
        recalibrate_every=args.recalibrate_every,
        prices_path=args.prices,
        out_path=args.out,
        summary_path=args.summary,
        skip_existing=args.skip_existing,
    )
    print(format_summary(table, cfg))
    print(f"written to {args.summary}")


if __name__ == "__main__":
    main()
