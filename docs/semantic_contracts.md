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

## Analysis Contracts

Metrics may declare the following optional contract. Legacy models still load; their
unit/time role remains unknown and their period completeness is unverified.

```yaml
metrics:
  - name: completion_rate
    description: Completed sessions divided by all sessions.
    entity: watch_session
    aggregation: ratio
    ratio:
      numerator: SUM(fact_watch_session.completed_flag)
      denominator: COUNT(*)
      scale: 1.0
    allowed_dimensions: [calendar.month, watch_session.device]
    time_field: fact_watch_session.watch_date_key
    analysis:
      unit: fraction
      additivity: non_additive
      time:
        role: watch_event
        timezone: UTC
        fiscal_year_start_month: 1
        coverage_start: "2023-01-01"
        complete_through: "2025-12-31"
        require_complete_periods: true
```

Omit `expression` when declaring `ratio`: it is derived as
`CAST(numerator AS REAL) * scale / NULLIF(denominator, 0)`. If both are supplied,
the expression must equal the canonical derived expression. Each component must be a
single `SUM`/`COUNT` over qualified base-table columns (or `COUNT(*)`), with no subquery,
nested aggregate, division, comments or extra statement. Numerator and denominator share
the metric's default/request filters, joins and time window. The compiler computes both
at the requested grain and returns `metric__numerator` / `metric__denominator` audit columns.
Zero denominators are undefined and cannot satisfy an analysis request.

The SQL gate verifies the declared numerator, denominator, scale and floating-point
division together, so merely referencing both aggregates does not prove the ratio.
An equivalent `AVG(column) * scale` is accepted for `SUM(column)/COUNT(column)`;
with `COUNT(*)`, it additionally requires physical non-nullability of that column.
An average of preaggregated group values is unsupported. Final-value `ROUND` for
presentation is allowed, while rounding a component changes the contract and is rejected.

`additivity` is `additive`, `non_additive`, or `semi_additive`. Ratios and distinct counts
must be non-additive. Semi-additive metrics must name `non_additive_dimensions` from their
allowed dimensions; time rollups over these dimensions are unsupported until an explicit
snapshot aggregation exists. Legacy ratio/distinct definitions receive conservative
non-additive inference. No ratio, distinct count or incompatible unit is added to another
metric's total. A missing fact population stays missing rather than becoming zero.

An undeclared legacy `COUNT(DISTINCT primary_key)` is a row count when the single
column is exactly the entity's fact grain. It can be summed over disjoint groups
only after the runtime quality gate proves that grain unique and the compiler's
join checks prevent duplication. Business distinct counts such as users per region
retain non-additive semantics; bounded or unavailable identity checks block the total.

`time.role` names the business event represented by the existing `time_field`; it does
not select a different field by column name. ISO timestamps may carry offsets; calendar
conversion supports UTC and explicit fixed offsets such as `+08:00`. Named DST zones
are rejected. Eight-digit date keys already represent civil dates and cannot be shifted.
Weeks begin Monday. Fiscal quarterly/yearly buckets use ISO period-start dates; other
labels remain `YYYY-MM-DD`, `YYYY-MM`, `YYYY-Qn`, or `YYYY`. Date ranges still use the
existing civil-calendar parser; fiscal buckets are not named fiscal-year range inference.
Daily leap-day year-over-year comparison has no same calendar day in a non-leap year and fails closed.
Weekly year-over-year comparison uses the Monday week containing the prior year's corresponding calendar date.

Coverage bounds are a reviewed ingestion guarantee supplied by the data owner, not
inferred from a fact table's min/max dates or the presence of one row per period. Both
bounds must be declared together. `require_complete_periods: true` requires them and
rejects requested windows outside coverage, clipped period buckets, missing requested
buckets and missing baselines. Without enforcement, outputs describe observed data and
report `period_completeness: unverified`. The publication gate and runtime SQL boundary
also reject invalid event dates, including normalized impossible dates such as February 30.
Contracts and component definitions participate in semantic drift snapshots.

## Active output and request obligations

Metric obligations are bound to the measure part of a request. Formal output-label
trailers such as `Return orders and lines in that order.` are labels, rather than
additional business measures. Explicit identifier lists must project original
columns; aggregates, constants and transformed identifiers fail before SQL
execution. Formal identifier-only output contracts require the final columns and
their order, without `SELECT *`. This is a bounded English/Chinese grammar, not
arbitrary natural-language understanding.

Runtime metric checks follow the final output's active dependencies through CTE
and derived-table projections. An unused CTE, unselected aggregate or unrelated
GROUP BY cannot prove a requested measure or grain. Independent fact counts are
checked in their own scopes; a fan-out join remains blocked. COUNT(*) can replace
a distinct fact-grain count only for a trusted non-null single-column primary key
on one physical source with no join. Other distinct-count obligations remain exact.
Explicit supported NULL requests require an AND-constrained IS NULL predicate in
the measure's active scope; omitted, negated or OR-weakened predicates fail closed.

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
