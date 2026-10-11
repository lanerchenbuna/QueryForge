# Live paired tasks, v1

These 23 workflow tasks cover the same three schemas and unchanged reference
SQL/data as the original control-flow fixtures in `evaluation/tasks/`. The
original fixtures and thresholds remain unchanged.

The first DeepSeek preflight exposed a question/oracle mismatch: “List playback
devices” does not ask for the count column frozen in its reference SQL. These
live questions state every requested measure, limit, ordering and column name.
Each task declares its full result projection, outer row order and empty policy.
The reference SQL is evaluator input; it is never supplied to the real model.

```bash
.venv/bin/python -B scripts/freeze_live_tasks.py --check
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/run_paired_benchmark.py --tier 3 \
  --provider deepseek --model deepseek-flash --tasks-dir evaluation/live_tasks \
  --feature candidates --repeat 5 --dry-run --report /tmp/live-candidates-manifest.json
```

`freeze_live_tasks.py` independently executes the unchanged SQL read-only,
checks its values against the original frozen rows, and audits split/schema
isolation before writing. Review question and output-contract changes as gold
changes, rather than changing reference SQL to match a model's answer.

An offline scripted run can fail these tasks: explicit business language may
activate existing metric gates or the deterministic multi-metric route. Those
refusals are product behavior to investigate; they do not justify removing a
task, relaxing a result contract or labelling the refusal a model error.
This small curated set does not establish population generalization, and its
end-to-end task pass rate is not SQL-generation accuracy alone.

Real evaluations require an authorized, funded account. Measured usage and
cached-input pricing must be separated from estimated tokens and invoices.
