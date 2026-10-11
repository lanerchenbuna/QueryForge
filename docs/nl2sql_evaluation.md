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
**The following live-model numbers are historical under the previous comparison policy.**
The phase-3 required-projection/order rules have not yet been evaluated with a real model.
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

## Phase-3 task contracts and paired experiments (2026-10-11)

Task-level `result_contract` is shared by the agent-task and NL2SQL evaluators:

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

For NL2SQL query cases, omit the paths (defaults are `columns` / `rows`).
A task's `expected_values` supplies the rows and column names. Agent-task numeric
values keep their existing relative tolerance and exact-zero behavior. The new
`values_absolute_tolerance` adds a bounded, per-path absolute tolerance only when
explicitly declared: diagnostic reconciliation residuals use 1e-9 to absorb
floating-point summation noise; other existing task assertions are unchanged.

`evaluation/diagnostics/` adds three planner tasks outside the existing frozen
32-task set. Independent base-table SQL in `scripts/freeze_diagnostic_tasks.py`
freezes both grouped values and separate period totals. Reproduce the three tasks:

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 \
  --tasks-dir evaluation/diagnostics --split dev --split regression --split holdout --gate \
  --report /tmp/queryforge_phase3_diagnostics.json
```

Dataset IDs, templates, questions and optional retrieval corpora are audited
before execution. Physical table/column/key/FK fingerprints enforce schema
isolation even if two dataset IDs point to copies of the same schema; each task
must match its dataset's `split_scope`. Dev uses anime, regression uses retail,
and holdout uses support. These small synthetic schemas establish test coverage,
not population generalization.

The paired harness compares one mechanism at a time on identical tasks:

```bash
# Offline control-flow QA; neither command calls a real model.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 1 \
  --feature candidates --repeat 5 --report /tmp/queryforge_phase3_paired_candidates.json
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 1 \
  --feature skills --parallel-candidates 1 --repeat 5 --report /tmp/queryforge_phase3_paired_skills.json

# Reviewable live-model manifest only; zero API calls.
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 3 \
  --provider deepseek --model deepseek-v4-flash --feature candidates --repeat 5 \
  --dry-run --report /tmp/queryforge_phase3_live_candidates_manifest.json
