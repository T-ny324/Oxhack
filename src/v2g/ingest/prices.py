"""S2 Ingest & clean (CONTEXT.md §9). Wholesale / tariff prices → standard schema.

Inputs : raw price APIs/CSVs (Elexon, Octopus Agile; §11)
Outputs: data/processed/prices.parquet (ts_utc, price_per_mwh, source)

TODO: load prices, convert p/kWh → £/MWh with shared.units, UTC index, DST-day checks (§12).
"""
