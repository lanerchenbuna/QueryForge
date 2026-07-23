# QueryForge Bundled Skills

Each child directory is a prompt-only skill containing `skill.yml` metadata and
`SKILL.md` instructions. QueryForge reads these files as text; it never imports or
executes code from a skill.

In the default `--skills auto` mode, `enabled: true` skills form the baseline and the
LLM may add up to three question-relevant skills whose `allowed_nodes` contains the
current node. Passing `--skills name_a,name_b` skips automatic routing and loads exactly
those applicable skills, including skills disabled by default. `--skills none` loads no
skills. Higher `priority` skills appear first.

The bundled catalog spans the data-engineering lifecycle:

1. requirements and data contracts
2. ingestion and CDC
3. data modeling
4. transformations and orchestration
5. SQL generation and business semantics
6. data quality testing
7. performance optimization
8. observability and operations
9. governance and security

Only `sql_best_practices` is enabled for the current `gen_sql` node by default.
Domain-specific and lifecycle skills are opt-in so unrelated database questions do
not inherit the wrong assumptions.
