# Oxhack V2G Hackathon Repo

This repository is set up for the Oxhack V2G hackathon project described in `v2g_hackathon_context.md`.

## Project thesis

We design a bidirectional EV charger and then show, with data, what that exact design is worth to the owner who buys it and to the grid it plugs into, today and in 10 years.

The repo is structured around the logic chain:

1. Hardware design → converter properties
2. Single vehicle → optimal buy/sell schedule
3. Fleet → aggregated flexibility and adoption
4. Grid → peak coverage, reliability and renewables absorption
5. 10-year view → adoption forecast and system value
6. Feedback loop → design targets back to hardware

## Repository layout

- `config/` - shared assumptions, converter spec, and design targets
- `shared/` - schema and unit conversion helpers
- `src/v2g/` - pipeline stages for ingest, forecast, optimisation, fleet, grid, economics and sensitivity
- `dashboard/` - interactive Streamlit demo app
- `hardware/` - converter design notes, BOM and low-voltage prototype ideas
- `tests/` - consistency checks and schema validation
- `data/` and `artefacts/` - generated inputs and outputs

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
make all
make data-prices   # GB wholesale prices (Elexon MID) -> data/processed/prices.parquet
```

Data ingest lives in `src/v2g/ingest/` (see its README). Raw API responses are
cached under `data/raw/` and cleaned tables written to `data/processed/`; both
are gitignored.

## Core contract

The shared interface starts with `config/converter_spec.yaml`. The hardware team owns the converter specification, and the data side consumes it to compute economics, fleet flexibility, and grid impact.

## Key files

- `config/converter_spec.yaml` - hardware specification contract
- `config/design_targets.yaml` - feedback loop from data/model output
- `shared/schema.py` - Pydantic models for validation
- `dashboard/app.py` - demo dashboard
- `v2g_hackathon_context.md` - full concept and delivery plan

## Hackathon objective

The aim is not just to build a model, but to produce a credible end-to-end value chain:

- a realistic converter spec
- a schedule optimiser for one EV
- a fleet-level flexibility model
- a 10-year adoption and grid impact analysis
- a headline result tying hardware, economics and grid value together

## Recommended team workflow

- Hardware team: refine the converter efficiency curve and cost assumptions
- Data team: forecast prices, build the optimizer, generate fleet metrics
- Integration lead: keep the pipeline contract stable and end-to-end tests passing

## License

MIT
