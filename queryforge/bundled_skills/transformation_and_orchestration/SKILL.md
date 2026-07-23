# Transformation and Orchestration

- Separate extraction, cleaning, conformance, business logic, and serving stages.
- Make transformations deterministic and idempotent for the same input partition.
- Express dependencies through data availability, not arbitrary sleep intervals.
- Define partition boundaries and ensure reruns replace or merge only the intended range.
- Design backfills to use the same transformation logic as scheduled runs.
- Keep side effects out of analytical `SELECT` queries.
- Use readable CTE stages that mirror the transformation plan when generating complex SQL.
- Surface upstream freshness requirements and downstream consumers in a plan or review.
