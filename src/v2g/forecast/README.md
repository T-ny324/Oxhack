# Forecast (stage S3)

Day-ahead-style price forecasts following the S3 -> S5 contract. Every model
writes to the same long parquet, `artefacts/forecasts/price_quantiles.parquet`
(`model, series, issue_time_utc, target_time_utc, p50, q10, q90`), through
`io.write_forecasts`, which validates the contract and upserts by model.

Issues are daily at 10:00 Europe/London; each covers every half-hour from the
issue to the end of the next local day (76 periods, 74/78 on clock-change
days). Only prices with a start time up to the known cutoff (09:00 local) may
be used. All settings live under `forecast:` in `config/assumptions.yaml`.

| module | role |
|---|---|
| `timing.py` | issue/target times, known cutoff, lead buckets, block A/B, DST-safe local->UTC |
| `io.py` | contract validator, upsert writer, loaders for forecasts and actuals |
| `quantiles.py` | empirical residual q10/q90 by lead bucket, shared by all models |
| `baselines.py` | `naive_weekly`, `naive_daily` (Lago et al. 2021 Naive) |
| `lear_data.py`, `lear_features.py`, `lear.py` | LEAR: lasso per (block, slot) on recent days, asinh transform, window ensemble |
| `exog.py` | as-of selection of exogenous forecast vintages (latest published before the issue), fixed slot layout, residual load |
| `validate.py` | picks the lasso criterion per model on the validation window, never on the backtest |
| `evaluate.py` | MAE/RMSE/rMAE/pinball/coverage by block and lead bucket, Diebold-Mariano vs the naive models |

```bash
make forecast-naive-smoke   # naive baselines, Nov 2025, separate smoke files
make forecast-naive         # naive baselines, full backtest
make forecast-lear-smoke    # LEAR, Nov 2025 (daily recalibration, ~10 min on 8 cores)
make forecast-lear          # LEAR, full backtest (daily recalibration, ~50 min; add --recalibrate-every 7 for ~8 min)
make forecast-validate      # lear and lear_x x {aic, bic} on the validation window -> validation_summary.csv
make forecast-lear-x        # LEAR + exogenous forecasts (lear_x), backtest, weekly recalibration
python -m v2g.forecast.evaluate --window backtest --show-issue 2025-11-10
```

## Exogenous features (`lear_x`)

`lear_x` is the same `lear.py` code path with exogenous forecast vintages
switched on through `forecast.exog.enabled_for_models`. Features come from
`data/processed/exog/` (see the ingest README): for each configured variable
the latest forecast published before the issue, laid out as local day d and
d+1 x 48 slots (`wind_da_d1_s34`), plus the derived `residual_load`. Training
rows with a missing exogenous value are dropped; at prediction time a missing
value takes the training mean. Nothing published after the issue is ever
visible, which `tests/test_forecast_exog.py` checks by perturbation.

## Criterion selection (validation, not test)

`make forecast-validate` runs `lear` and `lear_x` with AIC and BIC over
`forecast.validation_window` (2025-04-01 to 2025-09-30, before the backtest),
scores them and writes `artefacts/forecasts/validation_summary.csv`. The
winner per model is frozen in `forecast.lear.lambda_selection_by_model` and
not revisited after the backtest. Results are recorded below once run.

Ladder: naive -> LEAR -> LightGBM (TODO) -> exogenous NESO features (TODO, see
the `exog` hook in `lear_features.build_features`).
