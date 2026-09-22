"""Evaluate QueryForge against multi-domain NL2SQL gold cases.

The evaluator reports exact SQL agreement separately from semantic result equivalence.
Policy precision/recall is measured over *generated* SQL: rejection probes must be
rejected by the real policy engine (with the case's sql_policy), and any legitimate
query case whose generated SQL is rejected counts as a false positive.

Result comparison policy (declared, deterministic):
- Row ORDER is ignored (multiset comparison); DUPLICATE rows are preserved —
  the comparison never deduplicates with a set.
- Column ORDER is tolerated when the column name sets match.
- Column SETS may differ when the widths differ: comparison is done on the
  columns the two projections share by name, so an answer that is correct but
  projects a different *number* of columns is not scored semantically wrong.
  The difference itself is reported per case as ``extra_columns`` /
  ``missing_columns`` and in aggregate as ``projection_difference_rate``.
  Equal widths with disjoint names keep the positional fallback, so a renamed
  column holding the same values is not penalised. Different widths *and* no
  shared column name is a different query shape and is not equivalent.
- NULL compares equal to NULL only.
- Floats: integral floats compare equal to ints; non-integral floats are
  rounded to 10 decimal places before comparison (float-noise tolerance);
  non-finite floats compare as the strings "Infinity"/"-Infinity"/"NaN".
- Every run uses an ISOLATED state root (history/orchestration/vector) so
  evaluation never pollutes production retrieval or session state.

Token and cost values are deterministic estimates, not provider-reported billing usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.application import AgentOptions, AgentService
from queryforge.core.config import Config, load_config
from queryforge.data_assets import DataAssetBuilder
from queryforge.domain.security import SQLPolicyViolation, load_sql_policy
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.infrastructure.tools.database_tool import DatabaseTool

# Comparison tolerance for non-integral floats (see module docstring).
FLOAT_COMPARISON_ROUNDING = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate QueryForge against JSONL cases.")
    parser.add_argument("--cases", required=True, help="JSONL evaluation cases")
    parser.add_argument(
        "--database",
        help="Backward-compatible default SQLite database when a case omits database",
    )
    parser.add_argument("--output", default="evaluation-results.json")
    parser.add_argument("--model-provider")
    parser.add_argument("--model")
    parser.add_argument("--semantic-model")
    parser.add_argument("--sql-policy")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--asset-state-root",
        default=".queryforge/evaluation_assets",
        help="Ignored cache root for asset:// evaluation databases",
    )
    parser.add_argument(
        "--input-cost-per-million",
        type=float,
        default=0.0,
        help="Estimated input-token USD price per million tokens",
    )
    parser.add_argument(
        "--output-cost-per-million",
        type=float,
        default=0.0,
        help="Estimated output-token USD price per million tokens",
    )
    parser.add_argument(
        "--min-execution-success",
        type=float,
        default=1.0,
        help="Exit-code gate: minimum sql_execution_success_rate (0..1)",
    )
    parser.add_argument(
        "--parallel-candidates",
        type=int,
        choices=(1, 2, 3),
        default=None,
        help="Force the candidate count for every query case, overriding the "
        "per-case candidate_selection flag. This makes the multi-candidate "
        "ablation a controlled experiment over identical inputs: run the same "
        "case set with 1 and again with 2 and compare. Without it the flag "
        "tracks case category, so a candidate/no-candidate comparison is "
        "confounded with task difficulty.",
    )
    parser.add_argument(
        "--skill-mode",
        choices=("auto", "off"),
        default="auto",
        help="How the evaluated run resolves prompt skills. 'auto' (default) matches "
        "the production path: AgentOptions.skills stays unset, so the workflow runs "
        "its automatic skill selection and may load optional skills from the "
        "catalogue. 'off' passes an empty explicit list, which disables skill "
        "loading entirely. The evaluator used to hardcode 'off' without saying so, "
        "so every skill in the catalogue except the enabled default was inert in "
        "every measured run.",
    )
    parser.add_argument(
        "--min-semantic-correct",
        type=float,
        default=0.0,
        help="Exit-code gate: minimum semantic_correctness_rate (0..1); "
        "0 disables the gate",
    )
    parser.add_argument(
        "--min-policy-recall",
        type=float,
        default=0.0,
        help="Exit-code gate: minimum policy_rejection_recall over probes (0..1); "
        "0 disables the gate",
    )
    return parser.parse_args()


def normalize_sql(sql: str | None) -> str | None:
    if not sql:
        return None
    try:
        return " ".join(DatabaseTool.validate_readonly_sql(sql).lower().split())
    except Exception:
        return " ".join(sql.lower().split())


def load_cases(path: Path, limit: int = 0) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"--cases does not exist: {path}")
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit:
        cases = cases[:limit]
    if not cases or any(not isinstance(case.get("question"), str) for case in cases):
        raise ValueError("Every JSONL item must contain a non-empty question string")
    return cases


class EvaluationEnvironment:
    """Resolve case-local databases, including reproducible asset-built databases."""

    def __init__(self, asset_state_root: str | Path) -> None:
        self.asset_state_root = Path(asset_state_root).expanduser().resolve()
        self._asset_databases: dict[str, tuple[Path, Path]] = {}

    def resolve(
        self,
        case: dict[str, Any],
        default_database: str | None,
        default_semantic_model: str | None,
    ) -> tuple[Path, Path | None]:
        database_ref = str(case.get("database") or default_database or "").strip()
        if not database_ref:
            raise ValueError(f"Case {case.get('id')!r} has no database")
        if database_ref.startswith("asset://"):
            return self._build_asset_database(database_ref.removeprefix("asset://"))
        database = _project_path(database_ref)
        if not database.is_file():
            raise ValueError(f"Case database does not exist: {database}")
        semantic_ref = case.get("semantic_model") or default_semantic_model
        return database, _project_path(semantic_ref) if semantic_ref else None

    def _build_asset_database(self, config_ref: str) -> tuple[Path, Path]:
        config = _project_path(config_ref)
        cache_key = hashlib.sha256(str(config).encode()).hexdigest()[:12]
        if cache_key not in self._asset_databases:
            root = self.asset_state_root / cache_key
            database = root / "analytics.sqlite"
            state_root = root / "state"
            if root.exists():
                shutil.rmtree(root)
            results = DataAssetBuilder(database, state_root).build_from_file(config)
            failures = [result.error for result in results if result.status != "success"]
            if failures:
                raise ValueError(f"Could not prepare asset evaluation database: {failures}")
            self._asset_databases[cache_key] = (
                database,
                state_root / "semantic_assets.yml",
            )
        return self._asset_databases[cache_key]


def evaluate_cases(
    cases: list[dict[str, Any]],
    *,
    service: AgentService,
    environment: EvaluationEnvironment,
    default_database: str | None = None,
    default_semantic_model: str | None = None,
    default_sql_policy: str | None = None,
    model_provider: str | None = None,
    model: str | None = None,
    input_cost_per_million: float = 0.0,
    output_cost_per_million: float = 0.0,
    usage_log: list[dict[str, int]] | None = None,
    parallel_candidates_override: int | None = None,
    skill_selection: list[str] | None = None,
) -> dict[str, Any]:
    results = []
    for index, case in enumerate(cases, start=1):
        if usage_log is not None:
            usage_log.clear()
        database, semantic_model = environment.resolve(
            case, default_database, default_semantic_model
        )
        if case.get("expected_outcome") == "policy_rejection":
            result = _evaluate_policy_probe(
                case, database, default_sql_policy=default_sql_policy
            )
        else:
            result = _evaluate_query(
                case,
                service=service,
                database=database,
                semantic_model=semantic_model,
                default_sql_policy=default_sql_policy,
                model_provider=model_provider,
                model=model,
                index=index,
                usage_log=usage_log,
                parallel_candidates_override=parallel_candidates_override,
                skill_selection=skill_selection,
            )
        # Token accounting: prefer provider-reported usage over the character
        # heuristic. ``_estimate_tokens`` measured only the question text (a few
        # dozen characters), so the reported average came out around 10 "input
        # tokens" while the real prompt is thousands — the number was not merely
        # imprecise, it was off by two orders of magnitude and could not support
        # any cost bound. Measured usage is recorded when the run reports it, and
        # the source is stated explicitly so a report can never be read as if the
        # estimate were billing data.
        usage = _measured_usage(result, usage_log)
        if usage is not None:
            input_tokens = usage["input_tokens"]
            output_tokens = usage["output_tokens"]
            result["token_source"] = "measured"
        else:
            input_tokens = _estimate_tokens(case["question"])
            output_tokens = _estimate_tokens(
                str(result.get("sql") or "")
                + "".join(
                    str(candidate.get("sql") or "")
                    for candidate in result.get("candidates", [])
                )
            )
            result["token_source"] = "estimated"
        result["estimated_input_tokens"] = input_tokens
        result["estimated_output_tokens"] = output_tokens
        result["estimated_cost_usd"] = round(
            (input_tokens * input_cost_per_million + output_tokens * output_cost_per_million)
            / 1_000_000,
            8,
        )
        # Identity fingerprint from the ORIGINAL case definition (not from the
        # generated output), so uniqueness statistics are stable across runs.
        identity = {
            "question": case.get("question") or "",
            "expected_sql": case.get("expected_sql") or case.get("policy_probe_sql") or "",
        }
        result["failure_class"] = classify_outcome(result)
        result["failure_detail"] = failure_detail(result)
        result["case_fingerprint"] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        results.append(result)
    return _build_report(
        results,
        parallel_candidates_override=parallel_candidates_override,
        cases=cases,
        default_semantic_model=default_semantic_model,
        default_database=default_database,
    )


def isolated_config(config: Config, state_root: str | Path) -> Config:
    """Redirect every evaluator state path into an isolated root.

    Evaluation must never write into production SQL history, sessions, run
    artifacts, or the vector knowledge base.
    """
    root = Path(state_root).expanduser().resolve() / "isolated"
    return replace(
        config,
        history_db_path=str(root / "history.db"),
        orchestration_state_root=str(root / "runs"),
        vector_kb_path=str(root / "lancedb"),
    )


def _evaluate_query(
    case: dict[str, Any],
    *,
    service: AgentService,
    database: Path,
    semantic_model: Path | None,
    default_sql_policy: str | None,
    model_provider: str | None,
    model: str | None,
    index: int,
    usage_log: list[dict[str, int]] | None = None,
    parallel_candidates_override: int | None = None,
    skill_selection: list[str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    session_id = f"evaluation_{case.get('id', index)}" if case.get("follow_up_context") else None
    try:
        for turn, prior_question in enumerate(case.get("follow_up_context", []), start=1):
            service.ask(
                prior_question,
                AgentOptions(
                    database=str(database),
                    semantic_model_path=str(semantic_model) if semantic_model else None,
                    sql_policy_path=case.get("sql_policy") or default_sql_policy,
                    model_provider=model_provider,
                    model=model,
                    skills=skill_selection,
                    session_id=session_id,
                    run_id=f"evaluation_warmup_{index}_{turn}",
                ),
            )
        # Latency measures the evaluated turn only; warmup turns are excluded.
        started = time.perf_counter()
        output = service.ask(
            case["question"],
            AgentOptions(
                database=str(database),
                semantic_model_path=str(semantic_model) if semantic_model else None,
                sql_policy_path=case.get("sql_policy") or default_sql_policy,
                model_provider=model_provider,
                model=model,
                skills=skill_selection,
                session_id=session_id,
                parallel_candidates=(
                    parallel_candidates_override
                    if parallel_candidates_override is not None
                    else (2 if case.get("candidate_selection") else 1)
                ),
                run_id=f"evaluation_{index}",
            ),
        )
        oracle_started = time.perf_counter()
        expected_columns, expected_rows = _execute_expected(
            database, case.get("expected_sql")
        )
        oracle_latency_ms = round((time.perf_counter() - oracle_started) * 1000, 3)
        # Policy outcome of the *generated* SQL: rejecting a legitimate query is
        # a false positive; trusting the expected SQL would make precision
        # tautologically 1.0.
        policy_rejected = _generated_policy_outcome(output)
        semantic_correct = _semantic_equivalent(
            output.get("rows", []),
            output.get("columns"),
            expected_rows,
            expected_columns,
        )
        extra_columns, missing_columns = _projection_difference(
            output.get("columns"), expected_columns
        )
        selection = output.get("candidate_selection") or {}
        candidates = selection.get("candidates", [])
        first_candidate_correct = _candidate_correct(
            candidates[0].get("sql") if candidates else None,
            database,
            expected_rows,
            expected_columns,
        )
        return {
            "id": case.get("id", index),
            "domain": case.get("domain", "default"),
            "category": case.get("category", "unspecified"),
            "expected_outcome": "query",
            "status": output.get("status"),
            "execution_success": output.get("status") == "success",
            # Coverage markers, derived from the case definition (not the output)
            # so they are stable across runs and survive a failed case.
            "multi_turn": bool(case.get("follow_up_context")),
            "requires_context": bool(case.get("requires_context")),
            "compound": bool(case.get("compound")),
            "environment_error": False,
            "usage": dict(usage_log[-1]) if usage_log else None,
            "semantic_correct": semantic_correct,
            "extra_columns": extra_columns,
            "missing_columns": missing_columns,
            # False when the comparison could not use column names (either side
            # missing them) and fell back to positional values. Projection
            # difference is only meaningful when names were available.
            "columns_compared": bool(output.get("columns")) and bool(expected_columns),
            "sql_exact_match": normalize_sql(output.get("sql"))
            == normalize_sql(case.get("expected_sql")),
            "policy_rejected": policy_rejected,
            "policy_expected_rejection": False,
            "row_count": output.get("row_count"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            # SQL execution time of the *generated* query, recorded separately
            # from end-to-end latency. Candidate selection can only make the
            # engine part faster, and the engine part is tiny, so this field is
            # what settles whether "a faster candidate" could ever matter.
            "sql_execution_ms": output.get("sql_execution_duration_ms"),
            "oracle_latency_ms": oracle_latency_ms,
            "sql": output.get("sql"),
            "selected_index": selection.get("selected_index"),
            "candidates": candidates,
            "first_candidate_semantic_correct": first_candidate_correct,
            "candidate_selection_used": bool(candidates),
            # Which prompt skills the run actually loaded, and how they were
            # chosen. Without this a skill ablation cannot be audited at all: the
            # only trace was inside a log line, and a benchmark that silently
            # suppresses selection looks identical to one where selection ran and
            # chose nothing.
            "loaded_skills": list(output.get("skills_used") or []),
            # Whether the reflective second model call was skipped, and on what
            # basis. Without these two fields a conditional-reflection change
            # cannot be verified at all: a run that skipped reflection and a run
            # that reflected successfully look identical from the outside.
            "reasoning_confidence": (
                (output.get("reasoning") or {}).get("confidence")
                if isinstance(output.get("reasoning"), dict)
                else None
            ),
            # The two conditions that most often keep the self-verification gate
            # closed. Recorded so a gate that never fires can be explained rather
            # than guessed at.
            "reasoning_assumptions": len(
                (output.get("reasoning") or {}).get("assumptions") or []
            )
            if isinstance(output.get("reasoning"), dict)
            else None,
            "reasoning_risks": len(
                (output.get("reasoning") or {}).get("risks") or []
            )
            if isinstance(output.get("reasoning"), dict)
            else None,
            "skill_selection_mode": (output.get("skill_selection") or {}).get("mode"),
            "skill_selection_reason": (output.get("skill_selection") or {}).get("reason"),
            "model_provider": output.get("model_provider"),
            "model": output.get("model"),
            "error": None,
        }
    except Exception as exc:
        return {
            "id": case.get("id", index),
            "domain": case.get("domain", "default"),
            "category": case.get("category", "unspecified"),
            "expected_outcome": "query",
            "status": "failed",
            "execution_success": False,
            "multi_turn": bool(case.get("follow_up_context")),
            "requires_context": bool(case.get("requires_context")),
            "compound": bool(case.get("compound")),
            # Present on the failure path too, so a skipped or discarded reasoning
            # payload does not vanish from the bucket counts when the run fails.
            "reasoning_confidence": None,
            "reasoning_discarded": None,
            # A transport/provider failure is not a model-quality failure. A
            # depleted balance (HTTP 402) previously landed here as a plain
            # "failed" and dragged sql_execution_success_rate from 1.00 to 0.75,
            # which reads as a capability regression. Such cases are flagged and
            # excluded from the accuracy metrics instead.
            "environment_error": is_environment_error(exc),
            "environment_error_reason": (
                _environment_error_reason(exc) if is_environment_error(exc) else None
            ),
            "usage": dict(usage_log[-1]) if usage_log else None,
            "semantic_correct": False,
            "sql_exact_match": False,
            "policy_rejected": None,
            "policy_expected_rejection": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "sql": None,
            "candidates": [],
            "candidate_selection_used": False,
            "columns_compared": False,
            "extra_columns": [],
            "missing_columns": [],
            "error": str(exc),
        }


def _evaluate_policy_probe(
    case: dict[str, Any],
    database: Path,
    *,
    default_sql_policy: str | None = None,
) -> dict[str, Any]:
    """Run one rejection probe through the real policy engine.

    The probe is evaluated against the same ``DatabaseTool`` governance path
    used at execution time, with the case's ``sql_policy`` (or the default),
    so table/column scope, LIMIT budgets, and join rules are actually measured.
    """
    started = time.perf_counter()
    probe = str(case.get("policy_probe_sql") or "")
    if not probe:
        return {
            "id": case.get("id"),
            "domain": case.get("domain", "default"),
            "category": "policy_rejection",
            "expected_outcome": "policy_rejection",
            "status": "error",
            "policy_rejected": None,
            "policy_expected_rejection": True,
            "policy_rule": None,
            "policy_name": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "sql": probe,
            "candidates": [],
            "candidate_selection_used": False,
            "error": "policy_probe_sql is missing",
        }
    rejected = False
    rule: str | None = None
    policy_name: str | None = None
    error: str | None = None
    try:
        policy_path = (
            _project_path(str(case.get("sql_policy")))
            if case.get("sql_policy")
            else (
                _project_path(default_sql_policy)
                if default_sql_policy
                else None
            )
        )
        policy, policy_source = load_sql_policy(policy_path)
        policy_name = policy.name
        with SQLiteConnector(str(database)) as connector:
            tool = DatabaseTool(
                connector, policy, policy_source_path=policy_source
            )
            try:
                decision = tool.policy_engine.evaluate(probe)
                rule = decision.rule
            except SQLPolicyViolation as exc:
                rejected = True
                rule = exc.decision.rule
    except Exception as exc:
        error = str(exc)
    return {
        "id": case.get("id"),
        "domain": case.get("domain", "default"),
        "category": "policy_rejection",
        "expected_outcome": "policy_rejection",
        "status": (
            "error" if error else "rejected" if rejected else "accepted"
        ),
        "policy_rejected": rejected,
        "policy_expected_rejection": True,
        "policy_rule": rule,
        "policy_name": policy_name,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "sql": probe,
        "candidates": [],
        "candidate_selection_used": False,
        "error": error,
    }


def _schemas_for(database: Path) -> list[Any]:
    with SQLiteConnector(str(database)) as connector:
        tool = DatabaseTool(connector)
        return [tool.describe_table(name) for name in tool.list_tables()]


def _governance_coverage(
    cases: list[dict[str, Any]],
    default_semantic_model: str | None,
    default_database: str | None,
) -> dict[str, Any]:
    """How many cases the business-semantic layer can actually see.

    ``SemanticSQLValidator.for_context`` returns None when the question matches no
    governed metric, which means the entire semantic gate is skipped for that run:
    unknown tables and columns are still caught by table scope, but nothing checks
    grain, join keys, fan-out or default filters.

    Metric matching is deterministic term matching, so a follow-up that names no
    metric ("Break that down by region.") is exactly the case that needs context
    and also exactly the case the semantic layer cannot see. Measured on this
    repository: 11 of 12 context-dependent cases and 8 of 12 of the checked-in
    multi-turn cases match no governed metric, i.e. they run ungoverned.

    Reported rather than asserted: this is a coverage boundary of the semantic
    layer, not a failure of the evaluation.
    """

    from queryforge.domain.semantic import SemanticModelLoader

    ungoverned: list[str] = []
    ungoverned_requiring_context: list[str] = []
    checked = 0
    for case in cases:
        if case.get("expected_outcome") == "policy_rejection":
            continue
        model_path = case.get("semantic_model") or default_semantic_model
        database = case.get("database") or default_database
        if not model_path or not database:
            continue
        try:
            schemas = _schemas_for(Path(database))
            semantic = SemanticModelLoader.load_and_validate(
                Path(model_path), schemas, str(case.get("question") or "")
            )
            matches = SemanticModelLoader.match_metrics(
                semantic.model, str(case.get("question") or "")
            )
        except Exception:  # noqa: BLE001 - a coverage probe must never fail a run
            continue
        checked += 1
        if not matches:
            case_id = str(case.get("id"))
            ungoverned.append(case_id)
            if case.get("requires_context") or case.get("follow_up_context"):
                ungoverned_requiring_context.append(case_id)
    return {
        "cases_checked": checked,
        "ungoverned_cases": ungoverned,
        "ungoverned_count": len(ungoverned),
        #: The intersection that matters: questions that need context, and that the
        #: semantic layer therefore cannot see.
        "ungoverned_requiring_context": ungoverned_requiring_context,
        "note": (
            "A case listed here matched no governed metric, so SemanticSQLValidator "
            "was skipped for it: no grain, join-key, fan-out or default-filter check "
            "ran. Metric matching is term-based, so context-dependent follow-ups that "
            "do not restate the metric are systematically in this set."
        ),
    }


def _execute_expected(
    database: Path, sql: str | None
) -> tuple[list[str], list[list[Any]]]:
    if not sql:
        return [], []
    with SQLiteConnector(str(database)) as connector:
        result = connector.execute_sql(DatabaseTool.validate_readonly_sql(sql))
        return result.columns, result.rows


def _generated_policy_outcome(output: dict[str, Any]) -> bool | None:
    """Extract the policy outcome of the SQL that was actually generated.

    Returns True when the policy engine rejected the generated SQL (a false
    positive for a legitimate query case), False when it was allowed, and
    None when the outcome is unknown.
    """
    decisions = (output.get("sql_security") or {}).get("decisions") or []
    for decision in reversed(decisions):
        allowed = decision.get("allowed")
        if isinstance(allowed, bool):
            return not allowed
    if output.get("status") == "blocked":
        return True
    if output.get("status") == "success":
        return False
    return None


def _candidate_correct(
    sql: str | None,
    database: Path,
    expected_rows: list[list[Any]],
    expected_columns: list[str],
) -> bool | None:
    if not sql:
        return None
    try:
        columns, rows = _execute_expected(database, sql)
        return _semantic_equivalent(rows, columns, expected_rows, expected_columns)
    except Exception:
        return False


def _semantic_equivalent(
    actual_rows: list[list[Any]],
    actual_columns: list[str] | None,
    expected_rows: list[list[Any]],
    expected_columns: list[str],
) -> bool:
    """Compare result sets by content, tolerant to column order and row order.

    Comparison happens on the columns the two projections **share by name**:

    * Equal name sets in a different order are reordered and compared in full.
    * Different column counts (the defect this rule exists for) compare on the
      shared columns only, so an answer that is correct but projects a
      different *number* of columns is not scored as semantically wrong. The
      difference is reported separately by the caller (see
      ``_projection_difference``) rather than silently ignored.
    * Equal column counts with disjoint names fall back to positional
      comparison — this keeps the historical behaviour for a renamed column
      (``anime_count`` vs ``action_anime_count``) with identical values.
    * When the two projections share no column name *and* their counts differ,
      this is a different query shape, not a projection difference, so the
      result is not equivalent.

    Known limitation: a renamed column whose count also differs cannot be
    matched by name and is still scored as wrong. Alias normalisation is not
    attempted here.

    Row order is never significant and duplicate rows are preserved. When
    column names are unavailable on the actual side, comparison falls back to
    positional values (the historical behaviour).
    """
    actual_columns = list(actual_columns or [])
    if actual_columns and expected_columns:
        if set(actual_columns) == set(expected_columns):
            reordered = _reorder_columns(actual_columns, actual_rows, expected_columns)
            return _canonical_rows(reordered) == _canonical_rows(expected_rows)
        if len(actual_columns) == len(expected_columns) and not (
            set(actual_columns) & set(expected_columns)
        ):
            # Same width, completely different names: not a projection
            # difference. Keep the positional comparison so a renamed column
            # holding the same values is not newly penalised.
            return _canonical_rows(actual_rows) == _canonical_rows(expected_rows)
        actual_projection, expected_projection = _shared_column_projection(
            actual_columns, expected_columns
        )
        if not actual_projection:
            # Different widths AND no shared column name: the two results do not
            # describe the same quantity, so this is a genuine mismatch.
            return False
        actual_rows = [[row[index] for index in actual_projection] for row in actual_rows]
        expected_rows = [
            [row[index] for index in expected_projection] for row in expected_rows
        ]
    return _canonical_rows(actual_rows) == _canonical_rows(expected_rows)


def _shared_column_projection(
    actual_columns: list[str],
    expected_columns: list[str],
) -> tuple[list[int], list[int]]:
    """Index positions of the shared column names, in the expected order."""

    actual_positions: list[int] = []
    expected_positions: list[int] = []
    for position, column in enumerate(expected_columns):
        if column in actual_columns:
            expected_positions.append(position)
            actual_positions.append(actual_columns.index(column))
    return actual_positions, expected_positions


def _projection_difference(
    actual_columns: list[str] | None,
    expected_columns: list[str],
) -> tuple[list[str], list[str]]:
    """Column names the actual result has additionally / is missing.

    Reported as its own signal so projection tolerance cannot hide a wrong
    query shape: a result that shares no column name is not equivalent at all,
    and a partial overlap is still visible in the report.
    """

    actual = list(actual_columns or [])
    if not actual or not expected_columns:
        return [], []
    return (
        [column for column in actual if column not in expected_columns],
        [column for column in expected_columns if column not in actual],
    )


def _reorder_columns(
    actual_columns: list[str],
    rows: list[list[Any]],
    expected_columns: list[str],
) -> list[list[Any]]:
    if (
        actual_columns
        and expected_columns
        and len(actual_columns) == len(expected_columns)
        and actual_columns != expected_columns
        and set(actual_columns) == set(expected_columns)
    ):
        positions = [actual_columns.index(column) for column in expected_columns]
        return [[row[position] for position in positions] for row in rows]
    return rows


def _canonical_rows(rows: list[list[Any]]) -> list[str]:
    return sorted(
        json.dumps(
            [_canonical_value(value) for value in row],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for row in rows
    )


def _canonical_value(value: Any) -> Any:
    """Apply the declared numeric comparison policy (see module docstring)."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return "Infinity" if value > 0 else "-Infinity" if value < 0 else "NaN"
        if value.is_integer():
            return int(value)
        return round(value, FLOAT_COMPARISON_ROUNDING)
    return value


