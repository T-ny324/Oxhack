PYTHON ?= python3

.PHONY: install format lint test data data-prices forecast-naive forecast-naive-smoke forecast-lear forecast-lear-smoke all

install:
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m black src tests shared dashboard

lint:
	$(PYTHON) -m ruff check src tests shared dashboard

test:
	$(PYTHON) -m pytest tests -q

# --- Data ingest (stage S2). Outputs go to data/processed/, raw cache to data/raw/.
PRICES_START    ?= 2023-01-01T00:00Z
PRICES_END      ?= 2026-10-01T00:00Z
PRICES_PROVIDER ?= APXMIDP

data-prices:
	$(PYTHON) -m v2g.ingest.elexon_mid --start $(PRICES_START) --end $(PRICES_END) --provider $(PRICES_PROVIDER)

data: data-prices

# --- Forecasts (stage S3). Contract parquet: artefacts/forecasts/price_quantiles.parquet
forecast-naive:
	$(PYTHON) -m v2g.forecast.baselines --window backtest
	$(PYTHON) -m v2g.forecast.evaluate --window backtest

forecast-lear:
	$(PYTHON) -m v2g.forecast.lear --window backtest
	$(PYTHON) -m v2g.forecast.evaluate --window backtest --out artefacts/forecasts/eval_lear.csv

forecast-lear-smoke:
	$(PYTHON) -m v2g.forecast.lear --window smoke --out artefacts/forecasts/price_quantiles_smoke.parquet --diagnostics artefacts/forecasts/lear_diagnostics_smoke.parquet --top-features artefacts/forecasts/lear_top_features_smoke.csv
	$(PYTHON) -m v2g.forecast.evaluate --window smoke --forecasts artefacts/forecasts/price_quantiles_smoke.parquet --out artefacts/forecasts/eval_lear_smoke.csv

forecast-naive-smoke:
	$(PYTHON) -m v2g.forecast.baselines --window smoke --out artefacts/forecasts/price_quantiles_smoke.parquet
	$(PYTHON) -m v2g.forecast.evaluate --window smoke --forecasts artefacts/forecasts/price_quantiles_smoke.parquet --out artefacts/forecasts/eval_naive_smoke.csv

all: test
