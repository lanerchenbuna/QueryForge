"""Run the step-16 agent task benchmark, ablations, and effect gates.

Three tiers, deliberately kept apart so a score is never mixed up:

* **tier 1** — deterministic and offline. Runs the planned-analysis path (no
  model call at all) plus the SQL policy probes through the real policy engine.
  This is the gate that must pass on every commit.
* **tier 2** — integration: requires the optional transport dependencies. A
  missing dependency **fails** the gate instead of quietly skipping it.
* **tier 3** — real model evaluation. Opt-in (``--provider``/``--model``),
  reported separately, never part of the tier-1/2 verdict.

The runner records a raw trace per task (payload, tool calls, usage, latency) and
hands it to the isolated evaluator in :mod:`queryforge.evaluation`; every metric
in the report can therefore be recomputed from the report alone.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DATASETS = "evaluation/datasets.json"
DEFAULT_TASKS_DIR = "evaluation/tasks"
DEFAULT_THRESHOLDS = "evaluation/thresholds.json"
DEFAULT_REPORT_DIR = "evaluation/reports"

#: Tier-1 ablations, each mapped to a real switch (see the step-16 interface doc).
TIER1_ABLATIONS = (
    "analysis_tools",
    "data_quality",
    "semantic_compile",
    "evidence_layer",
    "replan",
)
#: Ablations that only exist on the workflow (LLM) path, measured in tier 3.
TIER3_ONLY_ABLATIONS = ("fix_loop", "multi_candidate")
#: Dependencies tier 2 must actually have installed (16-R1).
TIER2_REQUIRED_DEPENDENCIES = ("fastapi", "httpx", "mcp", "lancedb", "pyarrow", "duckdb", "multipart")
#: Test modules tier 2 must execute without being skipped.
TIER2_INTEGRATION_TESTS = (
    "tests.test_service_api_gateway_mcp",
    "tests.test_streaming",
    "tests.test_usage_tracing",
    "tests.test_database_adapters",
    "tests.test_demo_scripts",
    "tests.test_optional_integrations",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--datasets", default=DEFAULT_DATASETS)
    parser.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR)
    parser.add_argument(
        "--split",
        action="append",
        choices=("dev", "regression", "holdout"),
        help="Splits to run (default: dev + regression; holdout must be asked for)",
    )
    parser.add_argument("--dataset", action="append", help="Restrict to dataset id(s)")
    parser.add_argument("--task", action="append", help="Restrict to task id(s)")
    parser.add_argument(
        "--ablate",
        action="append",
        default=[],
        help=f"Disable a feature ({', '.join((*TIER1_ABLATIONS, *TIER3_ONLY_ABLATIONS))})",
    )
    parser.add_argument(
        "--ablation-sweep",
        action="store_true",
        help="Run the baseline plus one run per tier-1 ablation and report deltas",
    )
    parser.add_argument("--retrieval-corpus", action="append", default=[], help="JSONL knowledge corpus to audit for holdout contamination")
    parser.add_argument("--repeat", type=int, default=1, help="Repetitions per task")
    parser.add_argument("--provider", default=None, help="Tier 3 model provider")
    parser.add_argument("--model", default=None, help="Tier 3 model name")
    parser.add_argument("--limit", type=int, default=0, help="Cap the task count")
    parser.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--report", default=None, help="Report path (JSON)")
    parser.add_argument(
        "--state-root",
        default=None,
        help="Isolated state root for the benchmark (default: a temp directory)",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit non-zero when the tier's thresholds are not met",
    )
    parser.add_argument(
        "--prices",
        default=None,
        help="JSON file with {input_per_million, output_per_million} for cost accounting",
    )
    return parser.parse_args(argv)


@dataclass
class BenchmarkContext:
    """Everything one benchmark run needs; resolved once, reused per task."""

    datasets: dict[str, dict[str, Any]]
    state_root: Path
    ablations: frozenset[str] = frozenset()
    prices: dict[str, float] | None = None
    provider: str | None = None
    model: str | None = None
    runner: str = "planner"
    skipped: list[dict[str, str]] = field(default_factory=list)
    repeats: int = 1


# --------------------------------------------------------------------- helpers


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def load_datasets(path: str | Path = DEFAULT_DATASETS) -> dict[str, dict[str, Any]]:
    payload = json.loads(_project_path(path).read_text(encoding="utf-8"))
    datasets: dict[str, dict[str, Any]] = {}
    for entry in payload.get("datasets") or []:
        dataset_id = str(entry.get("dataset_id") or "").strip()
        if not dataset_id:
            continue
        datasets[dataset_id] = dict(entry)
    if not datasets:
        raise ValueError(f"no datasets declared in {path}")
    return datasets


def _cost_usd(
    usage: dict[str, Any] | None, prices: dict[str, float] | None
) -> float | None:
    """Cost from a price table; ``None`` when no prices are configured."""
    if not prices or not usage or usage.get("estimated") or not all(k in usage for k in ("prompt_tokens", "completion_tokens")):
        return None
    prompt = float(usage.get("prompt_tokens") or 0)
    completion = float(usage.get("completion_tokens") or 0)
    per_in = float(prices.get("input_per_million") or 0)
    per_out = float(prices.get("output_per_million") or 0)
    if not per_in and not per_out:
        return None
    return round((prompt * per_in + completion * per_out) / 1_000_000, 8)


def _tool_calls_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the tool-call trace from a recorded planner payload."""
    from queryforge.orchestration.planner.plan import ACTION_TOOL_MAP

    calls: list[dict[str, Any]] = []
    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("status") not in {"succeeded", "failed"} or step.get("tool_calls") == 0:
            continue
        action = str(step.get("action") or step.get("step_id") or "")
        # Local actions (e.g. `compose_answer`) have no governed tool; they are
        # recorded under their own action name so the trace never carries a null
        # tool name into the legality check.
        tool = ACTION_TOOL_MAP.get(action) or action
        calls.append(
            {
                "step_id": step.get("step_id"),
                "action": action,
                "tool": tool,
                "ok": step.get("status") == "succeeded",
                "status": step.get("status"),
                "duration_ms": step.get("duration_ms"),
                "error_category": step.get("error_category"),
            }
        )
    return calls