```

Candidate experiments compare 1 vs 2 with skills held at auto; skill experiments
compare off vs auto with candidate count fixed. History and vector retrieval are
disabled, each execution uses fresh state, and A/B order alternates within each
task. The manifest records every task/repeat/order and all controls. Reports pin
source, public model/skill assets, database bytes, semantic/policy contracts, gold,
thresholds and runtime versions; a version change during execution invalidates
completion. The general benchmark also accepts `--parallel-candidates` and
`--skill-mode` for explicit controlled runs.

The paired report keeps raw traces, deterministic task verdicts and failure
classes. A pair with an account/transport failure or a known provider signature
mismatch is excluded from both arms' accuracy denominator and listed separately;
such a run is incomplete and exits non-zero. Repeats remain inside their task
cluster for a seeded 2,000-sample percentile bootstrap. Reported means weight
tasks equally. Measured-token and measured/priced-cost pairs have their own
sample counts; unknown values remain null. A versioned `--prices` JSON is needed
for cost accounting. No price table was supplied in phase-3 offline checks.

Offline evidence is frozen in the [optimization roadmap](optimization_roadmap.md).
Live experiments require explicit approval and a funded account under AGENTS.md;
remove `--dry-run` only after approval. Each current manifest selects 23 workflow
tasks across three schemas, five repeats and two arms: 230 case executions per
experiment. These are case executions, not an upper bound on provider calls or
spend; selection, repair and follow-up turns can call the provider again. The
provider/model and budget remain a human decision. No live-model gain is claimed.

### Frozen phase-3 offline paired evidence

The two offline commands above were run on 2026-10-11 against the original
32-task set (23 workflow tasks qualify for these experiments). Both reports
passed their offline execution gate and had no version changes during execution.

| Experiment | Unique tasks / repeats | Evaluable pairs / total | Successful executions | Accuracy delta / task-cluster 95% interval | Measured token / cost delta |
| --- | --- | --- | --- | --- | --- |
| candidates 1→2, skills=auto | 23 / 5 | 115 / 115 | 230 / 230 | 0 / [0,0] | null / null |
| skills off→auto, candidates=1 | 23 / 5 | 115 / 115 | 230 / 230 | 0 / [0,0] | null / null |

Both excluded-environment and excluded-provider-contract pair counts are zero.
These values validate the scripted pipeline only. ScriptedModel returns the
reference SQL; the zero-width interval cannot establish model equivalence or
mechanism effectiveness. No live-model token, cost or latency gain was measured.

The reports' shared input/version fingerprints are frozen below. Runtime:
Python3.12.11, SQLite3.50.4, SQLGlot28.10.1, Pydantic2.13.4. The worktree is dirty
because the three-phase implementation is not committed; the source hash includes
new Python files, and the public model/skill assets have a separate digest.

```json
{
  "source_sha256": "32a2b6244d8fa5b737d7fd6a476fb33c323cddc5c28a35d641ddadf320ebe937",
  "assets_sha256": "06ad2b4d54be3939d1e6bed8641c72b18fcb7192d9e88d07967e5cc86801d2b3",
  "datasets_sha256": {
    "anime_streaming": "e5c53343b0d3cee46cbc02610353c60e2b909fd61c1e1fee16c222771e7ecfb8",
    "retail_orders": "04a18a5d5bc8118eae21b88a4159c1857f3caf86d5b0eb527e41a395f855543f",
    "support_tickets": "95829871b3525ed67256cfa7297c01c11d4ade01cacfe6a0436769b89e6065ad"
  },
  "contracts_sha256": {
    "anime_streaming": {
      "semantic_model": "fa6d790e860c2481104fe2bab3a18f3d0809e1bcb20c6f09d9be339b782d1124",
      "sql_policy": "a6817e5927206348ba833acc886ae8fdacd452f585ed1d489a3c86ecd1d00a01"
    },
    "retail_orders": {
      "semantic_model": "46238e583ffb49136286b0566a032eddfbf6981b4f51376a0c44a309d995b167",
      "sql_policy": "70ca076acf9ef843cc71d60b2661689abe2f803e9afc75f8e47402d9e726b27a"
    },
    "support_tickets": {
      "semantic_model": "66ca8903dd1406214dbd17239be34c86d88d8996fd1d679d6da41714ba575c9c",
      "sql_policy": "003e02d935e1d5f5059dabf0b3606b7444e7ab1cdbbbf0f7c1be0aa0cd5c625f"
    }
  },
  "gold_sha256": "ed76321afcaf6f5e5c45bb79f1af590e24b291b220b57602e90b7ffc558ef95b",
  "threshold_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "git_head": "d1e1e4f8ef95dfdf16bf7206ddcb797fa881eb4a",
  "dirty": true
}
```

## Authorized DeepSeek live pairs, explicit tasks v1 (2026-10-11)

The two authorized paid experiments are complete: each used 23 tasks across
three physically distinct schemas, five repeats and two arms (230 executions,
115 pairs). Candidate count compares 1→2 with skills=auto; skill selection
compares off→auto with candidate count=1. A/B order, questions, reference SQL,
databases, model parameters and code/assets/contracts stayed fixed within and
across these runs. Provider/model: `deepseek` / `deepseek-flash`, discovered as
DeepSeek-V4.1-Flash. The official [model documentation](https://api-docs.deepseek.com/quick_start/pricing/)
explains that the old v4 Flash name routes to V4.1. These results are not directly
comparable with the historical single-run 0.875 result above.

The first six-execution preflight used the original control-flow questions and
was stopped after question/oracle disagreement: device/country lists did not
request reference count columns, and user totals allowed different meanings.
Those executions are excluded from both experiments. Preflight account-balance
observations are kept in private local records rather than published here.

The separate [live-v1 corpus](../evaluation/live_tasks/README.md) explicitly
requests projections, labels and ordering while preserving original reference
SQL and databases. The original 32 tasks and thresholds are unchanged. Its
scripted preflight (`--tier 1 --repeat 2`, both features) passed 76/92 executions
per experiment. Four task families activated existing metric-gate/routing
failures; they remained in the live denominator. Scripted results describe
pipeline control flow, not model capability.

### Original results and separately audited scores

| Scoring / experiment | Baseline | Treatment | Delta | Task-cluster 95% interval |
| --- | ---: | ---: | ---: | --- |
| Frozen live-v1: candidates 1→2 | 79.1304% | 73.0435% | -6.0870 pp | [-13.9130, 0.0000] pp |
| Frozen live-v1: skills off→auto | 79.1304% | 76.5217% | -2.6087 pp | [-6.0870, 0.0000] pp |
| Additional source contract: candidates 1→2 | 77.3913% | 71.3043% | -6.0870 pp | [-13.9130, 0.0000] pp |
| Additional source contract: skills off→auto | 77.3913% | 73.9130% | -3.4783 pp | [-7.8261, 0.0000] pp |

Each arm's denominator is 115 executions. Original passes: candidates 91→84,
skills 91→88. Source-audited passes: candidates 89→82, skills 89→85. All 460 original
verdicts were reproduced exactly; none was overwritten. The nine newly rejected
passes read only `dim_date` for earliest/latest receipt keys. Fixture calendar
and receipt extremes happen to match. Replaying all 354 originally passing SQL
queries through DatabaseTool reproduced both stored and reference rows 354/354;
database bytes and original SQL-policy hashes matched. Matching rows alone
therefore cannot prove source intent or full SQL semantics.

A separate post-hoc sensitivity analysis removes one confirmed ambiguous
ranking question, `live_retail_orders_regression_4`. Its wording says “outlet”,
but `store_format=Outlet` is an actual subtype. Filtering those four stores is a
reasonable reading; the reference ranks all 24 stores. Neither arm passed this
task in either experiment. This question is a gold-design fault, not evidence
of a model failure. The primary results above retain it; no threshold was
relaxed and every original record remains available.

| Source audit + ambiguity exclusion | Baseline | Treatment | Delta | Task-cluster 95% interval |
| --- | ---: | ---: | ---: | --- |
| Candidates 1→2 (89/110→82/110) | 80.9091% | 74.5455% | -6.3636 pp | [-13.6364, 0.0000] pp |
| Skills off→auto (89/110→85/110) | 80.9091% | 77.2727% | -3.6364 pp | [-8.1818, 0.0000] pp |

This sensitivity analysis has 22 tasks×5 repeats=110 pairs per experiment.
Intervals use 2,000 seeded percentile bootstrap samples (seed 17), resampling
whole tasks and keeping repeats inside each task. Both intervals include zero;
these experiments establish no accuracy improvement from either mechanism.
The small curated fixtures do not establish population generalization.

### Latency, reported tokens and priced response usage

The following means use the same22-task sensitivity subset. Means give each
task equal weight. Differences are treatment minus baseline. Token totals include
provider-reported completion/reasoning tokens; estimated failed-call tokens
are not measurements.

| Measurement | Eligible pairs | Baseline mean | Treatment mean | Delta | Task-cluster 95% interval |
| --- | ---: | ---: | ---: | ---: | --- |
| Candidate wall seconds | 110 | 21.916467 | 24.072792 | 2.156325 | [-1.558006, 5.212513] |
| Skill wall seconds | 110 | 12.704076 | 23.689620 | 10.985543 | [7.812725, 15.157997] |
| Candidate measured tokens | 110 | 18498.700000 | 31011.018182 | 12512.318182 | [10884.390909, 14244.090909] |
| Skill measured tokens | 109 | 15075.893182 | 18621.406818 | 3545.513636 | [2682.572727, 4446.863636] |
| Candidate priced CNY/execution | 110 | 0.021003696 | 0.025221900 | 0.004218205 | [0.000849219, 0.007312811] |
| Skill priced CNY/execution | 109 | 0.012204155 | 0.020640890 | 0.008436735 | [0.006202863, 0.010821593] |

Candidates consumed 67.6389% more tokens and 20.0832% more priced CNY on this
subset, with no demonstrated quality gain; the wall-time delta interval spans
zero. Automatic skills consumed 23.5178% more measured tokens and 69.1300% more
priced CNY, and increased mean wall time 86.4726% (12.7041→23.6896 seconds).
Token/cost differences have positive intervals. These are measurements of this
configuration and shared provider-cache conditions, not a cold-cache tariff or
production-wide effect. The all23-task summaries remain in the audit JSON:
wall 21.6822→23.9587 seconds and tokens 18,225.1565→30,594.8783 for candidates;
wall 12.7801→23.4247 seconds and tokens 14,913.35→18,359.0761 for skills.
Full-corpus priced CNY means are 0.020759338→0.025081139 (115 pairs), and
0.012220516→0.020401366 (114 pairs), respectively.

Numeric provider responses reconcile exactly: candidates 913 responses,
5,614,304 reported tokens; skills 647 responses,3,835,434 reported tokens.
One skill-treatment execution (`live_retail_orders_regression_6`, repeat 3)
timed out during reflection after four completed model calls. Its 35,164 reported
tokens reconcile against reported model spans; the fifth call has unknown usage.
The original evaluator marks the mixed trace as estimated and measured_tokens=0.
That failed outcome is retained, not retried or dropped from quality/latency.
Consequently skill token/cost summaries omit its pair (109 sensitivity pairs,
114 full-corpus pairs); the whole experiment's total cost remains null. Reported
responses can be priced independently, without pretending the failed call was
free. This exception also shows that zero environment-exclusion counts do not
mean no transport incident: the frozen classifier retained this timeout as
`runner_error`.

The numeric observer paused after 175 executions. A guarded resume reused those
175 records verbatim and called the model only for the remaining 55. The original
incomplete report is preserved alongside the completed `*_resumed.json` report.
Offline resume QA reproduced 175/175 existing verdicts/records and every control
before continuing; a separate scripted CLI check retained 2 cases and ran only
2 pending cases (4/4 passed, zero provider responses).

The standard USD cost field remains null. The frozen [CNY price table](../evaluation/deepseek_flash_cny_2026_10_11.json)
uses separately measured cached input, uncached input and output at CNY 0.02/1/4
per million, from the [official price page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
checked on 2026-10-11. The Sunday off-peak window is pinned in UTC. Candidate
reported-response cost is CNY 5.27165492; skill reported-response subtotal is
CNY 3.79053536, excluding the unknown call. Their subtotal is CNY 9.06219028,
not a complete invoice. Private account balances are omitted from this public
record. No exchange-rate conversion was used.

### Changes implemented after both paid runs

`required_sql_tables` is an optional independent evaluator contract. SQLGlot
walks active physical sources through SELECTs, CTEs, scalar subqueries and unions;
unused CTEs and aliases do not satisfy it. Unsupported shapes/namespaces,
recursive CTEs, malformed constraints or missing SQL fail closed. A counterfactual
SQLite fixture extends the calendar without extending receipts and confirms
that the previous equal extrema were accidental. This checks necessary source
coverage, not joins, filters, active metric expressions or logical equivalence.
It changes the evaluation contract, not the production SQL execution boundary.

[live-v2](../evaluation/live_tasks_v2/README.md) adds only source-scoring metadata;
model questions and reference results are identical to v1. Re-scoring is marked
post-hoc and preserves original verdicts. [live-v3](../evaluation/live_tasks_v3/README.md)
clarifies “all stores, regardless of store_format” for future runs. No new model
responses were generated against v3, and the auditor rejects using a changed
question to retroactively grade v1 outputs.

The follow-up runner previously omitted the frozen today_provider: 20 candidate
follow-up turns reported 2026-10-11 despite the manifest's 2024-12-31 control.
The affected question counts all recorded receipts without a relative window;
its oracle is unaffected, but the date control was inaccurate. The runner now
passes the existing frozen-date factory, retry and vector controls through
AgentService. A real scripted two-turn regression verifies 2024-12-31 in every
turn. This fix happened after both experiments and does not rewrite their traces.

There are 30 added regressions (13 SQL-source,16 source/audit/gold,1 follow-up),
including tampered verdict/usage rejection, unknown cost, question-change
rejection and report immutability. The 184-test focused suite passes. The source
checker, usage audit and DatabaseTool replay made zero new model calls.

Several strict-contract failures require care: regional amount results in the
candidate experiment had correct cells but wrong requested column labels (10/10
cases), which is delivery-contract failure, not numeric error. Generic customer
satisfaction and SLA language binds extra aggregation obligations, and independent
receipt/line counts encounter grain gates. Correct final rows can still fail a
whole-run tool-validity contract after earlier failed attempts. These scores are
end-to-end governance/delivery results, not pure model SQL-generation accuracy.
Prioritize precise request-to-metric binding and output-column contracts before
adding calls; use measured triggers for skills/candidates rather than enabling
more work for every task. Runtime changes for those issues remain future work.

### Reproduction commands and frozen versions

The [archived observers](../evaluation/observers/README.md) contain the exact
numeric observer and resume instrument used, with hashes in the execution
reports. Run from the repository root with fresh report prefixes. The following
are the paid protocol, not commands to run as part of offline verification:

```bash
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/paired_usage_v1.py \
  --tier 3 --provider deepseek --model deepseek-flash --tasks-dir evaluation/live_tasks \
  --feature candidates --repeat 5 \
  --report evaluation/reports/phase3_deepseek_flash_candidates_live_v1.json
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/paired_usage_v1.py \
  --tier 3 --provider deepseek --model deepseek-flash --tasks-dir evaluation/live_tasks \
  --feature skills --parallel-candidates 1 --repeat 5 \
  --report evaluation/reports/phase3_deepseek_flash_skills_live_v1.json
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/paired_resume_v1.py \
  evaluation/reports/phase3_deepseek_flash_skills_live_v1.json \
  --tier 3 --provider deepseek --model deepseek-flash --tasks-dir evaluation/live_tasks \
  --feature skills --parallel-candidates 1 --repeat 5 \
  --report evaluation/reports/phase3_deepseek_flash_skills_live_v1_resumed.json