def _recording_model_factory(
    usage_log: list[dict[str, int]],
):
    """Wrap the model factory so provider-reported usage survives the run.

    The evaluator used to estimate tokens from the *question text*, which is a
    few dozen characters — so it reported roughly 10 input tokens while the real
    prompt is thousands. The provider already reports normalized usage
    (``last_usage`` with ``estimated=False`` when the API returned counts), so it
    is recorded here and preferred over the heuristic.
    """

    def factory(config: Any):
        return wrap_provider_for_usage(ModelFactory.create(config), usage_log)

    return factory


def wrap_provider_for_usage(model: Any, usage_log: list[dict[str, int]]) -> Any:
    """Record provider-reported usage for every model call on ``model``.

    Split out from the factory so it can be tested directly against an adapter:
    it must satisfy the *current* provider contract, and a wrapper that silently
    falls behind that contract makes every evaluated run fail before any spend.

    ``timeout`` is forwarded rather than swallowed — it carries the run's
    remaining model deadline (feat-012).
    """

    class _UsageRecordingModel(type(model)):  # type: ignore[misc]
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def generate_with_messages(self, messages, json_mode=False, timeout=None):
            result = self._inner.generate_with_messages(
                messages, json_mode=json_mode, timeout=timeout
            )
            usage = getattr(self._inner, "last_usage", None)
            if usage is not None:
                prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion = int(getattr(usage, "completion_tokens", 0) or 0)
                if prompt or completion:
                    usage_log.append(
                        {
                            "input_tokens": prompt,
                            "output_tokens": completion,
                            "estimated": bool(getattr(usage, "estimated", False)),
                        }
                    )
            return result

    return _UsageRecordingModel(model)


    return factory


