# Ingest (stage S2)

Raw public data in, clean UTC half-hourly tables out. Modules here write to
`data/processed/` and cache raw API responses under `data/raw/<source>/`.
Both directories are gitignored; re-create them with `make data`.

## Wholesale prices: `elexon_mid.py`

Source: Elexon Insights Solution API, dataset MID (Market Index Data), provider
`APXMIDP`. No API key. The module docstring records what the API actually
returns (field names, inclusive bounds, 7-day limit, units).

```bash
make data-prices                                  # 2023-01-01 -> 2026-10-01 (end exclusive)
python -m v2g.ingest.elexon_mid --start 2025-11-03 --end 2025-12-01 [--provider APXMIDP] [--refresh]
```

Output `data/processed/prices.parquet`: one row per 30-minute period over
`[start, end)`, no gaps.

| column | dtype | meaning |
|---|---|---|
| `ts_utc` | datetime64[ns, UTC] | start of the half-hour (required by the S3 -> S5 contract) |
| `price_wholesale_per_mwh` | float64 | £/MWh (required by the contract; never p/kWh) |
| `volume_mwh` | float64 | traded volume in the period |
| `provider` | str | `APXMIDP` |
| `is_missing` | bool | True where price is NaN (gap, or zero/null volume) |

Cleaning rules: UTC only; complete 30-min grid with gaps left as NaN and
`is_missing=True`; zero or null volume gives NaN price; duplicate timestamps
dropped keeping the last; no forward-fill or interpolation (that belongs to S3).

Raw chunks are cached at `data/raw/elexon_mid/<provider>_<from>_<to>.json` and
reused on re-runs. Pass `--refresh` to re-download.

## TODO (not part of the first ingest)

- [ ] NESO day-ahead wind, solar and demand forecasts (features for S3)
- [ ] Octopus Agile import and export tariffs (retail price signal for S5 and S9)
- [ ] Carbon intensity, optional (grid story in S8)
- [ ] Forecasting models (S3): simple benchmark first, then LightGBM