def _usage_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    budgets = payload.get("budgets") if isinstance(payload.get("budgets"), dict) else {}
    usage = budgets.get("usage") if isinstance(budgets.get("usage"), dict) else None
    if usage is None:
        return None
    model = payload.get("model_usage") if isinstance(payload.get("model_usage"), dict) else None
    return {
        "prompt_tokens": int(usage.get("max_estimated_tokens") or 0),
        "completion_tokens": 0,
        "estimated": True,
        "raw": usage,
        "model": model,
    }


# ---------------------------------------------------------------------- runners


def run_analysis_task(
    spec: Any, dataset: dict[str, Any], context: BenchmarkContext
) -> dict[str, Any]:
    """Run one task through the deterministic planned-analysis path."""
    from queryforge.application.analysis_planner import AnalysisPlannerService

    disabled = [
        feature for feature in context.ablations if feature in TIER1_ABLATIONS and feature != "replan"
    ]
    max_replans = 0 if "replan" in context.ablations else 2
    from scripts.benchmark_runners import isolated_config
    resolved = {k: str(_project_path(v)) if k in {"database", "semantic_model", "sql_policy"} else v for k,v in dataset.items()}
    config = isolated_config(resolved, context.state_root)
    service = AnalysisPlannerService(
        config_loader=lambda: config, max_replans=max_replans, disabled_features=disabled
    )
    database = _project_path(dataset["database"])
    started = time.perf_counter()
    error: str | None = None
    payload: dict[str, Any] = {}
    try:
        payload = service.analyze(
            spec.question,
            database=str(database),
            semantic_model_path=str(_project_path(dataset["semantic_model"])),
            sql_policy_path=str(_project_path(dataset["sql_policy"])),
        )
    except Exception as exc:  # a runner failure is a scored outcome, not a crash
        error = f"{type(exc).__name__}: {exc}"
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    usage = _usage_from_payload(payload)
    return {
        "task_id": spec.task_id,
        "payload": payload,
        "wall_ms": wall_ms,
        "tool_calls": _tool_calls_from_payload(payload),
        "usage": usage,
        "cost_usd": _cost_usd(usage, context.prices),
        "provider": context.provider,
        "model": context.model,
        "runner": context.runner,
        "error": error,
    }


