# Ingestion and CDC

- Identify source keys, ingestion timestamps, source event timestamps, and operation types.
- For incremental processing, use a stable watermark and document whether its boundary is
  inclusive or exclusive.
- Deduplicate retries by business key plus source version or event timestamp; use a
  deterministic tie-breaker.
- For CDC state reconstruction, apply inserts, updates, and deletes in source sequence.
- Distinguish late-arriving events from duplicates and avoid dropping valid corrections.
- Keep raw ingestion immutable; derive current-state or append-only views downstream.
- Validate row counts, rejected records, watermark movement, and freshness after a load.
