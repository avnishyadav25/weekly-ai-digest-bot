# Common tasks. "make install" first; the other targets use the virtualenv it creates.
#   make run JOB=uptime-check ARGS="--dry-run --offline"
PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
CONFIG ?= config.toml

.PHONY: install test lint format run schedule doctor

install: ## create .venv and install the pinned dependencies and the project
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install -r requirements.txt -r requirements-dev.txt

test: ## run the test suite (no network)
	$(BIN)/python -m pytest -q

lint: ## ruff: lint rules and formatting
	$(BIN)/python -m ruff check .
	$(BIN)/python -m ruff format --check .

format: ## apply ruff formatting and safe fixes
	$(BIN)/python -m ruff format .
	$(BIN)/python -m ruff check --fix .

run: ## run one job: make run JOB=<name> [ARGS="--dry-run --offline"]
	@test -n "$(JOB)" || (echo "usage: make run JOB=<name> [ARGS=...]; see: $(BIN)/automation jobs list" && exit 2)
	$(BIN)/automation run $(JOB) --config $(CONFIG) $(ARGS)

schedule: ## run the in-process scheduler until Ctrl+C
	$(BIN)/automation schedule --config $(CONFIG) $(ARGS)

doctor: ## check config, state path and notification channels
	$(BIN)/automation doctor --config $(CONFIG)
