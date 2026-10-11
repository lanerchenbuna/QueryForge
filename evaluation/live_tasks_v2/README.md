# Live tasks v2: source-aware post-hoc contracts

These are the same 23 questions, reference SQL, databases and output contracts
as [live v1](../live_tasks/README.md). No new model generation is involved.
`required_sql_tables` adds an independent physical-source coverage check to
saved traces. This rule was added after inspecting v1 model outputs; scores
under it are **post-hoc audit results**, not a prespecified new paid experiment.
The original reports and verdicts must be retained.

The ranking question retains a known v1 ambiguity: "outlet" can denote the
specific `store_format='Outlet'`, while the reference SQL ranks all stores.
Report the full scores and a separate sensitivity analysis excluding this one
task with an explicit reason. Do not silently remove it or blame the model for
that interpretation. [Live v3](../live_tasks_v3/README.md) corrects the wording
for future generation; no v3 live results are claimed.

```bash
.venv/bin/python -B scripts/freeze_live_tasks.py --source-contract --check
.venv/bin/python -B scripts/freeze_live_tasks.py --check
```

Coverage follows selected SQLGlot scopes, including active CTEs, derived tables,
scalar subqueries and set-operation branches. An unused CTE and a table alias
cannot establish a required physical table. Missing SQL and unsupported shapes
are unverified. This version supports ASCII SQLite identifiers in the main
namespace; other namespaces, recursive CTEs and source functions are unverified.

Source coverage is necessary but insufficient for semantic correctness: it does
not prove the right filter, join, grain or metric expression. It does not change
production runtime policy or its SQL execution boundary.

`scripts/audit_paired_benchmark.py` first reproduces every original verdict,
verifies manifest/gold completeness and reconciles numeric provider usage.
It then reports the additional source contract separately, preserves both
verdicts and records input/scoring hashes. It refuses to overwrite a report,
and has no model/network execution path. Cost is priced measured usage, with
unknown costs left null; prices must match the model and frozen time window.