def run_policy_probe(
    spec: Any, dataset: dict[str, Any], context: BenchmarkContext
) -> dict[str, Any]:
    """Run a rejection probe through the real policy engine (no model call)."""
    from queryforge.domain.security import SQLPolicyViolation, load_sql_policy
    from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
    from queryforge.infrastructure.tools.database_tool import DatabaseTool

    probe = str(getattr(spec, "reference_sql", "") or "")
    database = _project_path(dataset["database"])
    rejected = False
    accepted = False
    error: str | None = None
    rule: str | None = None
    started = time.perf_counter()
    try:
        policy, policy_source = load_sql_policy(
            _project_path(dataset["sql_policy"])
        )
        with SQLiteConnector(str(database)) as connector:
            tool = DatabaseTool(connector, policy, policy_source_path=policy_source)
            try:
                decision = tool.policy_engine.evaluate(probe)
                rule = decision.rule
                accepted = True
            except SQLPolicyViolation as exc:
                rejected = True
                rule = exc.decision.rule
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    payload = {
        "status": "blocked" if rejected else "succeeded",
        "stop_reason": "policy_rejection" if rejected else None,
        "sql": probe,
        "policy_rejected": rejected,
        "policy_accepted": accepted,
        "policy_rule": rule,
        "evidence": [],
        "steps": [],
        "budgets": {"usage": {"max_tool_calls": 0}},
    }
    tool = "execute_sql"
    return {
        "task_id": spec.task_id,
        "payload": payload,
        "wall_ms": wall_ms,
        "tool_calls": [
            {
                "tool": tool,
                "action": tool,
                "ok": rejected,
                "status": "blocked" if accepted else "succeeded",
            }
        ],
        "usage": None,
        "cost_usd": None,
        "provider": context.provider,
        "model": context.model,
        "runner": "policy_probe",
        "error": error,
    }


def _run_one(spec: Any, dataset: dict[str, Any], context: BenchmarkContext) -> dict[str, Any]:
    if spec.expected_outcome == "policy_rejection":
        return run_policy_probe(spec, dataset, context)
    if spec_runner(spec) in {"workflow", "scripted_workflow"}:
        from scripts.benchmark_runners import run_workflow_task
        resolved = {k: str(_project_path(v)) if k in {"database", "semantic_model", "sql_policy"} else v for k,v in dataset.items()}
        return run_workflow_task(spec, resolved, context)
    return run_analysis_task(spec, dataset, context)


# ------------------------------------------------------------------- suite runs


def select_specs(
    specs: Iterable[Any],
    *,
    splits: Sequence[str],
    datasets: Sequence[str],
    task_ids: Sequence[str],
    limit: int = 0,
) -> list[Any]:
    selected = [
        spec
        for spec in specs
        if spec.split in splits
        and (not datasets or spec.dataset in datasets)
        and (not task_ids or spec.task_id in task_ids)
    ]
    return selected[:limit] if limit else selected


def spec_runner(spec: Any) -> str:
    """Which link is expected to achieve this task: the spec, or its outcome."""
    runner = getattr(spec, "runner", None) or getattr(spec, "resolved_runner", None)
    if runner:
        return str(runner)
    return "workflow" if spec.expected_outcome == "query" else "planner"


