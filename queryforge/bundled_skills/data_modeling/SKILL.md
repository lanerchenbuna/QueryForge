# Data Modeling

- Establish the grain of each source before joining it.
- Prevent fan-out: aggregate child records or deduplicate dimensions before joining when
  the requested result has a coarser grain.
- Use business keys for semantic joins and surrogate keys only when the schema establishes
  their relationship.
- For facts and dimensions, aggregate additive measures at the fact grain and join
  descriptive dimensions through verified keys.
- For slowly changing dimensions, use effective date ranges when answering historical
  questions; do not automatically attach the latest dimension version.
- Treat event timestamps, snapshot dates, and validity intervals as different concepts.
- Verify uniqueness assumptions rather than relying on table names.
