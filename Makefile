PYTHON ?= python3

.PHONY: install format lint test data data-prices all

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

all: test
