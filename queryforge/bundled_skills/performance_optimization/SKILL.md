# Performance Optimization

- Filter early on selective and partition-like columns when semantics allow it.
- Project only required columns and avoid repeated computation.
- Aggregate at the required grain before joining large many-side tables.
- Prefer sargable predicates; avoid wrapping indexed filter columns in functions when an
  equivalent range predicate exists.
- Use `EXISTS` for membership checks when duplicate matches should not multiply rows.
- Avoid unnecessary `DISTINCT`; fix the join grain that created duplicates.
- Optimize only after preserving correctness, and mention any material semantic tradeoff.
- A `LIMIT` reduces returned rows but may not reduce sorting or aggregation work.
