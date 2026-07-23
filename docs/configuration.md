# Configuration Reference

QueryForge reads `.env` through `load_config()`. CLI/API/MCP request arguments take precedence
where a corresponding option exists.

## Core Paths

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | Anime Streaming SQLite sample | Default read-only SQLite database |
| `SEMANTIC_MODEL_PATH` | auto-discovered beside database | YAML semantic model |
| `REQUIRE_SEMANTIC_MODEL` | `true` | Reject analytical queries without a validated semantic layer |
| `SQL_SECURITY_POLICY_PATH` | unset | YAML SQL AST policy |
| `HISTORY_DB_PATH` | `.queryforge/history.db` | Compact successful SQL history |
| `ORCHESTRATION_STATE_ROOT` | `.queryforge/runs` | Agent Team state and artifacts |

## Advanced Workflow

| Variable | Default | Purpose |
| --- | --- | --- |
| `SUBJECT_TREE_ENABLED` | `false` | Enable subject-scoped context |
| `SUBJECT_TREE_PATH` | unset | Subject Tree YAML |
| `DEFAULT_SUBJECT` | unset | Scope fallback |
| `STREAMING_ENABLED` | `true` | Allow `AgentService.stream()` and SSE |
| `STREAMING_EVENT_BUFFER_SIZE` | `100` | Per-stream bounded event history |
| `REPORT_ENABLED` | `true` | Allow static report generation |
| `REPORT_MAX_ROWS` | `50` | Maximum rows embedded in report tables |
| `REPORT_MAX_CHARTS` | `3` | Maximum rule-selected report charts |
| `REPORT_OUTPUT_DIR` | `.queryforge/reports` | HTML and manifest destination |

Conversation memory is opt-in per request: pass `session_id` or `new_session`. Tool Loop,
parallel candidates and Subject Tree are disabled/minimal by default; structured reasoning is
optional in model output and never blocks a compatible response.

## MCP

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_RESOURCES_ENABLED` | `true` | Register read-only resources |
| `MCP_PROMPTS_ENABLED` | `true` | Register reusable MCP prompts |
| `MCP_SESSION_ENABLED` | `true` | Enable connection-level Conversation Memory |
| `MCP_HISTORY_LIMIT` | `20` | Maximum history items in MCP resource/tool output |

## Optional Dependencies

- REST/SSE: `pip install -r requirements-server.txt`
- MCP: `pip install -r requirements-mcp.txt`
- Vector retrieval: `pip install -r requirements-vector.txt`
- Parquet data assets: `pip install -r requirements-assets.txt`

All databases are opened read-only. Model provider secrets stay in `.env`; never put them in
semantic models, subject trees, prompts, reports, or artifacts.

The data asset build layer is the only deliberate write path. It writes separately specified
SQLite publication databases, never the database supplied to normal QueryForge analysis calls.
See [Data Asset Builds](data_assets.md) for source contracts, quality isolation, watermarks,
lineage, and semantic publication.

## Semantic Model Maintenance

Validate a semantic YAML file against its SQLite database and optionally produce Markdown
documentation and an operational contract report:

```bash
python scripts/semantic_model_tool.py \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --model sample_data/anime_streaming/semantic_model.yml \
  --contract-report semantic-contract-report.json \
  --output docs/anime-semantic-model.md
```

See [Operational Semantic Contracts](semantic_contracts.md) for owner, SLA, refresh,
sensitivity, version, schema-drift, null-rate, uniqueness, range, and referential
integrity contracts.

## SQL Policy Shape Budgets

SQL policy YAML can additionally bound query shape:

```yaml
allowed_tables: [fact_watch_session, dim_anime]
require_limit: true
max_limit: 100
max_tables: 2
max_joins: 1
allow_cross_join: false
```

`max_tables` and `max_joins` reject overly broad queries before SQLite execution.
`allow_cross_join` defaults to `false`, preventing accidental Cartesian products.