def _measured_usage(
    result: dict[str, Any],
    usage_log: list[dict[str, int]] | None,
) -> dict[str, int] | None:
    """Sum the usage recorded for this case, or None when nothing was reported.

    Returns None when the provider reported nothing, and for estimates that the
    adapter had to synthesize — a char-count guess must not be presented as
    measured usage.
    """

    recorded = result.get("usage")
    entries = [recorded] if isinstance(recorded, dict) else list(usage_log or [])
    measured = [item for item in entries if item and not item.get("estimated")]
    if not measured:
        return None
    return {
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in measured),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in measured),
    }


#: Markers of a transport / account problem rather than a model-quality problem.
_ENVIRONMENT_ERROR_MARKERS: tuple[str, ...] = (
    "insufficient balance",
    "insufficient_quota",
    "quota exceeded",
    "rate limit",
    "too many requests",
    "error code: 402",
    "error code: 429",
    "error code: 401",
    "error code: 403",
    "unauthorized",
    "authentication",
    "connection error",
    "connect timeout",
    "read timeout",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "no api key is configured",
)


def _environment_error_reason(exc: BaseException) -> str | None:
    """The matched environment-failure marker, or None when it is not one."""

    text = str(exc).lower()
    for marker in _ENVIRONMENT_ERROR_MARKERS:
        if marker in text:
            return marker
    return None


