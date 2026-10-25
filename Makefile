# ExtraEnergy pipeline (CONTEXT.md §9). Run from the repo root after creating .venv (see README).
#   make app    run the Streamlit app locally (http://localhost:8501)
#   make test   run the §12 checks
#   make all    every pipeline stage, then the tests
# Stage targets are stubs until their modules land (each prints its TODO).
PYTHON ?= .venv/bin/python

.PHONY: install app test data forecast degradation optimise econ sweep all

install:            ## app dependencies + test tools into .venv
	$(PYTHON) -m pip install -r requirements.txt pytest

app:                ## S11 App & headline
	$(PYTHON) -m streamlit run dashboard/app.py

test:               ## §12 consistency checks
	$(PYTHON) -m pytest tests -q

data:               ## S2 Ingest & clean
	@echo "TODO(S2): python -m v2g.ingest.prices / sessions -> data/processed/"

forecast: data      ## S3 Price forecast (Tony)
	@echo "TODO(S3): python -m v2g.forecast.gbm -> artefacts/forecasts/price_quantiles.parquet"

degradation:        ## S4 Degradation
	@echo "TODO(S4): python -m v2g.degradation.linear -> artefacts/degradation/c_deg.json"

optimise: forecast degradation   ## S5 Single-EV optimise
	@echo "TODO(S5): python -m v2g.optimise.mpc -> artefacts/schedules/, revenue_per_vehicle.json"

econ: optimise      ## S9 Economics
	@echo "TODO(S9): python -m v2g.economics.payback -> artefacts/econ/payback.json"

sweep: econ         ## S10 Sensitivity & design targets
	@echo "TODO(S10): python -m v2g.sensitivity.design_sweep -> config/design_targets.yaml"

all: sweep test
