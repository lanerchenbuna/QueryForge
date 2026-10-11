# Evaluation Guide

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

Configure a provider key in `.env` as declared in `models.yml` (for DeepSeek,
`DEEPSEEK_API_KEY`). Live evaluation calls the provider and spends API credits.
Choose the provider/model explicitly and confirm the account is funded.

```bash
python scripts/evaluate_sql.py \
  --cases evaluation/gold/nl2sql_multidomain.jsonl \
  --model-provider deepseek \
  --parallel-candidates 1 --skill-mode auto \
  --output .queryforge/evaluations/nl2sql.json
```

The evaluator supports the former `--database` argument as a default for simple
single-database JSONL files. Case-level `database`, `semantic_model`, and
`sql_policy` values take precedence.

## Case Contract

Query cases include `id`, `domain`, `category`, `question`, `database`,
`expected_sql`, and optional `semantic_model`, `sql_policy`, `follow_up_context`,
and `candidate_selection`. Query cases can also declare `result_contract` (below). Rejection cases use:

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

- All reference projection columns are required by default. Matching one shared
  column cannot excuse a missing measure. Auxiliary reference columns require
  explicit `required_columns`; extra output columns require `allow_extra_columns`.
- Named columns align their cell positions. Unordered outer rows are multisets
  preserving duplicates; explicit `row_order: ordered` or a reference `ORDER BY`
  requires row order. Cells inside a row are never treated as a multiset.
- Empty output is allowed only by the task's empty policy. A failed, blocked or
  partial run cannot receive a correct-result verdict even with matching values.
- NULL and booleans are distinct from numbers. Non-finite numbers fail. Finite
  numbers use relative and absolute tolerance of 1e-9 in the NL2SQL comparator.
- Legacy equal-width projections with completely different aliases retain
  positional matching only without an explicit contract.
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

### Usage and Cost

When the provider returns usage, the report records the real counts and sets
`token_source: "measured"`. Only when a provider exposes no usage at all does the
report fall back to the `character_count / 4` heuristic and set
`token_source: "estimated"`. Read the field before quoting a number: an `estimated`
figure cannot support a cost conclusion.

Per-million prices are configured for comparable estimates only. Treat cost as an
indicator of relative expense, never as an invoice.

## Offline Checks

Run from the repository root with the project virtual environment:

```bash
# Governed agent tasks, without a model API call.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py \
  --tier 1 --gate --report evaluation/reports/offline.json

# Independent total reconciliation and category-period diagnostics.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py \
  --tier 1 --tasks-dir evaluation/diagnostics \
  --split dev --split regression --split holdout --gate \
  --report evaluation/reports/diagnostics.json
```

| Tier | Purpose | Requirements |
| --- | --- | --- |
| 1 | Deterministic governance and control-flow checks | Core dependencies; ScriptedModel supplies reference SQL |
| 2 | REST/MCP integration checks | Optional integrations, `pip install -e ".[all,duckdb]"`; no paid model |
| 3 | Real-model quality and usage | Explicit provider/model, credentials and funded account |

Tier 1 passing does not measure model accuracy. Runnable end-to-end examples are
in [demo/README.md](demo/README.md).

## Task Result and Source Contracts

Agent tasks and NL2SQL cases share an explicit `result_contract`:

```json
{
  "required_columns": ["calendar.month", "sales_channel", "net_revenue"],
  "allow_extra_columns": false,
  "row_order": "ordered",
  "empty": "forbid",
  "columns_path": "answer.columns",
  "rows_path": "answer.rows"
}
```

For NL2SQL cases, omit the paths; they default to `columns` and `rows`. Agent
`expected_values` declares the expected columns and rows. A task can declare
`values_absolute_tolerance` for paths such as floating-point reconciliation
residuals; other numeric comparisons keep their configured tolerance.

Source contracts require evidence that generated SQL reads the intended physical
facts. Matching values from an unrelated table cannot satisfy that requirement.
The check establishes necessary source coverage rather than proving every SQL
semantic. Query execution still passes through DatabaseTool and its policy gates.

Dataset IDs, templates, questions, retrieval corpora and physical schema
fingerprints are audited before execution. Dev, regression and holdout datasets
have separate schema scopes. Synthetic fixtures provide regression coverage;
they do not establish population-wide generalization.

Use [live_tasks_v3](../evaluation/live_tasks_v3/README.md) for new model comparisons;
its questions explicitly state the expected population. Preserve the original
questions and contracts when auditing an existing report.

## Repeated Paired Comparisons

The paired harness holds tasks and controls fixed and alternates A/B order.
Candidate experiments compare 1 versus 2 with skills at `auto`; skill experiments
compare `off` versus `auto` with a fixed candidate count.

```bash
# Offline comparison of candidate control flow.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py \
  --tier 1 --tasks-dir evaluation/live_tasks_v3 \
  --feature candidates --repeat 5 \
  --report evaluation/reports/paired-candidates.json

# Offline comparison of skill routing.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py \
  --tier 1 --tasks-dir evaluation/live_tasks_v3 \
  --feature skills --parallel-candidates 1 --repeat 5 \
  --report evaluation/reports/paired-skills.json

# Preview a live experiment; --dry-run makes no provider calls.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py \
  --tier 3 --tasks-dir evaluation/live_tasks_v3 \
  --provider deepseek --model deepseek-v4-flash \
  --feature candidates --repeat 5 --dry-run \
  --report evaluation/reports/live-manifest.json
```

Inspect the manifest before removing `--dry-run`. Each task runs once per repeat
and arm; selection, repair and follow-up turns can add provider calls. Model,
source, assets, gold data, policies and runtime versions are pinned in reports;
a version change during execution invalidates completion.

Interpret paired reports using their explicit denominators:

- Account/transport failures and provider-contract failures exclude the pair
  from both accuracy arms and make the run incomplete.
- Repeats are clustered by task for bootstrap confidence intervals. A single
  small run cannot establish a small accuracy change.
- Measured-token and priced-cost comparisons have their own eligible pair counts.
  Missing usage and unknown timeout costs remain `null`.
- A versioned `--prices` JSON enables cost estimates; they are not invoices.
- Keep questions, expected SQL and scoring contracts fixed. A changed task belongs
  in a new run rather than a rescore of an old model answer.

Raw reports are generated under the ignored `evaluation/reports/` directory.
Keep the report and its pinned inputs when sharing a benchmark result.

## Maintain the Gold Set

```bash
.venv/bin/python -B scripts/generate_nl2sql_gold.py
.venv/bin/python -B -m unittest -q tests.test_evaluate_sql
```

Review expected SQL and category changes as data-contract changes. Do not alter
expected answers to raise a model score; valid alternative queries are accepted
through deterministic result equivalence.
