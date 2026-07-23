# Changelog

All notable changes to QueryForge will be documented here.

The format follows Keep a Changelog principles. The project is currently pre-release
and does not yet claim semantic-versioning stability.

## Unreleased

### Added

- Mandatory, reviewable semantic layers for data-asset publication and queries.
- Atomic data-and-semantic publication with rollback on contract failure.
- Semantic scaffolding, incremental model building, drift baselines, and weekly audits.
- A deterministic 370,762-row anime platform dataset and 120-case NL2SQL gold set.
- QueryForge Studio for data onboarding, semantic modeling, governed analysis,
  trust inspection, and persistent run history.
- D1/R2-backed atomic uploads that require a reviewed semantic contract.
- GitHub Actions quality gates and repository contribution/security metadata.

### Changed

- Canonical CLI implementation now lives in `queryforge.cli`; root `main.py` remains
  a compatibility launcher.
