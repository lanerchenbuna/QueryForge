# Data Quality Testing

- Test required columns for nulls and entity keys for uniqueness.
- Validate accepted categorical values using observed values or a documented contract.
- Check ranges, type conformance, temporal ordering, and referential integrity.
- Reconcile counts and additive measures between source and transformed layers at a shared
  grain.
- Measure freshness against source event time and ingestion time separately.
- Detect join fan-out by comparing entity counts before and after joins.
- Treat an empty result as potentially valid, but verify that filters and joins did not
  accidentally eliminate all rows.
- Prefer diagnostic queries that identify failing records, not only aggregate pass/fail.
