# Operational Semantic Contracts

QueryForge semantic YAML can declare operational ownership and data-quality contracts
for entities, dimensions, metrics, and Join Paths. The fields are backward-compatible:
existing semantic models remain valid and receive conservative defaults.

```yaml
entities:
  - name: watch_session
    table: fact_watch_session
    entity_type: fact
    grain: [session_id]
    expected_columns: [session_id, user_id, episode_id, watch_seconds, watch_date_key]
    allow_additive_columns: false
    contract_version: "2.0"
    owner: engagement-analytics
    sla: PT1H
    refresh_frequency: hourly
    sensitivity: confidential
    quality_rules:
      - rule: range
        column: watch_seconds
        minimum: 0
    dimensions:
      - name: watch_date
        column: watch_date_key
        owner: engagement-analytics
        sla: PT1H
        refresh_frequency: hourly
        sensitivity: internal
        quality_rules:
          - rule: null_rate
            max_null_rate: 0.01
```

`contract_version`, `owner`, `sla`, `refresh_frequency`, `sensitivity`, and
`quality_rules` are supported on entities, dimensions, metrics, and Join Paths.
Sensitivity values are `public`, `internal`, `confidential`, or `restricted`.

## Enforceable Rules

| Rule | Scope | Check |
| --- | --- | --- |
| `schema_drift` | Entity | Validates `expected_columns`; additive fields are blocked when `allow_additive_columns: false`. |
| `primary_key` | Entity | Validates declared primary key or grain for null rows and duplicate groups. |
| `null_rate` | Entity, dimension, metric | Enforces `max_null_rate`; a dimension rule defaults to its own column. |
| `unique` | Entity, dimension, metric | Fails when a column has duplicate value groups. |
| `range` | Entity, dimension, metric | Enforces numeric `minimum` and/or `maximum`. |
| `foreign_key` | Relationship, Join Path | Detects non-null source values with no referenced target row. |

Every declared relationship is checked for referential consistency. Join Paths repeat
those checks for each named relationship, so a path cannot be published over orphaned
keys. A Join Path may also record its own `foreign_key` quality rule for explicit
governance metadata. Metric rules must set `column` when they are not associated with
a dimension.

`severity: warning` records a failed result without blocking publication. The default
`error` severity blocks semantic publication.

## Validate a Model

```bash
python scripts/semantic_model_tool.py \
  --database warehouse.sqlite \
  --model semantic_model.yml \
  --contract-report contract-report.json \
  --output semantic-model.md
```

The command validates physical semantic references first, then runs the operational
contract checks read-only. It exits non-zero when one or more `error` rules fail.

## Asset Publication Gate

`scripts/build_data_assets.py` writes a generated semantic model to a temporary
`.pending.yml` file, validates it against the published SQLite tables, and only then
atomically publishes the final model. Reports are retained in the asset state
database's `semantic_contract_reports` table. When a contract blocks publication,
business tables remain available for remediation, but the new semantic model is not
published and the build exits non-zero.
