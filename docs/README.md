# QueryForge Documentation

Installation and quick-start commands are in the [project README](../README.md#quick-start).
Use the guides below for your next task.

## Start Here

- [Configuration](configuration.md) — environment variables, providers, paths
- [QueryForge Studio](studio.md) — the web UI: connect a dataset, review semantics, analyse
- [REST API](api_reference.md) — HTTP endpoints
- [MCP server](mcp_server.md) — expose QueryForge to an MCP client as tools

## Semantic Layer

- [Semantic model authoring](semantic_authoring.md) — describe metrics, dimensions and joins
- [Semantic contracts](semantic_contracts.md) — the rules a semantic model must satisfy
- [Subject tree](subject_tree.md) — domain organisation

## Architecture and Governance

- [Agent team architecture](agent_team_architecture.md) — entry routing, orchestration, workflow
- [Database adapters](database_adapters.md) — SQLite default; DuckDB and PostgreSQL optional

## Data

- [Data asset ingestion](data_assets.md) — bringing your own datasets in
- [Report artifacts](report_artifact.md) — what a run produces and where it is written

## Evaluation

- [Evaluation guide](nl2sql_evaluation.md) — offline checks, model benchmarks and result contracts
- [Runnable examples](demo/README.md) — data publication, analysis and recovery without an API key

## Development Checks

Run from the repository root after installing the development dependencies:

```bash
./init.sh             # environment and offline tests
make check            # repository hygiene and offline acceptance
make semantic-check   # sample semantic-model drift
make web-check        # Studio lint, types, build and tests
make package          # Python source and wheel distributions
```
