# Changelog

All notable changes to QueryForge will be documented here.

The format follows Keep a Changelog principles. The project is currently pre-release
and does not yet claim semantic-versioning stability.

## Unreleased

### Added

- Mandatory, reviewable semantic layers for data-asset publication and queries.
- Atomic data-and-semantic publication with rollback on contract failure.
- Semantic scaffolding, incremental model building, drift baselines, and weekly audits.
- A deterministic 370,762-row anime platform dataset and 120-case NL2SQL gold set.
- QueryForge Studio for data onboarding, semantic modeling, governed analysis,
  trust inspection, and persistent run history.
- D1/R2-backed atomic uploads that require a reviewed semantic contract.
- GitHub Actions quality gates and repository contribution/security metadata.
- Unified terminal outcomes (`succeeded`, `needs_clarification`, `blocked`,
  `partial`, `failed`, `cancelled`) derived in one place, so a run's persisted
  status, its delivery report and its event stream cannot disagree.
- A deterministic 32-task agent benchmark over three independent SQLite schemas,
  with split separation, repeats, an ablation switch and a frozen effect gate.
- A 21-case multi-turn and compound-intent gold set that separates capability
  failures from infrastructure failures.
- Run-budget coverage of every model call, including a persisted usage record and a
  `RunContext` carrying data, semantic and policy versions.
- `docs/nl2sql_evaluation.md` now records the frozen real-model baselines and the
  `--skill-mode auto|off` and candidate-count ablations, with their caveats.
- `./init.sh` — a single offline verification entry point (environment, full test
  suite, repository state).

### Changed

- Canonical CLI implementation now lives in `queryforge.cli`; root `main.py` remains
  a compatibility launcher.
- Workspace-relative paths (`.queryforge/` state, sample data, evaluation sets) are
  resolved by `queryforge.core.paths` from `QUERYFORGE_ROOT`, an enclosing source
  checkout, or the working directory — instead of `Path(__file__).parents[N]`, which
  pointed into `site-packages` after an install. Packaged resources such as
  `bundled_skills/` deliberately still resolve relative to the package.
- `SQLiteConnector.capabilities` is taken from the single frozen capability matrix
  rather than rebuilt from dataclass defaults, so a reader of that attribute now
  sees what the adapter actually enforces.
- `DatabaseTool.last_policy_decision` is read-only: it records decisions for calls
  made through that tool and can no longer be overwritten by a caller, which had let
  an audit record describe a call that never happened.
- Automatic skill selection is now measured rather than assumed: it costs p50
  latency +139% and output tokens +44% with no demonstrated accuracy benefit, so it
  is switchable and the question is recorded as open.
- For complex requests the candidate count is no longer raised implicitly; parallel
  candidates are an explicit opt-in.
- The evaluator compares result projections tolerantly and classifies account-level
  failures as environment errors instead of model errors, and reports measured token
  usage rather than a character-count estimate.
- `make` targets prefer the repository virtualenv, so `make check` runs the same
  interpreter as `./init.sh` instead of whatever `python` is first on `PATH`.

### Fixed

- Correct answers were scored wrong when the model returned extra columns or an
  equivalent shape; correctness is now decided by semantic result equivalence.
- A reflection step that asked for clarification discarded the answer and the run's
  artifacts; it now terminates as a structured `needs_clarification`.
- Chinese follow-up questions were not recognised, so multi-turn context was lost.
- A depleted provider balance was recorded as a model failure, corrupting accuracy
  denominators; it is now an environment error, excluded from those denominators.
- The `reasoning` audit payload was silently dropped when the model returned
  type-compatible-by-intent but schema-incompatible shapes (string lists, the string
  `"None"`, word confidences); it is now normalised, and a discarded payload is
  reported rather than ignored.
- Preview execution evaluated a rewritten statement, so a `require_limit` policy was
  satisfied by the injected `LIMIT` rather than by the caller's SQL.
- Truncated result sets overwrote the row count, hiding that the bound had been hit;
  `truncated` and `fetched_row_count` now preserve both numbers.
- Time-filter validation ignored `BETWEEN` and per-call time boundaries, so an
  unbounded or partially bounded time filter could validate as complete.
- Studio uploads accepted `.sqlite`/`.db` files that publication could never accept;
  the accepted set is now csv/parquet, and rejected database files explain why.
- The evaluator's isolation guard (independently graded code must not import the
  runtime it grades) is now recursive and covers plain `import` statements.
