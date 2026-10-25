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
not revisited after the backtest.

Validation 2025-04-01..2025-09-30, weekly recalibration, 13,893 scored rows per model:

| model | criterion | MAE | RMSE | pinball | coverage | chosen |
|---|---|---|---|---|---|---|
| lear | aic | 17.44 | 26.47 | 6.22 | 77.5% | yes |
| lear | bic | 17.61 | 27.08 | 6.26 | 77.4% | |
| lear_x | aic | 14.50 | 21.73 | 5.12 | 78.3% | yes |
| lear_x | bic | 14.52 | 21.81 | 5.14 | 77.6% | |

Frozen: `lambda_selection_by_model: {lear: aic, lear_x: aic}`.

## Backtest results (2025-10-01..2026-09-30, weekly recalibration)

27,678 scored rows per model (124 targets without actuals excluded). rMAE is
relative to `naive_weekly`; coverage is the share of actuals inside [q10, q90].

| model | slice | MAE | RMSE | rMAE | pinball | coverage |
|---|---|---|---|---|---|---|
| lear_x | overall | 17.22 | 25.80 | 0.586 | 5.81 | 76.4% |
| lear_x | block A / B | 15.03 / 18.51 | 23.62 / 27.00 | 0.498 / 0.640 | 5.05 / 6.25 | 77.6% / 75.6% |
| lear_x | lead 0-6 / 6-12 / 12-24 / 24-40 h | 14.65 / 15.79 / 16.73 / 19.37 | | 0.395 / 0.608 / 0.637 / 0.641 | | |
| lear | overall | 20.16 | 30.40 | 0.686 | 6.90 | 76.1% |
| lear | block A / B | 16.71 / 22.17 | 26.20 / 32.60 | 0.554 / 0.766 | 5.68 / 7.62 | 77.1% / 75.5% |
| lear | lead 0-6 / 6-12 / 12-24 / 24-40 h | 15.70 / 17.62 / 19.94 / 23.35 | | 0.423 / 0.679 / 0.759 / 0.773 | | |
| naive_daily | overall | 27.88 | 43.94 | 0.949 | 10.17 | 78.4% |
| naive_weekly | overall | 29.40 | 45.44 | 1.000 | 10.59 | 78.1% |

Diebold-Mariano (365 issues, one-sided, loss differential per issue in £/MWh):
lear_x beats lear by 2.94 (stat 9.98), naive_daily by 10.67 (14.87) and
naive_weekly by 12.17 (16.45); lear beats naive_daily by 7.72 (11.58). All
p-values are below 1e-15.

Diagnostics for lear_x: 42% (block A) and 43% (block B) of selected
coefficients are exogenous. Embedded solar and residual load are the most used
exogenous groups (3.5 and 2.9 coefficients per model in block A); the latest
known price (`today_s18`) remains the single most selected feature (76% of
block A fits). Exogenous z-scores are clipped to the training range at
prediction time; without that, 23 dawn/dusk forecasts in Feb-Mar 2026
exploded through the asinh back-transform.

Runtimes on 8 cores, weekly recalibration: lear 7 min, lear_x 21 min (one
recalibration 7 s and 21 s). Daily recalibration projects to about 50 min
and 2.5 h respectively.

Ladder: naive -> LEAR -> LightGBM (TODO) -> exogenous NESO features (TODO, see
the `exog` hook in `lear_features.build_features`).
