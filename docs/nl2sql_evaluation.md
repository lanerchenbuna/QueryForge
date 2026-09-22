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
so coverage is reported honestly (uniqueness is computed from a fingerprint of
the original case definition — question + expected SQL/probe — not from the
generated output).

## Comparison Policy and State Isolation

Result comparison is deterministic and declared:

- Row order is ignored (multiset comparison); duplicate rows are preserved —
  the comparison never deduplicates with a set.
- Column order is tolerated when the column name sets match.
- NULL compares equal to NULL only.
- Integral floats compare equal to ints; non-integral floats are rounded to 10
  decimals (float-noise tolerance); non-finite floats compare as
  `"Infinity"` / `"-Infinity"` / `"NaN"`.
- `oracle_latency_ms` (time to execute the expected SQL) is recorded per case
  and reported alongside service latency (`average_service_latency_ms` /
  `average_oracle_latency_ms`).

Every evaluation run redirects SQL history, orchestration state, and the vector
knowledge base into an isolated root (`.queryforge/evaluation_assets/isolated/`
by default), so evaluation never pollutes production retrieval or session
state; the report's `state_isolation` block documents the resolved paths.

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

### Token counts are measured, not estimated

When the provider returns usage, the report records the real counts and sets
`token_source: "measured"`. Only when a provider exposes no usage at all does the
report fall back to the `character_count / 4` heuristic and set
`token_source: "estimated"`. Read the field before quoting a number: an `estimated`
figure cannot support a cost conclusion.

Per-million prices are configured for comparable estimates only. Treat cost as an
indicator of relative expense, never as an invoice.

## Frozen Baselines

`evaluation/reports/` is gitignored, so raw evaluator output has no versioned carrier.
The numbers below are the tracked record; each row names the report file it came from.
**A baseline is only current if its versions are unchanged** — change the model, the gold
set, the semantic model, the SQL policy, or the comparison rules, and these become
historical.

Versions: provider `deepseek`, model `deepseek-v4-flash`, gold set
`evaluation/gold/nl2sql_multidomain.jsonl` (first 40 = `anime_content`), semantic model
`sample_data/anime_streaming/semantic_model.yml`.

| Run (report file) | `semantic_correctness_rate` | `sql_execution_success_rate` | `p50_latency_ms` | In / out tokens |
|---|---|---|---|---|
| `nl2sql_skill_auto.json` — automatic skill selection | 0.84375 | 1.0 | 12260 | 1742 / 633 |
| `nl2sql_model_eval.json` — skills inactive | 0.875 | 1.0 | 5138 | 1446 / 438 |
| `nl2sql_selfverify2.json` — after reasoning-payload and prompt fixes | 0.875 | 1.0 | 11728 | 1734 / 579 |
| `nl2sql_defects_fixed2.json` — after the governance defect pass | 0.875 | 1.0 | 13906 | 1754 / 866 |

> **These are single runs of 40 cases.** Differences of ±0.03 in
> `semantic_correctness_rate` have been observed across *identical* code, so the three
> 0.875 rows are the same result, not three improvements. Do not present a delta of that
> size as a gain; raise `--repeat` until the interval separates.

Two findings worth carrying forward, both from controlled comparisons rather than
aggregate impressions:

- **Automatic skill selection costs p50 +139% and output tokens +44%** (12260 ms vs 5138 ms;
  633 vs 438) with no demonstrated accuracy benefit. `--skill-mode auto|off` exists to test
  this; it is unresolved, not settled.
- **A second SQL candidate does not improve accuracy and costs ~20% p50 latency.** Measured
  over 20 paired cases with candidate count forced, not grouped by the gold set's
  `candidate_selection` flag (which correlates perfectly with task category and therefore
  measures difficulty). Parallel candidates remain an explicit opt-in.

A real-model run spends money. Confirm the account balance first: a depleted balance returns
HTTP 402, which the evaluator classifies as `environment_error` and excludes from the
accuracy denominators.


## Maintain the Gold Set

Regenerate the deterministic checked-in file after changing its templates:

```bash
python scripts/generate_nl2sql_gold.py
python -m unittest -q tests.test_evaluate_sql
```

Review SQL and category changes as data-contract changes. Do not alter expected SQL
solely to make a model score higher; use semantic result equivalence to accommodate
legitimate query rewrites.
