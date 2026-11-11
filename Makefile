.PHONY: install test lint format run live

VENV ?= .venv
PY   := $(VENV)/bin/python

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(PY) -m mypy

format:
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

run:
	$(PY) -m met_bread

live:
	MET_LIVE=1 $(PY) -m pytest tests/test_integration_live.py -v

demo:
	scripts/demo.sh http://$${MET_HOST:-127.0.0.1}:$${MET_PORT:-8000}
