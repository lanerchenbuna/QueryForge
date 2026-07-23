# Example Commerce Business Rules

Use these rules only when the schema and question concern orders or commerce:

- Treat `order_amount` as the gross amount before refunds unless schema evidence says
  otherwise.
- For recognized revenue, prefer `net_amount`; otherwise compute gross amount minus
  discounts and refunds using available fields.
- Exclude cancelled orders from completed-sales metrics. Match the exact observed status
  value rather than inventing `cancelled` or `canceled`.
- Do not exclude cancelled orders when the user asks about demand, cancellation rate, or
  all submitted orders.
- Count distinct order identifiers for order counts after joining line-item tables.
- State any unavoidable metric assumption in the SQL explanation.
