"""Run QueryForge's reproducible, offline integration acceptance checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


CORE_CHECKS = (
    Check("repository_hygiene", ("scripts/check_repository.py",)),
    Check(
        "static_compile",
        ("-m", "compileall", "-q", "main.py", "queryforge", "sample", "tests"),
    ),
    Check("dependency_consistency", ("-m", "pip", "check")),
    Check("bundled_sample_data", ("-m", "queryforge", "--prepare-sample-data")),
    Check("workflow_entry", ("-m", "queryforge", "--show-workflow")),
    Check("model_registry", ("-m", "queryforge", "--list-models")),
    Check("skill_registry", ("-m", "queryforge", "--list-skills")),
    Check(
        "upgrade_integration_tests",
        (
            "-m",
            "unittest",
            "-q",
            "tests.test_sample_integration",
            "tests.test_llm",
            "tests.test_config",
            "tests.test_skills",
            "tests.test_semantic_model",
            "tests.test_metric_to_sql",
            "tests.test_anime_semantic_governance",
            "tests.test_sql_security_policy",
            "tests.test_date_parser_node",
            "tests.test_plan_mode_node",
            "tests.test_retry_workflow",
            "tests.test_sql_history_store",
            "tests.test_vector_kb",
            "tests.test_visualization_node",
            "tests.test_observability",
            "tests.test_service_api_gateway_mcp",
            "tests.test_agent_team_router_orchestrator",
            "tests.test_phasea_integration",
            "tests.test_conversation_memory",
            "tests.test_bounded_tool_loop",
            "tests.test_parallel_candidates",
            "tests.test_structured_reasoning",
            "tests.test_subject_tree",
            "tests.test_phasebc_integration",
            "tests.test_streaming",
            "tests.test_report_artifact",
            "tests.test_mcp_enhanced",
            "tests.test_phase2_final_acceptance",
            "tests.test_legacy_imports",
            "tests.test_data_assets",
            "tests.test_semantic_contracts",
            "tests.test_semantic_builder",
            "tests.test_semantic_drift",
            "tests.test_evaluate_sql",
            "tests.test_architecture_boundaries",
        ),
    ),
    Check("phase_a_performance_baseline", ("scripts/benchmark_phase_a.py",)),
    Check("phase_bc_performance_baseline", ("scripts/benchmark_phase_bc.py",)),
)

FULL_CHECK = Check(
    "full_test_suite",
    ("-m", "unittest", "discover", "-s", "tests", "-q"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run offline integration acceptance checks for QueryForge."
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run the complete unittest suite after the targeted checks.",
    )
    return parser.parse_args()


def run_check(check: Check) -> dict[str, object]:
    command = (sys.executable, *check.command)
    environment = os.environ.copy()
    environment["LOG_LEVEL"] = "CRITICAL"
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "name": check.name,
        "status": "passed" if completed.returncode == 0 else "failed",
        "command": " ".join(command),
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def main() -> int:
    args = parse_args()
    checks = (*CORE_CHECKS, FULL_CHECK) if args.full else CORE_CHECKS
    results = []
    for check in checks:
        print(f"[acceptance] running {check.name}...", file=sys.stderr)
        result = run_check(check)
        results.append(result)
        if result["status"] == "failed":
            break

    passed = sum(result["status"] == "passed" for result in results)
    report = {
        "status": "passed" if passed == len(checks) else "failed",
        "project": "QueryForge",
        "offline": True,
        "passed_checks": passed,
        "total_checks": len(checks),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
