# Governance and Security

- Query only fields needed for the stated analytical purpose.
- Avoid returning direct identifiers or sensitive attributes unless explicitly required.
- Prefer aggregated output when row-level detail is unnecessary.
- Do not infer authorization from the presence of a column; the surrounding application
  must enforce access control.
- Preserve source lineage and metric definitions in explanations for auditable results.
- Respect retention and deletion semantics visible in the data contract.
- Never place credentials, tokens, or connection secrets in SQL or generated explanations.
- Skills guide generation but do not grant access, execute code, or override read-only SQL
  enforcement.