#: Failure buckets. A single aggregate accuracy number over a case set this
#: varied is misleading, because it mixes "the system answered the wrong
#: question" with "a deterministic gate refused the answer" and "the system
#: correctly asked for clarification". Those need different responses, so they are
#: separated here rather than collapsed into one rate.
FAILURE_CLASSES: tuple[str, ...] = (
    "context_lost",
    "clarification_requested",
    "governance_refusal",
    "retry_exhausted",
    "policy_blocked",
    "wrong_projection",
    "wrong_value",
    "execution_failed",
    "environment_error",
)

#: Textual markers of a deterministic governance refusal in an error chain.
_GOVERNANCE_REFUSAL_MARKERS: tuple[str, ...] = (
    "semantic sql validation failed",
    "unsupported metric dimension combination",
    "governance rejected",
)


def classify_outcome(result: dict[str, Any]) -> str | None:
    """Bucket one case result. ``None`` means the case passed.

    Order matters. A governance refusal is reported as ``governance_refusal`` even
    when it *led* to retry exhaustion, because the refusal is the cause and the
    exhaustion is the symptom — reporting only the symptom would hide that the
    deterministic layer, not the model, ended the run.
    """

    if result.get("environment_error"):
        return "environment_error"
    if result.get("semantic_correct"):
        return None

    error = str(result.get("error") or "").lower()
    status = str(result.get("status") or "")

    # Structural causes are classified on their own terms, even for a
    # context-dependent case. A first version of this function swept every failure
    # of a `requires_context` case into `context_lost`, which was misleading: a
    # deterministic gate refusing the answer, or the system correctly asking for
    # clarification, is NOT a context failure. Only a confident wrong answer is.
    if status == "needs_clarification":
        # The system asked instead of answering. Checked before the
        # comparison-based buckets because a clarification run can also show a
        # column difference (the clause that ran produced rows), and asking is the
        # headline outcome.
        return "clarification_requested"
    if any(marker in error for marker in _GOVERNANCE_REFUSAL_MARKERS):
        return "governance_refusal"
    if "retry_limit" in error or "maximum sql retries" in error:
        return "retry_exhausted"
    if status == "blocked" or "policy" in error:
        return "policy_blocked"
    if result.get("requires_context"):
        # SQL ran to completion and differs from the reference: a confident wrong
        # answer on a question that needed the prior turn.
        return "context_lost"
    if status != "success":
        # SQL never produced a comparable result for a reason not covered above.
        return "execution_failed"
    # SQL ran and produced a result that differs from the reference.
    if result.get("extra_columns") or result.get("missing_columns"):
        return "wrong_projection"
    return "wrong_value"


