# Multi-Domain NL2SQL Evaluation

The checked-in gold set is [nl2sql_multidomain.jsonl](../evaluation/gold/nl2sql_multidomain.jsonl).
It contains 120 cases across three business domains:

| Domain | Cases | Data source |
| --- | ---: | --- |
| Anime content | 40 | Catalog, episodes, studios, and genre bridge |
| Viewer engagement | 40 | Playback, ratings, users, and subscriptions |
| Platform monetization | 40 | Advertising, subscriptions, and merchandise commerce |

Each domain contains 10 single-table, 8 multi-table, 6 time, 4 metric, 4 follow-up,
and 8 policy-rejection cases. All domains share the same connected anime platform
schema, deliberately exercising bridge tables, multi-hop joins, and multiple fact grains.

## Run a Live Evaluation

```bash
python scripts/evaluate_sql.py \
  --cases evaluation/gold/nl2sql_multidomain.jsonl \
  --model-provider qwen \
  --output .queryforge/evaluations/qwen-multidomain.json \
  --input-cost-per-million 0.8 \
  --output-cost-per-million 2.0
```

The evaluator supports the former `--database` argument as a default for simple
single-database JSONL files. Case-level `database`, `semantic_model`, and
`sql_policy` values take precedence.

## Case Contract

Query cases include `id`, `domain`, `category`, `question`, `database`,
`expected_sql`, and optional `semantic_model`, `sql_policy`, `follow_up_context`,
and `candidate_selection`. Rejection cases use:

```json
{
  "expected_outcome": "policy_rejection",
  "policy_probe_sql": "DELETE FROM fact_watch_session"
}
```

Policy probes are deliberately evaluated through the same read-only AST validator
used by the platform, rather than relying on an LLM to reproduce unsafe SQL.

## Fixed Metrics

- `sql_execution_success_rate`: successful workflow executions / query cases.
- `semantic_correctness_rate`: result-set equivalence against the expected SQL,
  independent of SQL formatting or an alternative valid query plan.
- `policy_rejection_precision` and `policy_rejection_recall`: expected dangerous
  probes vs safe expected SQL probes.
- `p50_latency_ms` and `p95_latency_ms`: end-to-end final-turn query latency.
- `average_estimated_input_tokens`, `average_estimated_output_tokens`, and
  `average_estimated_cost_usd`.
- `candidate_selection_uplift`: selected candidate semantic correctness minus the
  first generated candidate's correctness on candidate-enabled cases.

Token and cost values are explicitly heuristic (`character_count / 4`) because the
provider adapters do not expose normalized billing usage across all configured model
vendors. Configure per-million prices only for comparable estimates; do not treat
them as invoices.

## Maintain the Gold Set

Regenerate the deterministic checked-in file after changing its templates:

```bash
python scripts/generate_nl2sql_gold.py
python -m unittest -q tests.test_evaluate_sql
```

Review SQL and category changes as data-contract changes. Do not alter expected SQL
solely to make a model score higher; use semantic result equivalence to accommodate
legitimate query rewrites.
