# Price forecasts for the optimiser (stage S3 -> S5 handoff)

File: `artefacts/forecasts/handoff_price_forecasts.parquet`
Contract columns only: `model, series, issue_time_utc, target_time_utc, p50, q10, q90`.
Models: `lear_x`, `lear`, `naive_daily`, `naive_weekly`. Series: `elexon_mid`.
Backtest window: issues 2025-10-01 to 2026-09-30 (365 daily issues per model,
27,740 rows each), weekly recalibration.

## Which model to use

Use `lear_x` (LEAR with exogenous day-ahead forecasts of demand, wind and
embedded wind/solar as features). The others are kept for benchmarking only.

## How to consume it

- One forecast per day: pick a single `issue_time_utc` (the 10:00 London issue
  of the solve day; 09:00Z in BST, 10:00Z in GMT) and take all rows for that
  issue and `model == "lear_x"`. That gives ~76 targets: every half-hour from
  the issue to the end of the NEXT local day (74 or 78 on clock-change days).
- Use `p50` as the price path. `q10`/`q90` are the 10th/90th percentile bands
  (empirical, about 76% coverage on the backtest against an 80% target).
- NEVER deduplicate by `target_time_utc` alone: consecutive issues overlap (the
  same half-hour appears in two issues, as today's Block A and yesterday's
  Block B). The unique key is `(model, series, issue_time_utc, target_time_utc)`.
- Units are £/MWh (never p/kWh). Timestamps are UTC, tz-aware, and mark the
  START of the half-hour settlement period.

## Accuracy (backtest, £/MWh)

| model | MAE | RMSE | rMAE vs naive_weekly | pinball | coverage |
|---|---|---|---|---|---|
| lear_x | 17.22 | 25.80 | 0.586 | 5.81 | 76.4% |
| lear | 20.16 | 30.40 | 0.686 | 6.90 | 76.1% |
| naive_daily | 27.88 | 43.94 | 0.949 | 10.17 | 78.4% |
| naive_weekly | 29.40 | 45.44 | 1.000 | 10.59 | 78.1% |

lear_x by block: A (rest of today) MAE 15.03, B (tomorrow) 18.51. By lead:
0-6 h 14.65, 6-12 h 15.79, 12-24 h 16.73, 24-40 h 19.37. Diebold-Mariano:
lear_x beats lear by 2.94 £/MWh per issue (stat 9.98, p < 1e-15) and
naive_daily by 10.67 (stat 14.87).

## Notes

- Every exogenous input is a forecast with a publish time; the model only sees
  forecasts published before the issue. A perturbation test confirms no
  leakage from later vintages or later prices.
- Clipping of exogenous z-scores to the training range was added after the
  first backtest exposed an extrapolation failure: 23 dawn/dusk forecasts in
  Feb-Mar 2026 blew up through the asinh back-transform (solar at those slots
  had been zero through a winter-only training window). With clipping every
  value in this file lies within the plausibility guard of -500..5000 £/MWh,
  which `validate_forecasts` now enforces on every write.
- Recalibration was weekly. Daily recalibration (about 2.5 h for lear_x on 8
  cores) has not been run.