```

Actual execution used byte-identical instruments in `/private/tmp`; the archive
allows inspection. The paid runtime's Python/project source is also preserved
locally in the ignored `evaluation/reports/phase3_deepseek_flash_execution_source_v1.tar.gz`,
whose members reproduce the source fingerprint below (archive SHA
`bd8cb7e168143f649678dd1bd784c051e1057f1b99489cbeab3369a703dee373`).
Source/assets/data/contracts/gold were identical across the original and resumed
segments. Runtime: Python3.12.11, SQLite3.50.4, SQLGlot28.10.1, Pydantic2.13.4.
The dirty worktree reflects the uncommitted three-phase implementation.

```json
{
  "source_sha256": "baa285d35e8813723e4ccff418b12d4f81f52aaf58f6c90558f2457a3f713001",
  "assets_sha256": "06ad2b4d54be3939d1e6bed8641c72b18fcb7192d9e88d07967e5cc86801d2b3",
  "datasets_sha256": {
    "anime_streaming": "e5c53343b0d3cee46cbc02610353c60e2b909fd61c1e1fee16c222771e7ecfb8",
    "retail_orders": "04a18a5d5bc8118eae21b88a4159c1857f3caf86d5b0eb527e41a395f855543f",
    "support_tickets": "95829871b3525ed67256cfa7297c01c11d4ade01cacfe6a0436769b89e6065ad"
  },
  "contracts_sha256": {
    "anime_streaming": {
      "semantic_model": "fa6d790e860c2481104fe2bab3a18f3d0809e1bcb20c6f09d9be339b782d1124",
      "sql_policy": "a6817e5927206348ba833acc886ae8fdacd452f585ed1d489a3c86ecd1d00a01"
    },
    "retail_orders": {
      "semantic_model": "46238e583ffb49136286b0566a032eddfbf6981b4f51376a0c44a309d995b167",
      "sql_policy": "70ca076acf9ef843cc71d60b2661689abe2f803e9afc75f8e47402d9e726b27a"
    },
    "support_tickets": {
      "semantic_model": "66ca8903dd1406214dbd17239be34c86d88d8996fd1d679d6da41714ba575c9c",
      "sql_policy": "003e02d935e1d5f5059dabf0b3606b7444e7ab1cdbbbf0f7c1be0aa0cd5c625f"
    }
  },
  "gold_sha256": "03e024632732f1b5bc6186ce25c5b24ea51d1d773af8a3e69c8df6b6ca8b0778",
  "threshold_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "git_head": "d1e1e4f8ef95dfdf16bf7206ddcb797fa881eb4a",
  "dirty": true
}
```

Offline audit commands, executed after development, use a fresh output path:

```bash
.venv/bin/python -B scripts/freeze_live_tasks.py --check
.venv/bin/python -B scripts/freeze_live_tasks.py --source-contract --check
.venv/bin/python -B scripts/freeze_live_tasks.py --correct-questions --check
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/audit_paired_benchmark.py \
  evaluation/reports/phase3_deepseek_flash_candidates_live_v1.json \
  --reference-tasks-dir evaluation/live_tasks --tasks-dir evaluation/live_tasks_v2 \
  --usage evaluation/reports/phase3_deepseek_flash_candidates_live_v1.provider_usage.jsonl \
  --prices evaluation/deepseek_flash_cny_2026_10_11.json \
  --exclude-task live_retail_orders_regression_4 \
  --exclusion-reason 'The term outlet overlaps the concrete store_format=Outlet, while the frozen reference ranks all store formats.' \
  --report evaluation/reports/phase3_deepseek_flash_candidates_live_v1.source_v2.json
