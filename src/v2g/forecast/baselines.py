"""S3 Price forecast (CONTEXT.md §9). Naive and seasonal-naive price forecasts (ladder step 1).

Inputs : data/processed/prices.parquet
Outputs: artefacts/forecasts/price_quantiles.parquet

TODO: Tony: seasonal-naive baseline + evaluation hook.
"""
