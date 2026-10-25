"""S3 Price forecast (CONTEXT.md §9). LightGBM quantile price forecast.

Inputs : data/processed/prices.parquet, calendar features
Outputs: artefacts/forecasts/price_quantiles.parquet (P10/P50/P90)

TODO: Tony: quantile LightGBM.
"""
