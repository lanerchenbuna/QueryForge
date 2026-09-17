"""Tests for the step-16 benchmark harness (scripts/benchmark_agent.py).

The harness is the part of the benchmark that decides *what* runs, on which tier,
and whether the effect gates pass. These tests pin the anti-footgun behaviour:
tiers cannot be mixed, ablations cannot be invented, a missing optional
dependency fails the integration tier instead of skipping it, and the recorded
trace really carries the tool calls the evaluator scores.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_agent.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("_qf_benchmark_agent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the module defines a dataclass, and dataclass
    # processing resolves the class's module through ``sys.modules``.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


harness = _load_harness()


class HarnessTierTest(unittest.TestCase):
    """Tier discipline: deterministic tiers must not depend on a model."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_tier_one_refuses_a_model_provider(self):
        code, _, stderr = self._run(
            ["--tier", "1", "--provider", "openai", "--model", "gpt-4o-mini"]
        )
        self.assertEqual(code, 2)
        self.assertIn("tier-3 only", stderr)

    def test_tier_three_requires_a_provider_and_model(self):
        code, _, stderr = self._run(["--tier", "3"])
        self.assertEqual(code, 2)
        self.assertIn("requires --provider and --model", stderr)

    def test_unknown_ablation_is_rejected(self):
        code, _, stderr = self._run(["--ablate", "teleportation"])
        self.assertEqual(code, 2)
        self.assertIn("unknown ablation", stderr)

    def test_tier_one_publishes_a_report_and_no_model_metadata(self):
        """A tier-1 run must not report a provider/model it never used."""
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "tier1.json"
            code, stdout, stderr = self._run(
                ["--tier", "1", "--report", str(report_path), "--limit", "1"]
            )
            payload = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        # Missing or empty benchmark inputs must be an explicit usage error (exit
        # 2), never a silent pass; a real run must not report model metadata that
        # tier 1 never used.
        if code == 0:
            self.assertEqual(payload["tier"], 1)
            self.assertIsNone(payload["provider"])
            self.assertIsNone(payload["model"])
            self.assertEqual(payload["mode"], "deterministic_offline")
            self.assertTrue(payload["evaluated_count"])
            self.assertIn("[gate]", stderr)
        else:
            self.assertEqual(code, 2)
            self.assertTrue(
                "no tasks selected" in stderr
                or "benchmark inputs are missing or invalid" in stderr,
                stderr,
            )
            self.assertEqual(payload, {})


class HarnessGateTest(unittest.TestCase):
    """16-R1: a skipped integration tier is a failed integration tier."""

    def test_a_missing_dependency_fails_the_integration_tier(self):
        failures = harness.tier2_dependency_failures(["module_that_does_not_exist_qa"])
        self.assertEqual(len(failures), 1)
        self.assertIn("module_that_does_not_exist_qa", failures[0])
        self.assertIn("skipped integration tier is a failed integration tier", failures[0])

    def test_present_dependencies_pass_the_integration_dependency_check(self):
        self.assertEqual(harness.tier2_dependency_failures(["json", "unittest"]), [])

    def test_the_required_dependency_list_matches_the_contract(self):
        self.assertEqual(
            set(harness.TIER2_REQUIRED_DEPENDENCIES), {"fastapi", "httpx", "mcp", "lancedb", "pyarrow", "duckdb", "multipart"}
        )
        self.assertTrue(harness.TIER2_INTEGRATION_TESTS)

    def test_ablation_vocabulary_matches_the_planner_switches(self):
        from queryforge.application.analysis_planner import DISABLEABLE_FEATURES

        # `replan` is an existing knob rather than a disabled feature.
        self.assertEqual(
            set(harness.TIER1_ABLATIONS) - {"replan"}, set(DISABLEABLE_FEATURES)
        )
        self.assertFalse(set(harness.TIER3_ONLY_ABLATIONS) & set(harness.TIER1_ABLATIONS))


