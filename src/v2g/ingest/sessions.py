"""S2 Ingest & clean (CONTEXT.md §9). Charging sessions → standard sessions schema.

Inputs : raw session data (Electric Nation, ACN-Data; §11)
Outputs: data/processed/sessions.parquet

TODO: reuse 8b_singleev_optimiser/v2g-hack/src/v2g/ingest/electric_nation.py (already built and tested).
"""
