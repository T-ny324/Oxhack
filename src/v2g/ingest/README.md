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

## Exogenous forecast vintages: `elexon_ndf.py`, `elexon_windfor.py`, `neso_embedded_forecast.py`, `neso_wind_forecast.py`

Day-ahead FORECASTS of demand, wind and embedded wind/solar, keeping the vintage
of every forecast so a model issued at 10:00 can only see what was published
before 10:00. All four write the same long schema (see `vintages.py`) to
`data/processed/exog/<source>.parquet`:

| column | meaning |
|---|---|
| `source`, `variable` | e.g. `elexon_ndf`, `demand_da_mw` |
| `target_time_utc` | start of the half-hour the forecast is FOR |
| `forecast_made_utc` | when it was made or published |
| `value_mw`, `capacity_mw` | forecast and (where reported) installed capacity |

| source | variable | vintages at a 10:00 issue |
|---|---|---|
| Elexon NDF | `demand_da_mw` | every 30 min; 09:46 covers today, 08:45 local covers tomorrow 05:00 -> 05:00 |
| Elexon WINDFOR | `wind_da_mw` | 7-8 per day, hourly values written to both half-hours, 3-day horizon |
| NESO embedded archives | `embedded_wind_mw`, `embedded_solar_mw` | hourly at hh:12 UTC, 13-day horizon (kept to 48 h) |
| NESO historic DA wind | `wind_neso_da_mw` | one per day at ~08:49 UTC the day before, but stamps are unreliable from late 2025 (see module docstring); not used by the model |

```bash
make data-exog     # all four; settings under ingest.exog in config/assumptions.yaml
python -m v2g.ingest.elexon_ndf --start 2025-11-01 --end 2025-12-01
```

Rules: UTC only; settlement date + period converted with the DST-safe helper;
rows made after their target or beyond `max_lead_hours` are dropped and logged;
duplicates dropped; values left NaN; plausibility bounds per variable raise.
Never store actual outturn here.

## TODO (not part of the first ingest)

- [x] NESO/Elexon day-ahead wind, solar and demand forecast vintages (features for S3)
- [ ] Octopus Agile import and export tariffs (retail price signal for S5 and S9)
- [ ] Carbon intensity, optional (grid story in S8)
- [ ] Forecasting models (S3): simple benchmark first, then LightGBM
