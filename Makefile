PYTHON ?= python3

.PHONY: install format lint test data data-prices data-exog forecast-naive forecast-naive-smoke forecast-lear forecast-lear-smoke forecast-validate forecast-lear-x forecast-lear-x-smoke notebooks optimise-backtest optimise-backtest-smoke all

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

# Exogenous forecast vintages (wind, embedded wind/solar, demand) -> data/processed/exog/
data-exog:
	$(PYTHON) -m v2g.ingest.elexon_ndf
	$(PYTHON) -m v2g.ingest.elexon_windfor
	$(PYTHON) -m v2g.ingest.neso_embedded_forecast
	$(PYTHON) -m v2g.ingest.neso_wind_forecast

data: data-prices data-exog

# --- Forecasts (stage S3). Contract parquet: artefacts/forecasts/price_quantiles.parquet
forecast-naive:
	$(PYTHON) -m v2g.forecast.baselines --window backtest
	$(PYTHON) -m v2g.forecast.evaluate --window backtest

forecast-lear:
	$(PYTHON) -m v2g.forecast.lear --window backtest
	$(PYTHON) -m v2g.forecast.evaluate --window backtest --out artefacts/forecasts/eval_lear.csv

forecast-validate:
	$(PYTHON) -m v2g.forecast.validate --models lear,lear_x --criteria aic,bic --recalibrate-every 7

forecast-lear-x:
	$(PYTHON) -m v2g.forecast.lear --window backtest --model-name lear_x --recalibrate-every 7 --diagnostics artefacts/forecasts/lear_x_diagnostics.parquet --top-features artefacts/forecasts/lear_x_top_features.csv --feature-groups artefacts/forecasts/lear_x_feature_groups.csv
	$(PYTHON) -m v2g.forecast.evaluate --window backtest --out artefacts/forecasts/eval_lear.csv

forecast-lear-x-smoke:
	$(PYTHON) -m v2g.forecast.lear --window smoke --model-name lear_x --recalibrate-every 7 --out artefacts/forecasts/price_quantiles_smoke.parquet --diagnostics artefacts/forecasts/lear_x_diagnostics_smoke.parquet --top-features artefacts/forecasts/lear_x_top_features_smoke.csv --feature-groups artefacts/forecasts/lear_x_feature_groups_smoke.csv
	$(PYTHON) -m v2g.forecast.evaluate --window smoke --forecasts artefacts/forecasts/price_quantiles_smoke.parquet --out artefacts/forecasts/eval_lear_smoke.csv

forecast-lear-smoke:
	$(PYTHON) -m v2g.forecast.lear --window smoke --out artefacts/forecasts/price_quantiles_smoke.parquet --diagnostics artefacts/forecasts/lear_diagnostics_smoke.parquet --top-features artefacts/forecasts/lear_top_features_smoke.csv
	$(PYTHON) -m v2g.forecast.evaluate --window smoke --forecasts artefacts/forecasts/price_quantiles_smoke.parquet --out artefacts/forecasts/eval_lear_smoke.csv

# Execute the result notebooks in place so the saved files carry their outputs (figures -> artefacts/figures/).
notebooks:
	$(PYTHON) -m jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=600 notebooks/01_model_performance.ipynb notebooks/02_forecast_horizons.ipynb

# --- Stage S5: optimiser on the price forecasts (teammate's optimiser, wrapped; see 8b_singleev_optimiser/v2g-hack/docs/forecast_backtest.md)
optimise-backtest:
	$(PYTHON) 8b_singleev_optimiser/v2g-hack/scripts/forecast_backtest.py --window backtest

optimise-backtest-smoke:
	$(PYTHON) 8b_singleev_optimiser/v2g-hack/scripts/forecast_backtest.py --window smoke --out-dir artefacts/schedules_smoke

forecast-naive-smoke:
	$(PYTHON) -m v2g.forecast.baselines --window smoke --out artefacts/forecasts/price_quantiles_smoke.parquet
	$(PYTHON) -m v2g.forecast.evaluate --window smoke --forecasts artefacts/forecasts/price_quantiles_smoke.parquet --out artefacts/forecasts/eval_naive_smoke.csv

all: test
