.PHONY: help install demo install-all test acceptance check sample semantic-check package web-install web-dev web-check web-build

PYTHON ?= python

# Every target below needs the project's dependencies (sqlglot, pydantic, ...).
# ``PYTHON ?= python`` alone takes whatever ``python`` is first on PATH, which on a
# machine with a system or conda interpreter is *not* the project venv, so
# ``make check`` failed with ``ModuleNotFoundError: No module named 'sqlglot'``
# while ``init.sh`` (which hardcodes ``.venv/bin/python``) stayed green — the
# verification command documented in AGENTS.md could not be run as written.
# ``?=`` reports origin as "file", so we cannot tell a caller-supplied value from
# our own fallback by origin alone. Test the value instead: if it is still the bare
# ``python`` we defaulted to, upgrade it to the repository venv. An explicit
# ``make PYTHON=/usr/bin/python3.12 check`` (or an environment ``PYTHON``) names a
# different interpreter and is left untouched.
ifeq ($(PYTHON),python)
ifneq ($(wildcard .venv/bin/python),)
PYTHON := .venv/bin/python
endif
endif

help:
	@echo "install         Install QueryForge in editable mode"
	@echo "install-all     Install all optional integrations"
	@echo "test            Run the complete unittest suite"
	@echo "demo            Run the step-17 end-to-end acceptance demos"
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

demo:
	$(PYTHON) docs/demo/run_all.py

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