def run_suite(
    specs: Sequence[Any],
    context: BenchmarkContext,
    *,
    tier: int = 1,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Run every selected task this tier can evaluate; returns (outcomes, traces).

    Only tasks whose runner matches the tier are executed: tier 1 is the
    deterministic planner (no model call), so a task that needs SQL generation
    from a model is recorded as skipped with the reason instead of being silently
    counted as a failure or, worse, silently counted as a pass.
    """
    runnable: list[tuple[Any, dict[str, Any]]] = []
    for spec in specs:
        dataset = context.datasets.get(spec.dataset)
        if dataset is None:  # pragma: no cover - gold self-check owns this case
            context.skipped.append(
                {"task_id": spec.task_id, "reason": f"unknown dataset {spec.dataset!r}"}
            )
            continue
        runner = spec_runner(spec)
        if tier == 1 and runner not in {"planner", "scripted_workflow"}:
            context.skipped.append(
                {
                    "task_id": spec.task_id,
                    "reason": f"runner={runner!r} needs a model; evaluated in tier 3",
                }
            )
            continue
        if tier == 3 and runner not in {"workflow", "scripted_workflow"}:
            context.skipped.append(
                {
                    "task_id": spec.task_id,
                    "reason": "deterministic planner task; measured in tier 1",
                }
            )
            continue
        runnable.append((spec, dataset))

    if not runnable:
        # Nothing this tier can evaluate: report the skips without needing the
        # evaluator package to be importable.
        return [], []

    from queryforge.evaluation import TaskTrace, evaluate_task

    outcomes: list[Any] = []
    traces: list[dict[str, Any]] = []
    import tempfile
    context.state_root.mkdir(parents=True, exist_ok=True)
    for repeat in range(context.repeats):
        for spec, dataset in runnable:
            with tempfile.TemporaryDirectory(prefix="case_", dir=context.state_root) as isolated:
                case_context = replace(context, state_root=Path(isolated))
                record = _run_one(spec, dataset, case_context)
            record["repeat"] = repeat
            trace = TaskTrace.model_validate(record)
            outcomes.append(evaluate_task(spec, trace, price_table=context.prices))
            traces.append(record)
    return outcomes, traces


def build_report(
    *,
    tier: int,
    specs: Sequence[Any],
    outcomes: Sequence[Any],
    traces: Sequence[dict[str, Any]],
    context: BenchmarkContext,
    thresholds: dict[str, Any],
    ablations: Sequence[str],
    repeats: int,
) -> dict[str, Any]:
    from queryforge.evaluation import aggregate

    metrics = aggregate(outcomes, thresholds=thresholds.get(_tier_key(tier)) or {})
    import hashlib
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    sources = sorted([*PROJECT_ROOT.glob("queryforge/**/*.py"), *PROJECT_ROOT.glob("scripts/*.py"), PROJECT_ROOT/"pyproject.toml"])
    source_digest = hashlib.sha256("\n".join(f"{p.relative_to(PROJECT_ROOT)}:{digest(p)}" for p in sources).encode()).hexdigest()
    by_task = {}
    for o in outcomes:
        by_task.setdefault(o.task_id, []).append(o.passed)
    n = len(by_task)
    successes = sum(all(values) for values in by_task.values())
    if n:
        z = 1.96
        p = successes/n
        center = (p+z*z/(2*n))/(1+z*z/n)
        radius = z*((p*(1-p)/n+z*z/(4*n*n))**0.5)/(1+z*z/n)
        interval = [center-radius, center+radius]
    else:
        interval = None
    return {
        "benchmark": "agent_tasks",
        "tier": tier,
        "tier_key": _tier_key(tier),
        "mode": "deterministic_offline" if tier == 1 else "integration" if tier == 2 else "model_e2e",
        "runner": context.runner,
        "provider": context.provider,
        "model": context.model,
        "repeats": repeats,
        "ablations": list(ablations),
        "state_root": str(context.state_root),
        "task_count": len(specs),
        "evaluated_count": len(outcomes),
        "skipped_tasks": list(context.skipped),
        "thresholds": thresholds.get(_tier_key(tier)) or {},
        "price_table": context.prices,
        "provenance": {"source_sha256": source_digest,
                       "datasets_sha256": {k: digest(_project_path(v["database"])) for k,v in context.datasets.items()},
                       "gold_sha256": hashlib.sha256(json.dumps([s.model_dump() for s in specs],sort_keys=True).encode()).hexdigest(),
                       "threshold_sha256": hashlib.sha256(json.dumps(thresholds,sort_keys=True).encode()).hexdigest(),
                       "git_head": subprocess.run(["git","rev-parse","HEAD"],cwd=PROJECT_ROOT,capture_output=True,text=True).stdout.strip(),
                       "dirty": bool(subprocess.run(["git","status","--porcelain"],cwd=PROJECT_ROOT,capture_output=True,text=True).stdout.strip())},
        "uncertainty": {"unique_tasks": n, "all_repeats_passed": successes, "wilson95_task_stability": interval,
                        "note": "Repeated cases are not independent samples. Curated small datasets do not establish population generalization."},
        "metrics": metrics,
        "results": metrics["results"],
        "raw_traces": list(traces),
    }


def _tier_key(tier: int) -> str:
    return {1: "tier1_offline", 2: "tier2_integration", 3: "tier3_model_e2e"}[tier]


def ablation_sweep(
    specs: Sequence[Any], context: BenchmarkContext
) -> dict[str, Any]:
    """Baseline vs one run per tier-1 ablation, with benefits *and* costs."""
    from queryforge.evaluation import aggregate

    results: dict[str, Any] = {}
    baseline_context = BenchmarkContext(
        datasets=context.datasets,
        state_root=context.state_root,
        prices=context.prices,
        provider=context.provider,
        model=context.model,
        runner=context.runner, repeats=context.repeats,
    )
    baseline_outcomes, _ = run_suite(specs, baseline_context, tier=1)
    baseline_metrics = aggregate(baseline_outcomes, thresholds={})
    results["baseline"] = {
        "results": baseline_metrics["results"],
        "ablations": [],
        "task_success_rate": baseline_metrics.get("task_success_rate"),
        "evidence_coverage_rate": baseline_metrics.get("evidence_coverage_rate"),
        "avg_tool_calls": baseline_metrics.get("avg_tool_calls"),
        "p50_wall_ms": baseline_metrics.get("p50_wall_ms"),
        "p95_wall_ms": baseline_metrics.get("p95_wall_ms"),
        "evaluated_count": len(baseline_outcomes),
        "failure_classes": baseline_metrics.get("failure_classes"),
    }
    for feature in TIER1_ABLATIONS:
        ablated = BenchmarkContext(
            datasets=context.datasets,
            state_root=context.state_root,
            prices=context.prices,
            provider=context.provider,
            model=context.model,
            runner=context.runner, repeats=context.repeats,
            ablations=frozenset({feature}),
        )
        outcomes, _ = run_suite(specs, ablated, tier=1)
        metrics = aggregate(outcomes, thresholds={})
        results[feature] = {
            "results": metrics["results"],
            "ablations": [feature],
            "cost_usd": metrics.get("cost_usd"),
            "task_success_rate": metrics.get("task_success_rate"),
            "evidence_coverage_rate": metrics.get("evidence_coverage_rate"),
            "avg_tool_calls": metrics.get("avg_tool_calls"),
            "p50_wall_ms": metrics.get("p50_wall_ms"),
            "p95_wall_ms": metrics.get("p95_wall_ms"),
            "evaluated_count": len(outcomes),
            "failure_classes": metrics.get("failure_classes"),
            "delta_task_success_rate": _delta(
                metrics.get("task_success_rate"),
                baseline_metrics.get("task_success_rate"),
            ),
            "delta_avg_tool_calls": _delta(
                metrics.get("avg_tool_calls"), baseline_metrics.get("avg_tool_calls")
            ),
            "delta_evidence_coverage_rate": _delta(
                metrics.get("evidence_coverage_rate"),
                baseline_metrics.get("evidence_coverage_rate"),
            ),
        }
    return {
        "sample_size": len(specs),
        "repeats": context.repeats,
        "baseline": results.pop("baseline"),
        "ablations": results,
        "note": (
            "Each ablation disables one capability through the same code path a "
            "deployment without it would take; the same model and data are used, "
            "and no model call happens on this tier."
        ),
    }


def _delta(value: Any, baseline: Any) -> float | None:
    if value is None or baseline is None:
        return None
    return round(float(value) - float(baseline), 6)


# ------------------------------------------------------------------------ gates


def tier2_dependency_failures(
    required: Sequence[str] = TIER2_REQUIRED_DEPENDENCIES,
) -> list[str]:
    """Missing optional dependencies must FAIL the gate, never skip it (16-R1)."""
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        return [
            "tier 2 requires the optional dependencies to be installed, but "
            f"{', '.join(missing)} is missing; a skipped integration tier is a "
            "failed integration tier"
        ]
    return []


def tier2_skipped_test_failures(
    modules: Sequence[str] = TIER2_INTEGRATION_TESTS,
) -> list[str]:
    """Tier 2 must execute its integration tests, not report them as skipped."""
    failures: list[str] = []
    for module in modules:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", module],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        if completed.returncode != 0:
            failures.append(f"{module} failed (exit {completed.returncode}): {output[-1800:]}")
            continue
        import re
        if re.search(r"skipped=\d+|\.\.\. skipped|Ran 0 tests", output):
            failures.append(
                f"{module} reported skipped tests; the integration tier must run "
                "them (install the optional dependencies)"
            )
    return failures


def gate_failures(
    report: dict[str, Any], thresholds: dict[str, Any]
) -> list[str]:
    from queryforge.evaluation.thresholds import check_metrics

    tier_key = str(report.get("tier_key") or "tier1_offline")
    failures = list(check_metrics(report.get("metrics") or {}, tier_key, thresholds))
    if not report.get("evaluated_count"):
        failures.append("evaluated zero tasks")
    return failures


# -------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Argument discipline first: a misuse must be reported as a usage error even
    # when the gold set or thresholds file is missing, so CI surfaces the real
    # problem instead of a stack trace.
    unknown = sorted(set(args.ablate) - set((*TIER1_ABLATIONS, *TIER3_ONLY_ABLATIONS)))
    if unknown:
        print(
            f"QueryForge benchmark failed: unknown ablation(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 2
    if args.repeat < 1 or args.limit < 0:
        print("repeat must be positive and limit non-negative", file=sys.stderr)
        return 2
    if args.tier != 3 and set(args.ablate) & set(TIER3_ONLY_ABLATIONS):
        print("model ablations require tier 3", file=sys.stderr)
        return 2
    if args.tier == 3 and args.ablation_sweep:
        print("tier-3 ablations require separate matched runs using --ablate", file=sys.stderr)
        return 2
    if args.tier == 3 and not (args.provider and args.model):
        print(
            "QueryForge benchmark failed: tier 3 requires --provider and --model",
            file=sys.stderr,
        )
        return 2
    if args.tier == 3 and args.gate:
        print("tier 3 has no calibrated baseline; run report-only and freeze thresholds before gating", file=sys.stderr)
        return 2
    if args.tier != 3 and (args.provider or args.model):
        print(
            "QueryForge benchmark failed: --provider/--model are tier-3 only; "
            "tiers 1 and 2 must stay deterministic",
            file=sys.stderr,
        )
        return 2

    try:
        thresholds = json.loads(_project_path(args.thresholds).read_text(encoding="utf-8"))
        datasets = load_datasets(args.datasets)
        prices = (
            json.loads(_project_path(args.prices).read_text(encoding="utf-8"))
            if args.prices
            else None
        )
        if prices and not prices.get("version"):
            raise ValueError("price table must include a version")
    except (OSError, ValueError) as exc:
        print(
            f"QueryForge benchmark failed: benchmark inputs are missing or "
            f"invalid ({type(exc).__name__}: {exc}); expected "
            f"{args.thresholds} and {args.datasets}",
            file=sys.stderr,
        )
        return 2

    splits = args.split or ["dev", "regression"]
    if args.tier == 1:
        import tempfile

        state_root = (
            Path(args.state_root).expanduser().resolve()
            if args.state_root
            else Path(tempfile.mkdtemp(prefix="qf_benchmark_"))
        )
    else:
        state_root = (
            Path(args.state_root).expanduser().resolve()
            if args.state_root
            else _project_path(DEFAULT_REPORT_DIR) / f"tier{args.tier}_state"
        )

    if args.tier == 2:
        # Dependencies first: a missing optional dependency is a gate failure.
        failures = tier2_dependency_failures()
        report = {
            "benchmark": "agent_tasks",
            "tier": 2,
            "tier_key": "tier2_integration",
            "mode": "integration",
            "dependency_failures": failures,
            "metrics": {},
            "results": [],
        }
        if not failures:
            report["skipped_test_failures"] = tier2_skipped_test_failures()
        report_path = _write_report(report, args.report, tier=args.tier)
        failures = [*failures, *report.get("skipped_test_failures", [])]
        for reason in failures:
            print(f"[gate] FAILED: {reason}", file=sys.stderr)
        print(json.dumps({"report": str(report_path), "failures": failures}, indent=2))
        return 1 if (failures and args.gate) else 0

    from queryforge.evaluation import load_spec_splits

    all_specs = load_spec_splits(_project_path(args.tasks_dir))
    from queryforge.evaluation.isolation import audit_splits, audit_corpus
    audit_splits(all_specs)
    for corpus in args.retrieval_corpus:
        audit_corpus(all_specs, _project_path(corpus))
    specs = select_specs(
        all_specs,
        splits=splits,
        datasets=args.dataset or [],
        task_ids=args.task or [],
        limit=args.limit,
    )
    if not specs:
        print(
            "QueryForge benchmark failed: no tasks selected (check --split/--dataset)",
            file=sys.stderr,
        )
        return 2

    context = BenchmarkContext(
        datasets=datasets,
        state_root=state_root,
        ablations=frozenset(args.ablate),
        prices=prices,
        repeats=args.repeat,
        provider=args.provider,
        model=args.model,
    )
    outcomes, traces = run_suite(specs, context, tier=args.tier)
    report = build_report(
        tier=args.tier,
        specs=specs,
        outcomes=outcomes,
        traces=traces,
        context=context,
        thresholds=thresholds,
        ablations=args.ablate,
        repeats=args.repeat,
    )
    if args.ablation_sweep:
        report["ablation_sweep"] = ablation_sweep(specs, context)
    report_path = _write_report(report, args.report, tier=args.tier)

    print(json.dumps({k:v for k,v in report["metrics"].items() if k != "results"}, ensure_ascii=False, indent=2))
    failures = gate_failures(report, thresholds)
    for reason in failures:
        print(f"[gate] FAILED: {reason}", file=sys.stderr)
    if failures and args.gate:
        print(f"[gate] report: {report_path}", file=sys.stderr)
        return 1
    print(
        f"[gate] {'PASSED' if not failures else 'FAILED (report-only; no --gate)'}: {report_path}",
        file=sys.stderr,
    )
    return 0


def _write_report(report: dict[str, Any], path: str | None, *, tier: int) -> Path:
    target = (
        _project_path(path)
        if path
        else _project_path(DEFAULT_REPORT_DIR) / f"agent_benchmark_tier{tier}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    raise SystemExit(main())
