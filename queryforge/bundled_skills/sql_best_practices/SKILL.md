# SQL Best Practices

- Generate exactly one read-only `SELECT` or `WITH ... SELECT` statement.
- Select only columns needed to answer the question; never use `SELECT *`.
- Use short, meaningful table aliases and qualify ambiguous columns.
- Quote SQLite identifiers containing spaces or punctuation with double quotes.
- Make joins explicit and verify every join key against the supplied schema.
- Exclude `NULL` values when ranking or aggregating unless nulls are meaningful.
- Use `NULLIF(denominator, 0)` and cast integer numerators for ratios.
- Make top/bottom results deterministic with an appropriate secondary ordering.
- Apply a reasonable `LIMIT` to row-level previews, but do not add one to a scalar
  aggregate or when the user explicitly requests the complete result.
- Prefer clear CTE names when a query contains multiple logical stages.