# Same auditor arguments for the skill report, using skills_live_v1_resumed in all three paths.
LOG_LEVEL=CRITICAL .venv/bin/python -B evaluation/observers/engine_replay_v1.py \
  evaluation/reports/phase3_deepseek_flash_candidates_live_v1.json \
  --report evaluation/reports/phase3_deepseek_flash_candidates_live_v1.engine_audit_checked.json
# Same replay for skills_live_v1_resumed: 179/179 passing queries matched.
```

Both source audits recomputed 230 cases, with zero verdict/reported-token mismatches.
Scoring is versioned separately from the preserved execution provenance:

```json
{
  "script_sha256": "22502440c9473e8c0f1ef36dfbb52da8786b1d0fab4f6d822a2fa7cd4407208d",
  "evaluator_sha256": "a6c68efb33b0be5dd0cc2a319eedef829aa7d8c70ffa1a394c519e01b091835a",
  "gold_sha256": "fec4d0b0713cac8d74178874bdf3b559bb92ba4a710fcf0660af6448730de5b0",
  "price_sha256": "4791edfc2b167cad3ad7bf305c17dc8050b59da727394a895225c7a9224e26d0",
  "versions": {
    "python": "3.12.11",
    "sqlite": "3.50.4",
    "sqlglot": "28.10.1",
    "pydantic": "2.13.4"
  }
}
```

Full original/raw and supplementary JSON files are gitignored. The numbers,
commands and versions in this tracked section are the durable evaluation record.

Final post-development verification (2026-10-11): `./init.sh` exited 0 with
1,078 tests in 95.934 seconds, 25 skipped and zero failures. `make check` exited 0:
repository checks and 13/13 offline acceptance checks passed, including 1,078 tests
in 98.705 seconds (25 skipped, zero failures). The fresh three-split tier-1 gate
passed 32/32; the fresh diagnostic gate passed 3/3. Both use ScriptedModel and
unchanged thresholds. `git diff --check` passed. Exact commands and logs are
recorded in the [roadmap's final verification](optimization_roadmap.md#补充开发最终验收).
