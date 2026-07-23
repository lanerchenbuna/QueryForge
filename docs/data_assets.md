# Data Asset Build Layer

The data asset build layer creates governed SQLite analytics tables before they are
queried by QueryForge. It is intentionally outside the read-only `DatabaseTool`
boundary:

```text
CSV / Parquet / JSON API
          |
          v
field normalization -> staging SQLite -> quality + quarantine
          |
          v
published SQLite tables -> generated semantic model -> QueryForge
```

Published business tables are written to the database passed to
`--publish-database`. Staging data, watermarks, quarantined rows, lineage records,
and the generated semantic model are stored under `--state-root` instead, so
internal pipeline tables are not exposed to NL2SQL schema discovery.

## Run a Build

CSV and JSON API sources use the core dependencies. Parquet sources additionally
require `pyarrow`:

```bash
python -m pip install -r requirements-assets.txt

python scripts/scaffold_data_asset.py \
  --source sample_data/data_assets/anime_watch_events.csv \
  --output .queryforge/anime-watch-assets.yml \
  --owner engagement-analytics

# Review the generated semantics and set semantic_model.reviewed: true.
python scripts/build_data_assets.py \
  --config sample_data/data_assets/assets.yml \
  --publish-database .queryforge/demo/analytics.sqlite \
  --state-root .queryforge/data_assets
```

The command returns one JSON result per asset and exits non-zero when any asset
fails. Point the normal QueryForge CLI at the output database and generated model:

```bash
queryforge \
  --database .queryforge/demo/analytics.sqlite \
  --semantic-model .queryforge/data_assets/semantic_assets.yml \
  --question "Show watch time by anime title"
```

## YAML Contract

```yaml
version: 1
semantic_model:
  name: anime_engagement
  description: Reviewed anime engagement semantics.
  owner: engagement-analytics
  reviewed: true
  auto_count_metrics: true
  relationships: []
  join_paths: []
  metrics: []
assets:
  - name: anime_watch_events
    target_table: fact_watch_events
    source:
      type: csv # csv | parquet | api
      path: anime_watch_events.csv
    column_aliases:
      Event ID: event_id
      Anime Title: anime_title
      Watched At: watched_at
    watermark_field: watched_at
    quality:
      required_columns: [event_id, anime_title, watched_at]
      unique_key: [event_id]
      max_invalid_ratio: 0.05
    semantic:
      entity_name: watch_events
      entity_type: fact
      description: One uploaded anime playback event.
      grain: [event_id]
      dimensions: [event_id, anime_title, watched_at, device_type]
      owner: engagement-analytics
```

`column_aliases` maps source fields to stable snake_case names. Unmapped names are
normalized to ASCII snake_case. The pipeline trims text, converts blank cells to
`NULL`, preserves JSON values as JSON text, and infers SQLite `INTEGER`, `REAL`, or
`TEXT` columns from the batch.

For API input, the source accepts `url`, `headers`, `params`, `records_path`,
`page_param`, `page_size`, and `max_pages`. Header values, parameters, and URLs may
reference environment variables with `${NAME}`. Keep API secrets in `.env` or the
deployment environment, never in the YAML file.

```yaml
source:
  type: api
  url: https://api.example.com/v1/orders
  headers:
    Authorization: Bearer ${ORDERS_API_TOKEN}
  records_path: data.records
  page_param: page
  page_size: 500
  max_pages: 100
```

## Quality, Incremental State, and Lineage

- `required_columns` sends null or blank required fields to quarantine.
- `unique_key` detects duplicates inside the batch and against existing published
  rows; duplicates are quarantined rather than silently discarded.
- `max_invalid_ratio` blocks publication if a batch exceeds its configured error
  budget. Its default is `1.0`, allowing teams to begin by observing quarantined
  data before enforcing a stricter threshold.
- `watermark_field` publishes only values greater than the asset's last successful
  watermark. It cannot be combined with `publish_mode: replace`, preventing
  accidental replacement by a delta batch.
- Append publication rejects schema drift rather than silently changing a published
  table. Use an explicit migration or `publish_mode: replace` for a full rebuild.

The state database contains `asset_watermarks`, `asset_lineage`,
`asset_quarantine`, and `semantic_catalog`. Each lineage row records source type and
location, target table, row counts, watermark transition, status, error, timestamp,
and published database path.

## Semantic Publication

Every upload must include a reviewed model-level `semantic_model` contract and a
non-empty `semantic` contract for every asset. Upload is rejected before publication
when either is missing or when `semantic_model.reviewed` is false. For
`entity_type: fact`, declare `grain` or `primary_key`; referenced semantic columns
are verified before publication.

After all configured assets succeed, the pipeline builds entities and dimensions,
adds safe count metrics when `auto_count_metrics` is enabled, and includes the
reviewed metrics, relationships, and Join Paths from `semantic_model`. The complete
model is validated against the physical database and operational contracts.

The optional `semantic` contract additionally accepts `contract_version`, `owner`,
`sla`, `refresh_frequency`, `sensitivity`, and `quality_rules`. The generated entity
sets `expected_columns` to the published table schema and rejects additive drift. It
is validated in a temporary `.pending.yml` file before atomically replacing the
published model. The data and semantic model form one atomic publication: any
semantic failure restores the prior database and watermark state. By default the
model is published as `<database>.semantic.yml`, allowing automatic query-time
discovery. See [Semantic Layer Authoring](semantic_authoring.md) and
[Operational Semantic Contracts](semantic_contracts.md).

Use `scripts/semantic_model_tool.py` to validate the generated model after adding
metrics and relationships.
