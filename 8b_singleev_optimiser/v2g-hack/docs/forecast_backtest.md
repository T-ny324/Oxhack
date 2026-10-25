# Rolling backtest on the S3 price forecasts (decision value)

`scripts/forecast_backtest.py` (from the repository root: `make optimise-backtest`,
`make optimise-backtest-smoke`) runs the optimiser on Tony's forecasts exactly as the
product would and reports how much each forecast model is worth.

**Loop, per issue (10:00 Europe/London), identical for every model:** take that issue's
~76 forecast periods (`price_inputs.forecast_prices`: selected by issue, never by target
time alone, because consecutive issues overlap), map to tariffs
(`p_buy = wholesale + import_adder`, `p_sell = wholesale + export_adder`, £/MWh), solve
`lp.solve_window` from the car's current SoC with the profile's availability and the
learned-habits floor (true trips, so only the price forecast differs between models),
keep the decisions up to the next issue (46/48/50 periods), settle them on ACTUAL prices
from `data/processed/prices.parquet`, carry the SoC over. `perfect_foresight` plans on the
actuals; the unmanaged charger is the floor of the comparison.

**Modules** (`src/v2g/optimise/`): `price_inputs.py` (forecast / actual / perfect-foresight
prices on the issue's target index), `availability.py` (config commuter profile -> sessions
table for `daily.trace_from_sessions`), `rolling_backtest.py` (the loop, settlement, physics
checks), `decision_value.py` (per-model totals, decision value, outputs), `schedule_plot.py`,
`bridge.py` (loads this package under an alias from the root environment, because both
trees define `v2g` and `shared`).

**Config:** `forecast_backtest` in `config/assumptions.yaml` (paths relative to the repo
root, models, windows, commuter profile, floor reserve). Vehicle and tariff come from the
existing `battery` and `tariff` sections; limits and efficiencies from the root
`config/converter_spec.yaml`.

**Outputs:** `artefacts/schedules/<model>.parquet` (per executed period: powers with
+ = charging, SoC at period start, forecast and actual buy/sell prices, revenue) and
`artefacts/schedules/revenue_per_vehicle.json` (net £/yr, saving vs unmanaged, % of the
perfect-foresight saving, decision value, energy and cycles, excluded periods, physics
checks). Periods whose actual price is missing are executed but excluded from revenue and
counted. A missing forecast issue stops the run.

**Tests:** `tests/test_optimiser_forecast_backtest.py` in the repository root (both
packages are only importable together from there).
