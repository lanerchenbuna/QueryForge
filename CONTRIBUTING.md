# Contributing to QueryForge

Thank you for helping improve QueryForge. The project favors small, reviewable
changes that preserve its semantic and security guarantees.

## Development Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
cp .env.example .env
```

Provider credentials are not required for the offline test suite.

## Before Opening a Pull Request

```bash
make check
```

At minimum, new behavior should include focused tests. Changes to semantic models,
metrics, relationships, Join Paths, SQL policy, or sample schema are data-contract
changes and must also update their validation evidence and documentation.

## Repository Conventions

- Keep dependencies flowing through the documented package layers.
- Use `queryforge.cli` for CLI implementation; root `main.py` is compatibility only.
- Keep normal analytics read-only and route SQL through the AST policy boundary.
- Never commit `.env`, credentials, run artifacts, user databases, or raw sensitive data.
- Preserve the bundled synthetic SQLite sample and all 15 versioned CSV exports.
  Regenerate them only through the documented sample-data workflow.
- Do not weaken the mandatory semantic-layer gate to make a test pass.

## Pull Requests

Keep each pull request focused. Explain:

1. the problem and intended behavior;
2. security or semantic-contract impact;
3. tests and manual verification performed;
4. compatibility or migration considerations.

Public contribution intake should begin only after the repository owner selects and
adds a project license.