def failure_detail(result: dict[str, Any]) -> str | None:
    """The concrete mechanism behind a bucket, for attribution inside it.

    ``context_lost`` deliberately absorbs every failure mode of a
    context-dependent case; this keeps the mechanism visible so the bucket does
    not become a place where causes go to hide.
    """

    if not result.get("failure_class"):
        return None
    error = str(result.get("error") or "").lower()
    if "semantic sql validation failed" in error:
        return "semantic_validator_refusal"
    if "unsupported metric dimension combination" in error:
        return "metric_search_refusal"
    if "retry_limit" in error or "maximum sql retries" in error:
        return "retry_exhausted"
    if "fix response repeated" in error or "fix response must contain" in error:
        return "repair_loop_stalled"
    status = str(result.get("status") or "")
    if status == "needs_clarification":
        return "clarification_requested"
    if status == "blocked":
        return "policy_blocked"
    if status != "success":
        return f"status_{status}"
    if result.get("extra_columns") or result.get("missing_columns"):
        return "wrong_projection"
    return "wrong_value"


def is_environment_error(exc: BaseException) -> bool:
    """Whether a failure is an environment/transport problem, not model quality.

    The first baseline attempt was contaminated by ``Error code: 402 -
    Insufficient Balance`` mid-run: those cases were recorded as model failures
    and lowered ``sql_execution_success_rate`` to 0.75. Re-running with a funded
    account produced 1.00 for the same code, which is the whole point — the
    number must not move because an account ran dry.
    """

    return _environment_error_reason(exc) is not None


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return round(values[lower], 3)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 3)


