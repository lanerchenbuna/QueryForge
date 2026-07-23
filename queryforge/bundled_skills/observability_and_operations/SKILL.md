# Observability and Operations

- Track run identity, input/output partitions, row counts, duration, and terminal status.
- Monitor freshness, volume anomalies, schema changes, null rates, and rejected records.
- Distinguish source delays from pipeline failures using source and ingestion timestamps.
- Attach errors to the failing dataset, partition, transformation, and upstream dependency.
- Preserve enough lineage to identify downstream impact and support targeted reprocessing.
- Define retry behavior for transient failures and stop retrying deterministic data errors.
- Make alerts actionable with owner, severity, evidence, and recovery guidance.
- Verify recovery by rerunning quality checks, not only by observing a successful process.
