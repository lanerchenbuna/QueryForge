# Requirements and Data Contracts

- Identify the requested business metric, population, time range, dimensions, and result
  grain before selecting fields.
- Preserve qualifiers such as timezone, currency, unit, age range, geography, and status.
- Determine the entity key and expected uniqueness at each stage.
- Do not silently reinterpret ambiguous business terms; expose the assumption in the
  explanation or request clarification when no schema evidence resolves it.
- Respect contract constraints visible in the schema: types, nullability, primary keys,
  and enumerated values.
- Keep numerator and denominator populations aligned for rates and percentages.
