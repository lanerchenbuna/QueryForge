# Live tasks v3: corrected future questions

This corpus keeps all 23 reference SQL queries, databases, required output
columns and source contracts. Only the store-ranking question changes from
v1/v2: it explicitly requests **every store regardless of store_format**.
The old word "outlet" overlapped the concrete `Outlet` format and could
reasonably imply filtering to that subset, contrary to the all-store oracle.

```bash
.venv/bin/python -B scripts/freeze_live_tasks.py --correct-questions --check
```

Use this corrected corpus for future, separately authorized model runs. No v3
model outputs were generated during the 2026-10-11 live-v1 experiments. Changing
a question cannot fix or justify regrading an old model answer. The offline
audit therefore rejects any v1/v2 question or result-contract change, retains
the original full scores, and reports a separate sensitivity analysis excluding
the ambiguous ranking task with an explicit rationale.

As in [live v2](../live_tasks_v2/README.md), the added source rule is a necessary
check, not proof of all SQL semantics, and does not modify runtime policy.
