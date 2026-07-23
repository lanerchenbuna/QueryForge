# Semantic Layer Authoring

QueryForge treats the semantic layer as a publication contract, not optional prompt
decoration. Production configuration requires a validated model before a database can
be queried, and uploaded data is committed only when its reviewed semantic model passes
physical and operational contract validation.

## Recommended Workflow

```text
source data
  → semantic scaffold
  → business review
  → physical + contract validation
  → atomic data and semantic publication
  → automatic query-time discovery
```

### 1. Existing SQLite database

Create or incrementally refresh a model:

```bash
python scripts/build_semantic_model.py \
  --database warehouse.sqlite \
  --output warehouse.semantic.yml \
  --owner data-platform
```

The builder uses SQLite primary and foreign keys first, then schema metadata,
bounded profiling, and naming heuristics. It generates:

- `warehouse.semantic.draft.yml`: inspectable proposal when publication is blocked
  or `--draft-only` is used;
- `warehouse.semantic.build.json`: evidence, table profiles, review items, and
  contract results;
- `warehouse.semantic.yml`: atomically published model after validation passes.

If the output already exists, curated entities, metrics, relationships, and Join
Paths remain authoritative while newly discovered schema is proposed incrementally.

### 2. CSV or Parquet upload

Generate a mandatory-semantic upload contract:

```bash
python scripts/scaffold_data_asset.py \
  --source events.csv \
  --output events.assets.yml \
  --owner engagement-analytics
```

The scaffold normalizes columns, proposes an entity type and grain, detects common
PII columns, and writes an explicit review checklist into the contract state. Before
upload:

1. verify entity grain and hidden columns;
2. add canonical metrics and their filters;
3. add relationships and Join Paths for multi-table batches;
4. set `semantic_model.reviewed: true`.

Then publish:

```bash
python scripts/build_data_assets.py \
  --config events.assets.yml \
  --publish-database warehouse.sqlite
```

The default model is written beside the database as `warehouse.semantic.yml`, which
QueryForge discovers automatically. If any asset, relationship, metric, or quality
contract fails, the entire batch is rolled back: no uploaded rows or watermark
advance remains without a semantic layer.

## Upload Contract Shape

```yaml
version: 1
semantic_model:
  name: engagement
  description: Canonical engagement semantics.
  owner: engagement-analytics
  reviewed: true
  auto_count_metrics: true
  relationships: []
  join_paths: []
  metrics:
    - name: watch_hours
      description: Valid playback duration in hours.
      entity: watch_event
      aggregation: sum
      expression: SUM(fact_watch_event.watch_seconds) / 3600.0
      owner: engagement-analytics

assets:
  - name: watch_event
    target_table: fact_watch_event
    source: {type: csv, path: events.csv}
    quality:
      required_columns: [event_id]
      unique_key: [event_id]
    semantic:
      entity_name: watch_event
      entity_type: fact
      description: One playback event.
      grain: [event_id]
      hidden_columns: [viewer_email]
      dimensions: [event_id, anime_title, watched_at, device_type]
      owner: engagement-analytics
      sensitivity: confidential
```

Every asset must include `semantic`; an empty block is invalid. A fact must declare
grain or primary key. The model-level `reviewed` flag must be explicitly confirmed
before publication. Safe count metrics can be generated automatically; business
measures, ratios, filters, and cross-table relationships should be declared
explicitly.

## Query-Time Enforcement

`REQUIRE_SEMANTIC_MODEL=true` is the default. QueryForge resolves semantics in this
order:

1. request or CLI `semantic_model_path`;
2. `SEMANTIC_MODEL_PATH`;
3. `<database>.semantic.yml`;
4. `semantic_model.yml` beside the database;
5. `<database>_semantic_model.yml`.

If no model is found, the query is rejected with the exact build command. The
`--allow-schema-only` / `allow_schema_only` option exists only as an explicit
diagnostic escape hatch.

## What Still Requires Human Judgment

Structural inference can safely propose tables, keys, PII, and physical foreign-key
relationships. It cannot decide the canonical revenue definition, exclusions,
timezone, attribution window, denominator, late-arriving-data policy, or whether a
heuristic relationship is analytically valid. Those definitions belong in reviewed
metrics, filters, Join Paths, ownership, freshness, and quality contracts.

The provenance used for this implementation is recorded in
[Semantic source inventory](semantic_source_inventory.md).

## Weekly Drift Monitoring

The checked-in [weekly workflow](../.github/workflows/semantic-weekly.yml) runs every
Monday at 09:00 Asia/Shanghai and can also be started manually from GitHub Actions.
It compares the current database and semantic model with
`sample_data/anime_streaming/semantic_baseline.json`, then reruns all operational
quality contracts.

Run the same check locally:

```bash
python scripts/check_semantic_drift.py \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --model sample_data/anime_streaming/semantic_model.yml \
  --baseline sample_data/anime_streaming/semantic_baseline.json \
  --report .queryforge/semantic-weekly-report.json
```

The command exits non-zero for table/column drift, entity or grain changes, metric
changes, relationship or Join Path changes, and data-quality contract failures.
GitHub retains the JSON report as a workflow artifact for 30 days.

Only refresh the reviewed baseline after intentionally approving the change:

```bash
python scripts/check_semantic_drift.py \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --model sample_data/anime_streaming/semantic_model.yml \
  --baseline sample_data/anime_streaming/semantic_baseline.json \
  --report .queryforge/semantic-baseline-update.json \
  --update-baseline
```

Baseline updates are blocked while any quality contract fails.
