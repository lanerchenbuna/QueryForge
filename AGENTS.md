# AGENTS.md

Project harness for agent-assisted development on **QueryForge** — a governed
NL2SQL / data-analysis platform (Python 3.11/3.12 + Next.js Studio).

Keep this file short. Project facts live in `docs/`; this file is routing and invariants.

## Startup Workflow

Before writing code:

1. **Confirm the working directory is the repository root** with `pwd` — the directory
   containing `pyproject.toml` and this file
2. **Read this file** completely
3. **Run `./init.sh`** — checks the environment, runs the full offline test suite, reports
   repository state. Exits non-zero on failure.
4. **Read the routed docs** for the area you are touching (see "Where Facts Live")

If baseline verification fails, **repair that first**. Do not add scope on top of a red baseline.

## Invariants — Do Not Violate

These are load-bearing. Any change that weakens them is a regression, not a refactor.

1. **Single SQL execution boundary.** `DatabaseTool` is the only sanctioned SQL execution
   path. `DatabaseAdapter.execute_sql` is a *trusted primitive* that performs no policy
   check — never call it directly from application code.
2. **Deterministic gates outrank models.** Final truth for these is decided by code, never by
   an LLM verdict:
   - `SQLPolicyEngine` (`queryforge/domain/security/sql_policy.py`) — AST policy
   - `SemanticSQLValidator` (`queryforge/domain/semantic/sql_validator.py`) — business semantics
   - `EvidenceStore` (`queryforge/domain/analysis/evidence.py`) — evidence ids
   - `QualityGateEvaluator` (`queryforge/orchestration/gates.py`) — phase gates
   - Terminal run status (`queryforge/core/outcomes.py`)
   A model may *propose*. It may not declare success.
3. **Fail closed.** Unknown table/column, unsupported shape, or unverifiable claim must
   surface as `unsupported` / `blocked` / `unverified` — never silently as `success`.
4. **Read-only by default.** Analysis opens the database read-only; write/admin SQL is
   rejected at the AST layer *and* at the engine layer.

## Working Rules

- **No completion claim without evidence.** Run the relevant verification command and report
  its actual output. "Should work" is not evidence.
- **Stay in scope.** Do not opportunistically refactor unrelated modules.
- **Prefer existing mechanisms over new ones.** Tool permissions, `PlanValidator`, the
  ablation switches, the evidence layer and journal/resume already exist — check before
  adding a mechanism.
- **Leave the repo verifiable.** `./init.sh` must pass when you stop.

## Verification Commands

```bash
./init.sh                          # full: environment + test suite + repo state

# Individual checks (from the venv)
LOG_LEVEL=CRITICAL .venv/bin/python -m unittest discover -s tests -q
make check                         # repository hygiene / required files + offline acceptance
make acceptance                    # offline acceptance only

# Evaluation (tier-1 is offline; tier-3 costs real API spend)
LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --report /tmp/tier1.json
```

**Static and build checks** — `./init.sh` does not run these; run the relevant one when you
touch that area:

```bash
make check           # repository hygiene (check_repository.py) + offline acceptance
make web-check       # Studio: eslint + tsc --noEmit + build + node --test (TypeScript)
make web-build       # Studio production build
make semantic-check  # semantic-model drift against the sample database
```

> **Tier-3 requires credentials and a funded account — ask the human before running it.**
> It spends real money. See "Known Traps" below.

## Known Traps

Each of these has already cost time or produced a wrong conclusion. Read before touching
the relevant area.

1. **`evaluation/reports/` is gitignored.** Raw evaluator JSON there is regenerable and
   untracked. Any number you want to cite must be written into a tracked document, with the
   command that produced it and the versions it depends on.
2. **Tier-1 "all green" does not measure model capability.** `scripts/benchmark_runners.py`
   swaps in `ScriptedModel`, which returns `reference_sql` verbatim. Tier-1 measures the
   governance pipeline and control flow only.
3. **Tier-3 needs a funded account.** A depleted balance returns HTTP 402. Such cases are
   classified as `environment_error` and excluded from the accuracy denominators, but the run
   still cannot complete. Confirm balance before a baseline run.
4. **A candidate/no-candidate comparison is confounded unless you force the count.** The gold
   set's per-case `candidate_selection` flag correlates perfectly with category
   (`multi_table`/`metric` set it; `single_table`/`time` never do), so grouping by it measures
   task difficulty, not the mechanism. Use `--parallel-candidates N` to hold inputs fixed.
5. **Provider credentials are provider-specific.** `load_config` reads `DEEPSEEK_API_KEY` /
   `OPENAI_API_KEY` / … as declared in `models.yml`. A generic `LLM_API_KEY` is silently
   ignored. `.env` is gitignored — never commit it.
6. **`ScriptedModel` vs real model, in the same runner.** `runner: "scripted_workflow"` tasks
   call the real model when a provider is configured (tier-3) and the fake one otherwise.
   Do not conclude from the runner name alone.
7. **Do not trust a measurement without reproducing its verdict against the data.** Several
   "semantic errors" in the first baseline were the evaluator's fault, not the model's, and a
   "parallel candidates are 3× slower" finding was pure difficulty confounding. Verify the
   per-case evidence before quoting an aggregate.
8. **Check what the evaluator is *suppressing*, not just what it measures.** It once passed
   `skills=[]` for every case, which takes the manual skill path and silently disables
   automatic skill selection — so the catalogue was inert in every measured run and the
   headline accuracy described a configuration that does not match production. Use
   `--skill-mode auto` (the default) for a production-aligned number.
9. **A run that fails in ~60 ms with zero measured tokens never called the model.** That is
   the signature of a provider-contract mismatch (for example a wrapper whose
   `generate_with_messages` lags the adapter signature), not of bad model output.
10. **A single run of ~30 cases cannot resolve small accuracy differences.** Deltas of ±0.03
    have been observed across *identical* code. Do not present such a difference as an
    improvement; increase `--repeat` instead.

## Escalation

- **Architecture decisions** → read `docs/agent_team_architecture.md` and `docs/README.md`, then ask.
- **Anything that changes an invariant above** → ask before implementing.
- **Tier-3 / any real API spend** → ask before running.
- **Repeated test failures** → report them rather than weakening the assertions.

## Where Facts Live

| Need | Doc |
|---|---|
| Documentation index | `docs/README.md` |
| Architecture overview | `docs/agent_team_architecture.md` |
| Configuration / env vars | `docs/configuration.md` |
| Semantic model authoring | `docs/semantic_authoring.md`, `docs/semantic_contracts.md` |
| REST / MCP surfaces | `docs/api_reference.md`, `docs/mcp_server.md` |
| Evaluation method + frozen numbers | `docs/nl2sql_evaluation.md` |
| Database backends | `docs/database_adapters.md` |
| Release process | `docs/github_release.md` |