class HarnessRunnerRoutingTest(unittest.TestCase):
    """Tier 1 must not try to evaluate a task that needs a model."""

    def _spec(self, runner=None, outcome="analysis"):
        class Spec:
            pass

        spec = Spec()
        spec.task_id = "t"
        spec.expected_outcome = outcome
        spec.runner = runner
        return spec

    def test_runner_defaults_by_expected_outcome(self):
        self.assertEqual(harness.spec_runner(self._spec(outcome="analysis")), "planner")
        self.assertEqual(harness.spec_runner(self._spec(outcome="clarification")), "planner")
        self.assertEqual(
            harness.spec_runner(self._spec(outcome="policy_rejection")), "planner"
        )
        self.assertEqual(harness.spec_runner(self._spec(outcome="query")), "workflow")

    def test_explicit_runner_wins(self):
        self.assertEqual(
            harness.spec_runner(self._spec(runner="workflow", outcome="analysis")),
            "workflow",
        )

    def test_tier_one_skips_model_tasks_with_a_reason(self):
        dataset = {
            "dataset_id": "d",
            "database": "sample_data/anime_streaming/anime_streaming.sqlite",
            "semantic_model": "sample_data/anime_streaming/semantic_model.yml",
            "sql_policy": "sample_data/anime_streaming/sql_policy.yml",
        }
        context = harness.BenchmarkContext(datasets={"d": dataset}, state_root=Path("."))
        spec = self._spec(runner="workflow", outcome="query")
        spec.dataset = "d"
        outcome, traces = None, None
        with unittest.mock.patch.object(
            harness, "_run_one", side_effect=AssertionError("must not run a model task")
        ):
            outcomes, traces = harness.run_suite([spec], context, tier=1)
        self.assertEqual(outcomes, [])
        self.assertEqual(traces, [])
        self.assertEqual(len(context.skipped), 1)
        self.assertIn("tier 3", context.skipped[0]["reason"])

    def test_unknown_dataset_is_skipped_with_a_reason(self):
        context = harness.BenchmarkContext(datasets={}, state_root=Path("."))
        spec = self._spec(runner="planner", outcome="analysis")
        spec.dataset = "missing"
        outcomes, traces = harness.run_suite([spec], context, tier=1)
        self.assertEqual((outcomes, traces), ([], []))
        self.assertEqual(context.skipped[0]["reason"], "unknown dataset 'missing'")


class HarnessTraceTest(unittest.TestCase):
    """The recorded trace is what the evaluator scores; it must carry the calls."""

    def test_tool_calls_are_derived_from_the_recorded_payload(self):
        payload = {
            "steps": [
                {
                    "step_id": "resolve_metric",
                    "action": "resolve_metric",
                    "status": "succeeded",
                    "duration_ms": 1.0,
                },
                {
                    "step_id": "query_metric",
                    "action": "query_metric",
                    "status": "failed",
                    "error_category": "data_quality",
                    "duration_ms": 2.0,
                },
            ]
        }
        calls = harness._tool_calls_from_payload(payload)
        # The trace carries the governed *tool*, not just the plan action.
        self.assertEqual([call["tool"] for call in calls], ["list_metrics", "execute_sql"])
        self.assertEqual(
            [call["action"] for call in calls], ["resolve_metric", "query_metric"]
        )
        self.assertEqual([call["ok"] for call in calls], [True, False])
        self.assertEqual(calls[1]["error_category"], "data_quality")

    def test_cost_is_none_without_a_price_table(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        self.assertIsNone(harness._cost_usd(usage, None))
        self.assertIsNone(harness._cost_usd(usage, {}))
        priced = harness._cost_usd(usage, {"input_per_million": 1.0, "output_per_million": 2.0})
        self.assertAlmostEqual(priced, (1000 * 1.0 + 500 * 2.0) / 1_000_000, places=10)
        self.assertIsNone(
            harness._cost_usd(None, {"input_per_million": 1.0, "output_per_million": 2.0})
        )

    def test_selection_respects_split_dataset_and_limit(self):
        class Spec:
            def __init__(self, task_id, split, dataset):
                self.task_id = task_id
                self.split = split
                self.dataset = dataset

        specs = [
            Spec("a", "dev", "x"),
            Spec("b", "regression", "x"),
            Spec("c", "holdout", "y"),
            Spec("d", "regression", "y"),
        ]
        selected = harness.select_specs(specs, splits=["regression"], datasets=[], task_ids=[])
        self.assertEqual([spec.task_id for spec in selected], ["b", "d"])
        self.assertEqual(
            [spec.task_id for spec in harness.select_specs(specs, splits=["dev", "holdout"], datasets=[], task_ids=[])],
            ["a", "c"],
        )
        self.assertEqual(
            [spec.task_id for spec in harness.select_specs(specs, splits=["regression"], datasets=["y"], task_ids=[])],
            ["d"],
        )
        self.assertEqual(
            [spec.task_id for spec in harness.select_specs(specs, splits=["regression"], datasets=[], task_ids=[], limit=1)],
            ["b"],
        )


if __name__ == "__main__":
    unittest.main()
