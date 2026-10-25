# Price forecast → optimiser contract (S3 → S5)

Status: **v0.1 agreed**. Answers Tony's questions for 8.A.

## 1. Horizon and cadence
- **v0: one solve per day** (deterministic MILP), not MPC.
- **Issue time:** 10:00 Europe/London every day (agreed).
- **Targets per issue:** every half-hour from `issue_time` up to the end of the **next local day**
  (≈ 38 h).
- **Plan 38 h, act 24 h:** the optimiser plans the whole window but executes only
  10:00 today → 10:00 tomorrow, then re-plans from the next issue with the car's real SoC.
  The extra look-ahead stops the plan draining the battery just before the window ends.
- **Later (MPC stretch):** same file, more issues per day (e.g. every 30 min). Nothing in the schema changes;
  only the number of `issue_time_utc` values grows.

## 2. Point or probabilistic
- **v0: P50 only** (column `p50`). Deterministic optimiser.
- **Optional now, used later:** `q10`, `q90` (for a robust-optimisation stretch and for showing bands on the dashboard).
- **Scenarios / sampled paths: not needed.** If we ever do a stochastic LP we'll ask for
  `scenario_id` paths separately.

## 3. Buy vs sell
- **Forecast ONE series: wholesale.** Elexon Market Index Price (MID): free and half-hourly (agreed).
  Name the series in the `series` column.
- **We map wholesale → tariffs** on our side, from config:
  `p_buy = wholesale + import_adder`, `p_sell = wholesale + export_adder` (£/MWh).
  So no Agile or export-tariff forecasts are needed from you.

## 4. Time convention
- **Timestamp = START of the half-hour.**
- **UTC, timezone-aware** (`datetime64[ns, UTC]`).
- **No settlement date/period needed.** If you hold them, convert to UTC at ingest; extra columns are ignored.
- **DST:** handled by using UTC. Clock-change days simply have 46 or 50 local periods. Please make sure
  none are dropped or duplicated.

## 5. Units
- **£/MWh, always.** You never convert. We do any £/kWh maths inside the optimiser.

## 6. Format and versioning
One long parquet: `artefacts/forecasts/price_quantiles.parquet`

| column | type | notes |
|---|---|---|
| `model` | str | e.g. `naive`, `lear`, `lgbm`. One file, many models. We switch models for the decision-value comparison. |
| `series` | str | e.g. `elexon_mid` |
| `issue_time_utc` | datetime UTC | when the forecast was made; must use only data available before this |
| `target_time_utc` | datetime UTC | start of the half-hour being forecast |
| `p50` | float | £/MWh, required |
| `q10`, `q90` | float | £/MWh, optional |

- Unique key: (`model`, `series`, `issue_time_utc`, `target_time_utc`).
- Actual prices come from `data/processed/prices.parquet` (`ts_utc`, `price_wholesale_per_mwh`), not from this file.
- We write the loader on our side; you only produce the file.

## 7. Backtest span
- **2025-10-01 → 2026-09-30** (UTC, agreed): the most recent full 12 months, so £/yr includes all seasons.
- **Smoke test first:** 4 weeks, **2025-11-03 → 2025-11-30**.
- Forecasts must be **out-of-sample, issued as if live**. Every row is built using only data
  before its `issue_time_utc`. Refit or roll as you like, but never train on the backtest period ahead of the issue time.
- Training data: anything before the issue time (e.g. 2023 onwards).

## What the optimiser hands back (FYI)
`revenue_per_vehicle.json` includes a revenue per `model` value, giving the **decision value**
of each forecast: £ earned with that forecast vs. with perfect foresight.
