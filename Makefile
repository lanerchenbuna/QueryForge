.PHONY: help install install-all test acceptance check sample semantic-check package web-install web-dev web-check web-build

PYTHON ?= python

help:
	@echo "install         Install QueryForge in editable mode"
	@echo "install-all     Install all optional integrations"
	@echo "test            Run the complete unittest suite"
	@echo "acceptance      Run offline acceptance checks"
	@echo "check           Run repository hygiene and full acceptance"
	@echo "sample          Validate the bundled anime dataset"
	@echo "semantic-check  Check sample semantic/data drift"
	@echo "package         Build wheel and source distribution"
	@echo "web-install     Install QueryForge Studio dependencies"
	@echo "web-dev         Start QueryForge Studio"
	@echo "web-check       Validate QueryForge Studio"
	@echo "web-build       Build QueryForge Studio for production"

install:
	$(PYTHON) -m pip install -e .

install-all:
	$(PYTHON) -m pip install -e ".[all,dev]"

test:
	LOG_LEVEL=CRITICAL $(PYTHON) -m unittest discover -s tests -q

acceptance:
	$(PYTHON) scripts/run_acceptance.py --full

check:
	$(PYTHON) scripts/check_repository.py
	$(PYTHON) scripts/run_acceptance.py --full

sample:
	$(PYTHON) -m queryforge --prepare-sample-data

semantic-check:
	$(PYTHON) scripts/check_semantic_drift.py \
		--database sample_data/anime_streaming/anime_streaming.sqlite \
		--model sample_data/anime_streaming/semantic_model.yml \
		--baseline sample_data/anime_streaming/semantic_baseline.json

package:
	$(PYTHON) -m build

web-install:
	npm --prefix web ci

web-dev:
	npm --prefix web run dev

web-check:
	npm --prefix web run check

web-build:
	npm --prefix web run build
