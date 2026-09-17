# Support Tickets Analytics Sample

This directory contains a fully synthetic, deterministic customer support
operations dataset. No row is copied from a real support desk, agent roster,
customer base, or third-party benchmark.

## Scale

The generated SQLite database contains **1,082 rows across 6 tables**:

| Table | Grain | Rows |
| --- | --- | --- |
| `dim_date` | calendar day | 366 |
| `dim_agent` | agent | 24 |
| `dim_queue` | queue | 12 |
| `fact_ticket` | ticket | 420 |
| `fact_ticket_sla_breach` | recorded breach (none yet) | 0 |
| `fact_csat_response` | survey response | 260 |

Two properties of this dataset are deliberate, because the agent benchmark needs
them:

- **`fact_ticket_sla_breach` is intentionally empty.** No service-level breach has
  been recorded, so any dimensioned breach question returns no rows at all and the
  honest answer is "no breach records exist" (`empty_result`).
- **`fact_csat_response` fails its quality contract.** `escalation_reason` never
  arrived from the survey export and is NULL for every response (a `null_rate`
  error for the governed check), and `csat_score` is missing for
  48% of responses on top of that. A question that depends on the
  response table must stop at the data fault instead of reporting a confident
  average (`data_fault`).

The `verbatim_comment` column is physically present but **outside the SQL policy
allowlist**, so a request for survey free text must be rejected rather than
answered. `fact_ticket` itself is fully populated, so ticket-level metrics are not
affected by either trap.

## Files

- `support_tickets.sqlite` — ready-to-query database;
- [semantic_model.yml](semantic_model.yml) — entities, dimensions, metrics, and safe Join Paths;
- [sql_policy.yml](sql_policy.yml) — table/column scope and query-shape budgets;
- [README.md](README.md) — this file.

Regenerate the database, semantic model, policy, and README from repository root
(with a fixed seed; regeneration is idempotent byte-for-byte):

```bash
python sample/generate_aux_datasets.py
```

The generator lives in [sample/generate_aux_datasets.py](../../sample/generate_aux_datasets.py).

Example:

```bash
python main.py \
  --database sample_data/support_tickets/support_tickets.sqlite \
  --semantic-model sample_data/support_tickets/semantic_model.yml \
  --sql-policy sample_data/support_tickets/sql_policy.yml \
  --question "How many tickets were created by queue in 2024?"
```
