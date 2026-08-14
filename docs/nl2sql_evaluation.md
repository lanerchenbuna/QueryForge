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

Policy probes are evaluated through the **real governance path**: the same
`SQLPolicyEngine` used at execution time, with the case's `sql_policy` (or the
default policy) and the case's physical database schema. Table/column scope,
LIMIT budgets, join rules, and dangerous-function rules are therefore actually
measured — not just the static read-only rejections.

## Fixed Metrics

- `sql_execution_success_rate`: successful workflow executions / query cases.
- `semantic_correctness_rate`: result-set equivalence against the expected SQL,
  independent of SQL formatting or an alternative valid query plan. Comparison
  is column-name aware: when the returned column set matches the expected set
  but the order differs, rows are reordered before comparing, so a correct
  answer in a different column order is not mis-scored as wrong.
- `policy_rejection_precision` / `policy_rejection_recall`, measured over
  **generated** SQL:
  - true positive: a probe correctly rejected by the policy engine;
  - false negative: a probe that slipped through (bypass);
  - false positive: a legitimate query case whose generated SQL the engine
    rejected. Precision therefore degrades when the policy is over-strict —
    it is no longer tautologically 1.0.
  - Supporting counts (`policy_true_positives`, `policy_false_positives`,
    `policy_false_negatives`, `policy_probe_errors`) and the governing rule per
    probe (`policy_rule`, `policy_name`) are included for audits.
- `p50_latency_ms` / `p95_latency_ms`: final-turn query latency. Follow-up
  warmup turns are excluded from each case's latency, and the first executed
  case (which includes provider-client warmup) is excluded from the aggregates.
- `average_estimated_input_tokens`, `average_estimated_output_tokens`, and
  `average_estimated_cost_usd`.
- `candidate_selection_uplift`: selected candidate semantic correctness minus the
  first generated candidate's correctness on candidate-enabled cases.
- `per_domain`: per-domain execution-success and semantic-correctness rates.

The report also exposes `query_count`, `probe_count`, and `unique_case_count`
so coverage is reported honestly (exact duplicate question+SQL pairs are
counted once in `unique_case_count`).

## Exit-Code Gates

```bash
python scripts/evaluate_sql.py \
  --cases evaluation/gold/nl2sql_multidomain.jsonl \
  --min-execution-success 1.0 \
  --min-semantic-correct 0.8 \
  --min-policy-recall 1.0
```

- `--min-execution-success` (default 1.0): fails when query cases run below the
  threshold. Probe-only runs are exempt (the metric is not measurable there).
- `--min-semantic-correct` (default 0.0, disabled): fails when semantic
  correctness falls below the threshold.
- `--min-policy-recall` (default 0.0, disabled): fails when any probe bypasses
  the policy engine.

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
