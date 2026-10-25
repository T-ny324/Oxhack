"""S5 Single-EV optimise (CONTEXT.md §9). Rolling-horizon MPC (plan 38 h, act 24 h).

Inputs : price forecasts, learned habits, converter spec
Outputs: artefacts/schedules/*.parquet, revenue_per_vehicle.json

TODO: reuse 8b_singleev_optimiser/v2g-hack/src/v2g/optimise/daily.py (run_daily / run_daily_vs_reality, tested).
"""