def _failure_class_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count failures per bucket, so one number cannot hide three causes."""

    counts: dict[str, int] = {}
    for item in results:
        bucket = item.get("failure_class") or classify_outcome(item)
        if bucket:
            counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _build_report(
    results: list[dict[str, Any]],
    *,
    parallel_candidates_override: int | None = None,
    cases: list[dict[str, Any]] | None = None,
    default_semantic_model: str | None = None,
    default_database: str | None = None,
) -> dict[str, Any]:
    query_results = [item for item in results if item["expected_outcome"] == "query"]
    # Environment failures (no balance, rate limit, transport) are excluded from
    # the accuracy denominators and counted separately, so an account running dry
    # can never be read as a capability regression.
    environment_failures = [
        item for item in query_results if item.get("environment_error")
    ]
    scored_results = [
        item for item in query_results if not item.get("environment_error")
    ]
    probe_results = [
        item for item in results if item["expected_outcome"] == "policy_rejection"
    ]
    selection_results = [item for item in query_results if item["candidate_selection_used"]]
    first_correct = [
        bool(item["first_candidate_semantic_correct"])
        for item in selection_results
        if item.get("first_candidate_semantic_correct") is not None
    ]
    selected_correct = [
        bool(item["semantic_correct"])
        for item in selection_results
        if item.get("first_candidate_semantic_correct") is not None
    ]
    # Policy metrics over *generated* SQL:
    # - TP: probe correctly rejected by the real policy engine.
    # - FN: probe that slipped through (bypass).
    # - FP: legitimate query case whose generated SQL the engine rejected.
    true_positives = sum(
        bool(item["policy_rejected"]) for item in probe_results
        if item.get("policy_rejected") is not None
    )
    false_negatives = sum(
        not bool(item["policy_rejected"]) for item in probe_results
        if item.get("policy_rejected") is not None
    )
    false_positives = sum(
        bool(item.get("policy_rejected")) for item in query_results
    )
    probe_errors = [
        item for item in probe_results if item.get("status") == "error"
    ]
    domains = sorted({str(item["domain"]) for item in results})
    # The first executed query case includes provider-client warmup; exclude it
    # from latency aggregates when other samples exist (per-case latencies stay
    # in the results).
    latency_cases = query_results[1:] if len(query_results) > 1 else query_results
    per_domain: dict[str, dict[str, Any]] = {}
    for domain in sorted({str(item["domain"]) for item in query_results}):
        domain_results = [item for item in query_results if item["domain"] == domain]
        per_domain[domain] = {
            "cases": len(domain_results),
            "sql_execution_success_rate": _rate(
                [bool(item["execution_success"]) for item in domain_results]
            ),
            "semantic_correctness_rate": _rate(
                [bool(item["semantic_correct"]) for item in domain_results]
            ),
        }
    unique_queries = {
        str(item.get("case_fingerprint")) for item in results
    }
    oracle_latencies = [
        float(item["oracle_latency_ms"])
        for item in query_results
        if item.get("oracle_latency_ms") is not None
    ]
    return {
        "case_count": len(results),
        "query_count": len(query_results),
        "probe_count": len(probe_results),
        "unique_case_count": len(unique_queries),
        "domains": domains,
        "coverage": {
            category: sum(item["category"] == category for item in results)
            for category in sorted({str(item["category"]) for item in results})
        },
        # Explicit buckets so a coverage gap is visible in the report instead of
        # having to be derived by hand. `multi_turn` counts cases that carry
        # follow_up_context; `requires_context` counts the stricter subset whose
        # question cannot be answered without it (an elliptical or referential
        # follow-up), which is the bucket that actually measures context handling
        # rather than merely exercising a multi-turn code path.
        "coverage_buckets": {
            "total": len(results),
            "query": len(query_results),
            "policy_rejection": len(probe_results),
            "multi_turn": sum(1 for item in results if item.get("multi_turn")),
            "requires_context": sum(
                1 for item in results if item.get("requires_context")
            ),
            "compound": sum(1 for item in results if item.get("compound")),
        },
        "governance_coverage": _governance_coverage(
            cases or [], default_semantic_model, default_database
        ),
        "failure_classes": _failure_class_counts(results),
        "per_domain": per_domain,
        "metrics": {
            # Environment failures are reported, counted and excluded from every
            # accuracy rate below: an unfunded account must not look like a
            # capability regression.
            "environment_error_cases": len(environment_failures),
            "environment_error_ids": [
                item.get("id") for item in environment_failures
            ],
            "scored_case_count": len(scored_results),
            "sql_execution_success_rate": _rate(
                [bool(item["execution_success"]) for item in scored_results]
            ),
            "semantic_correctness_rate": _rate(
                [bool(item["semantic_correct"]) for item in scored_results]
            ),
            # Whether the token numbers come from provider usage or from the
            # character heuristic. "estimated" means the figures cannot support a
            # cost bound.
            "token_source": (
                "measured"
                if any(item.get("token_source") == "measured" for item in results)
                else "estimated"
            ),
            "token_source_measured_cases": sum(
                1 for item in results if item.get("token_source") == "measured"
            ),
            # Projection tolerance (see module docstring): semantic_correctness_rate
            # compares the shared columns, so a differing projection is reported
            # here instead of being silently absorbed into the correctness rate.
            "projection_difference_rate": _rate(
                [
                    bool(item.get("extra_columns") or item.get("missing_columns"))
                    for item in query_results
                    if item.get("columns_compared")
                ]
            ),
            "projection_difference_cases": sum(
                1
                for item in query_results
                if item.get("extra_columns") or item.get("missing_columns")
            ),
            "sql_exact_match_rate": _rate(
                [bool(item["sql_exact_match"]) for item in scored_results]
            ),
            "policy_rejection_precision": round(
                true_positives / (true_positives + false_positives), 6
            )
            if true_positives + false_positives
            else None,
            "policy_rejection_recall": round(
                true_positives / (true_positives + false_negatives), 6
            )
            if true_positives + false_negatives
            else None,
            "policy_true_positives": true_positives,
            "policy_false_positives": false_positives,
            "policy_false_negatives": false_negatives,
            "policy_probe_errors": len(probe_errors),
            "p50_latency_ms": _percentile(
                [float(item["latency_ms"]) for item in latency_cases], 0.50
            ),
            "p95_latency_ms": _percentile(
                [float(item["latency_ms"]) for item in latency_cases], 0.95
            ),
            "latency_warmup_excluded_case": (
                query_results[0].get("id") if query_results else None
            ),
            "average_service_latency_ms": _average(
                [float(item["latency_ms"]) for item in query_results]
            ),
            "average_oracle_latency_ms": _average(oracle_latencies),
            # Engine time of the generated query. Bounded by the oracle latency,
            # i.e. microseconds-to-milliseconds against a multi-second run — the
            # field that settles whether "a faster candidate" could ever matter.
            "average_sql_execution_ms": _average(
                [
                    float(item["sql_execution_ms"])
                    for item in query_results
                    if item.get("sql_execution_ms") is not None
                ]
            ),
            "sql_execution_measured_cases": sum(
                1 for item in query_results if item.get("sql_execution_ms") is not None
            ),
            "average_estimated_input_tokens": _average(
                [int(item["estimated_input_tokens"]) for item in results]
            ),
            "average_estimated_output_tokens": _average(
                [int(item["estimated_output_tokens"]) for item in results]
            ),
            "average_estimated_cost_usd": _average(
                [float(item["estimated_cost_usd"]) for item in results]
            ),
            "candidate_selection_uplift": (
                round((_rate(selected_correct) or 0) - (_rate(first_correct) or 0), 6)
                if first_correct
                else None
            ),
            "candidate_selection_cases": len(first_correct),
            # Stated so an ablation run is self-describing: None means the
            # per-case candidate_selection flag was honoured (which tracks case
            # category), an integer means every query case was forced to that
            # candidate count.
            "parallel_candidates_override": parallel_candidates_override,
        },
        "multi_candidate_method": (
            "candidate_selection_uplift compares the selected candidate against the first "
            "candidate over the same cases. NOTE: the per-case candidate_selection flag in "
            "the gold set correlates perfectly with category (multi_table/metric set it, "
            "single_table/time do not), so a candidate-vs-no-candidate comparison across "
            "cases is confounded with task difficulty. Use --parallel-candidates to force "
            "one candidate count across identical cases."
        ),
        "token_cost_method": "heuristic char_count/4; configure per-million prices for estimated USD only",
        "policy_evaluation_method": (
            "probes run through the real SQLPolicyEngine with the case's "
            "sql_policy; query-case rejections count as false positives"
        ),
        "results": results,
    }


def _average(values: list[float | int]) -> float | None:
    return round(float(statistics.mean(values)), 6) if values else None


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    cases = load_cases(_project_path(args.cases), args.limit)
    # Isolated state: evaluation never writes production history, sessions,
    # run artifacts, or the vector knowledge base.
    base_config = load_config(
        provider_override=args.model_provider,
        model_override=args.model,
    )
    evaluation_config = isolated_config(base_config, args.asset_state_root)
    usage_log: list[dict[str, int]] = []
    report = evaluate_cases(
        cases,
        service=AgentService(
            config_loader=lambda **_: evaluation_config,
            llm_factory=_recording_model_factory(usage_log),
        ),
        environment=EvaluationEnvironment(args.asset_state_root),
        default_database=args.database,
        default_semantic_model=args.semantic_model,
        default_sql_policy=args.sql_policy,
        model_provider=args.model_provider,
        model=args.model,
        input_cost_per_million=args.input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
        usage_log=usage_log,
        parallel_candidates_override=args.parallel_candidates,
        skill_selection=[] if args.skill_mode == "off" else None,
    )
    report["state_isolation"] = {
        "mode": "isolated",
        "history_db_path": evaluation_config.history_db_path,
        "orchestration_state_root": evaluation_config.orchestration_state_root,
        "vector_kb_path": evaluation_config.vector_kb_path,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    failures = _gate_failures(report, args)
    for reason in failures:
        print(f"[gate] FAILED: {reason}", file=sys.stderr)
    if failures:
        return 1
    print("[gate] PASSED", file=sys.stderr)
    return 0


def _gate_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Exit-code gates that are only evaluated where measurable.

    A gate configured at its default (0.0) or with no applicable cases does
    not block; a gate configured with an explicit threshold fails when the
    measured rate is below it.
    """
    metrics = report["metrics"]
    failures: list[str] = []
    if report["query_count"]:
        execution = metrics["sql_execution_success_rate"]
        if (
            args.min_execution_success > 0
            and execution is not None
            and execution < args.min_execution_success
        ):
            failures.append(
                f"sql_execution_success_rate={execution} below "
                f"--min-execution-success {args.min_execution_success}"
            )
        semantic = metrics["semantic_correctness_rate"]
        if (
            args.min_semantic_correct > 0
            and semantic is not None
            and semantic < args.min_semantic_correct
        ):
            failures.append(
                f"semantic_correctness_rate={semantic} below "
                f"--min-semantic-correct {args.min_semantic_correct}"
            )
    if report["probe_count"] and args.min_policy_recall > 0:
        recall = metrics["policy_rejection_recall"]
        if recall is not None and recall < args.min_policy_recall:
            failures.append(
                f"policy_rejection_recall={recall} below "
                f"--min-policy-recall {args.min_policy_recall}"
            )
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
