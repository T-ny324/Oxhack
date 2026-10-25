# Price forecasting: models and results

**What is forecast.** The GB wholesale electricity price (Elexon market index), every half-hour, in £/MWh.
Each day at **10:00 London time** one forecast covers every half-hour from now to midnight tomorrow night
(about 76 periods): **Block A** = the rest of today, **Block B** = all of tomorrow. Lead time runs from 0 to 38 h.

**How it was tested.** One year of daily forecasts, 2025-10-01 to 2026-09-30, issued as if live (only data
known before 10:00), models refitted weekly, scored against the actual prices.

## The four models

| model | idea | uses | MAE £/MWh |
|---|---|---|---|
| naive weekly | copy the price from the same time **7 days ago** | past prices | 29.4 |
| naive daily | copy **yesterday** (Tue–Fri) or 7 days ago (Sat–Mon) | past prices | 27.9 |
| LEAR | lasso regression on recent days' prices, one model per half-hour slot | past prices | 20.2 |
| **LEAR + exog (`lear_x`)** | LEAR plus day-ahead **forecasts** of demand, wind and embedded wind/solar | past prices + NESO/Elexon forecasts | **17.2** |

The two naive models are the benchmarks any real model must beat. `lear_x` is the model the optimiser uses.

## Headline numbers (full year)

| model | MAE | Block A / B | P10–P90 coverage (target 80%) |
|---|---|---|---|
| naive weekly | 29.4 | 30.2 / 28.9 | 78% |
| naive daily | 27.9 | 27.7 / 28.0 | 78% |
| LEAR | 20.2 | 16.7 / 22.2 | 76% |
| `lear_x` | **17.2** | **15.0 / 18.5** | 76% |

## Key takeaways

- **`lear_x` is the most accurate model on every cut:** 15% lower error than LEAR, 38% lower than naive daily,
  and best in all 12 months. The improvements are statistically certain (Diebold–Mariano p < 0.001).
- **The exogenous forecasts help most for tomorrow:** Block B error falls from 22.2 to 18.5 £/MWh (−17%),
  because wind, solar and demand forecasts carry information that yesterday's prices do not.
- **Negative prices are the hardest regime, and where exog helps most:** 55 £/MWh error vs 72 for LEAR and 87 for naive.
  Spikes above 150 £/MWh stay hard for everyone (37 vs 41).
- **Errors grow with lead time:** `lear_x` is off by ~9 £/MWh for the next half-hour, ~15 by midnight, ~20 by tomorrow
  afternoon. Re-forecasting the same day cuts the error on the same periods by 22%, so the car should re-plan every morning.
- **Naive models are worst at short leads** (35 £/MWh at lead 0): they copy old prices and ignore this morning's.
- **Uncertainty bands are slightly too narrow on the upside:** 13% of actual prices exceed the 90th percentile
  (nominal 10%), giving 76% coverage instead of 80%. Prices overshoot more often than they undershoot.
- **Better forecasts turn into more money, in the same order:** on a 60 kWh commuter car with a 7.4 kW bidirectional
  charger, the optimiser saves £258/yr with `lear_x`, £208 with LEAR, £135 with naive daily, £133 with naive weekly,
  against £357 with perfect foresight. `lear_x` captures 72% of the perfect-foresight value, with fewer battery cycles
  than the naive models.

## Caveats

- Tariffs are wholesale + fixed adders (+15 import, −5 export £/MWh, placeholders), no battery-wear cost in the optimiser.
- Forecasts were refitted weekly; daily refits are possible but slower (about 2.5 h for `lear_x`).
- The NESO day-ahead wind archive was excluded (unreliable publication stamps); wind comes from Elexon's WINDFOR stream.

## Files

- `artefacts/forecasts/handoff_price_forecasts.parquet` – the forecasts (see `HANDOFF.md` for how to read them)
- `artefacts/schedules/revenue_per_vehicle.json` – £ per model from the optimiser backtest
- `notebooks/01_model_performance.ipynb`, `notebooks/02_forecast_horizons.ipynb` – the figures behind these numbers
