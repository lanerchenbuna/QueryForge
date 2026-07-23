# Semantic Layer Source Inventory

## Coverage

- Coverage level: repository-complete for QueryForge's SQLite and data-asset paths.
- Sources checked: semantic schemas and loader, builder, contract validator, asset
  pipeline, workflow integration, tests, sample model, CLI, REST, MCP, and docs.
- Missing high-value lanes: production warehouse catalogs, organization-specific
  metric dictionaries, and real dashboard definitions are outside this repository.
- Rejected or lower-confidence candidates: query history and LLM-generated SQL are
  not treated as canonical semantic evidence.

## Sources

| Source | Type | Locator | Permission | Last checked | Supports | Gaps or caveats | Automation eligible | Update boundary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Semantic schemas and loader | Code | `queryforge/domain/semantic/` | Read/write | 2026-07-24 | Entities, dimensions, metrics, relationships, Join Paths, physical validation | SQLite only | Yes | Update with tests |
| Semantic builder | Code | `queryforge/domain/semantic/builder.py` | Read/write | 2026-07-24 | Inference, curated merge, evidence report, atomic model publication | Heuristic metrics require review | Yes | Draft structural improvements; preserve curated definitions |
| Asset pipeline | Code | `queryforge/data_assets/` | Read/write | 2026-07-24 | Upload, quality, lineage, semantic publication | CSV/Parquet/API batch ingestion | Yes | Update with rollback and contract tests |
| Anime semantic model | Maintained contract | `sample_data/anime_streaming/semantic_model.yml` | Read/write | 2026-07-24 | Full worked example with multiple fact grains and business metrics | Synthetic domain | Yes | Preserve reviewed definitions |
| Automated tests | Tests | `tests/test_semantic_builder.py`, `tests/test_data_assets.py`, `tests/test_semantic_model.py` | Read/write | 2026-07-24 | Regression evidence | Does not establish organization-specific business truth | Yes | Update alongside behavior |
| Weekly semantic audit | Automation | `.github/workflows/semantic-weekly.yml`, `scripts/check_semantic_drift.py` | Read/write | 2026-07-24 | Schema, metric, relationship, Join Path, and quality drift | Becomes active after the repository is pushed with GitHub Actions enabled | Yes | Report only; baseline changes require explicit review |
| Query history and reference SQL | Derived evidence | `.queryforge/`, `sample_data/**/reference_sql/` | Local | 2026-07-24 | Query patterns and terminology | Never canonical by itself | No | Supporting evidence only |
