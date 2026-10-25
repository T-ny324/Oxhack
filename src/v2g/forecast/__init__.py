"""Stage S3: price forecasts following the S3 -> S5 contract.

Output: ``artefacts/forecasts/price_quantiles.parquet`` (see :mod:`v2g.forecast.io`).
Ladder: naive baselines (:mod:`v2g.forecast.baselines`) -> LEAR -> LightGBM.
"""
