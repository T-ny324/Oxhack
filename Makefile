PYTHON ?= python3

.PHONY: install format lint test all

install:
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e .

format:
	$(PYTHON) -m black src tests shared dashboard

lint:
	$(PYTHON) -m ruff check src tests shared dashboard

test:
	$(PYTHON) -m pytest tests -q

all: test
