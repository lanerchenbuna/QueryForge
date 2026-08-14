"""Evaluate QueryForge against multi-domain NL2SQL gold cases.

The evaluator reports exact SQL agreement separately from semantic result equivalence.
Policy precision/recall is measured over *generated* SQL: rejection probes must be
rejected by the real policy engine (with the case's sql_policy), and any legitimate
query case whose generated SQL is rejected counts as a false positive. Token and
cost values are deterministic estimates, not provider-reported billing usage.
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
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.application import AgentOptions, AgentService
from queryforge.data_assets import DataAssetBuilder
from queryforge.domain.security import SQLPolicyViolation, load_sql_policy
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool


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
) -> dict[str, Any]:
    results = []
    for index, case in enumerate(cases, start=1):
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
            )
        input_tokens = _estimate_tokens(case["question"])
        output_tokens = _estimate_tokens(
            str(result.get("sql") or "")
            + "".join(
                str(candidate.get("sql") or "")
                for candidate in result.get("candidates", [])
            )
        )
        result["estimated_input_tokens"] = input_tokens
        result["estimated_output_tokens"] = output_tokens
        result["estimated_cost_usd"] = round(
            (input_tokens * input_cost_per_million + output_tokens * output_cost_per_million)
            / 1_000_000,
            8,
        )
        results.append(result)
    return _build_report(results)


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
                    skills=[],
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
                skills=[],
                session_id=session_id,
                parallel_candidates=2 if case.get("candidate_selection") else 1,
                run_id=f"evaluation_{index}",
            ),
        )
        expected_columns, expected_rows = _execute_expected(
            database, case.get("expected_sql")
        )
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
            "semantic_correct": semantic_correct,
            "sql_exact_match": normalize_sql(output.get("sql"))
            == normalize_sql(case.get("expected_sql")),
            "policy_rejected": policy_rejected,
            "policy_expected_rejection": False,
            "row_count": output.get("row_count"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "sql": output.get("sql"),
            "selected_index": selection.get("selected_index"),
            "candidates": candidates,
            "first_candidate_semantic_correct": first_candidate_correct,
            "candidate_selection_used": bool(candidates),
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
            "semantic_correct": False,
            "sql_exact_match": False,
            "policy_rejected": None,
            "policy_expected_rejection": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "sql": None,
            "candidates": [],
            "candidate_selection_used": False,
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

    When the actual and expected column name sets match but their order
    differs, actual rows are reordered to the expected column order before
    comparison; otherwise comparison falls back to positional values.
    """
    reordered = _reorder_columns(
        list(actual_columns or []), actual_rows, expected_columns
    )
    return _canonical_rows(reordered) == _canonical_rows(expected_rows)


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
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


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


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _build_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    query_results = [item for item in results if item["expected_outcome"] == "query"]
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
        (str(item.get("question")), str(item.get("sql") or item.get("policy_probe_sql") or ""))
        for item in results
    }
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
        "per_domain": per_domain,
        "metrics": {
            "sql_execution_success_rate": _rate(
                [bool(item["execution_success"]) for item in query_results]
            ),
            "semantic_correctness_rate": _rate(
                [bool(item["semantic_correct"]) for item in query_results]
            ),
            "sql_exact_match_rate": _rate(
                [bool(item["sql_exact_match"]) for item in query_results]
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
        },
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
    report = evaluate_cases(
        cases,
        service=AgentService(),
        environment=EvaluationEnvironment(args.asset_state_root),
        default_database=args.database,
        default_semantic_model=args.semantic_model,
        default_sql_policy=args.sql_policy,
        model_provider=args.model_provider,
        model=args.model,
        input_cost_per_million=args.input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
    )
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
