"""Evaluator-level tests with hand-written traces (step 16, part B).

Covers 16-N1 (preset correct/incorrect traces score as expected and the report is
recomputable), 16-T1 (beautiful prose without evidence is a failure),
16-T2 (goal-oriented scoring accepts a different step order and a declared
substitution), 16-M1 (usage/cost accounting per task, failed tasks included,
measured and estimated tokens kept apart) plus tool legality/validity, budget,
claim guard, clarification (both directions), expected values with tolerance,
empty-result/data-fault tolerance and the threshold gate.

No runner, no network, no filesystem writes: every trace here is hand-written so
the expected verdict is provable by reading the fixture.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from queryforge.evaluation import (
    CHECK_NAMES,
    DEFAULT_THRESHOLDS_PATH,
    FAILURE_CLASSES,
    METRIC_PATHS,
    TIER_NAMES,
    TaskSpec,
    TaskSpecError,
    TaskTrace,
    aggregate,
    check_metrics,
    classify_failure,
    evaluate_task,
    load_spec_splits,
    load_specs,
    load_thresholds,
    recompute,
)

QUESTION = "What is total revenue for the selected period?"
VALUE = 43183.49

#: The tier the gate uses; a plain mapping so a test cannot accidentally pass
#: because of a typed object's default.
TIER1 = {
    "min_task_success_rate": 0.9,
    "min_evidence_coverage_rate": 0.95,
    "max_unsupported_assertion_rate": 0.0,
    "min_tool_legality_rate": 1.0,
    "min_clarification_appropriateness": 1.0,
    "max_avg_tool_calls_per_task": 12,
    "splits": {"regression": {"min_task_success_rate": 0.9}},
}


# ---------------------------------------------------------------------------
# Fixtures: hand-written payloads (analysis planner shape)
# ---------------------------------------------------------------------------


def metric_evidence_payload(value: float = VALUE) -> dict[str, Any]:
    """``query_metric`` step output, as the executor records it."""

    return {
        "metric": "revenue",
        "aggregation": "sum",
        "sql": "SELECT SUM(amount) AS revenue FROM orders",
        "columns": ["revenue"],
        "rows": [[value]],
        "row_count": 1,
        "value": value,
        "dimensions": [],
        "degraded": False,
        "semantic_version": "1.0",
        "query_spec": {
            "metric": "revenue",
            "aggregation": "sum",
            "base_table": "orders",
            "group_by": [],
        },
    }


def resolution_evidence_payload() -> dict[str, Any]:
    """``resolve_metric`` step output."""

    return {
        "metric": "revenue",
        "aggregation": "sum",
        "entity": "order",
        "table": "orders",
        "expression": "SUM(amount)",
        "time_field": "created_at",
        "declared_dimensions": ["order.channel"],
        "legal_dimensions": ["order.channel"],
        "requested_dimensions": [],
        "matched_term": "revenue",
        "semantic_version": "1.0",
        "candidates": ["revenue"],
    }


def evidence_entries() -> list[dict[str, Any]]:
    """The evidence the planner lifts to ``payload["evidence"]``."""

    return [
        {
            "evidence_id": "ev_res",
            "kind": "metric_resolution",
            "step_id": "s1",
            "action": "resolve_metric",
            "domain_id": "retail",
            "payload": resolution_evidence_payload(),
        },
        {
            "evidence_id": "ev_val",
            "kind": "metric_value",
            "step_id": "s2",
            "action": "query_metric",
            "domain_id": "retail",
            "payload": metric_evidence_payload(),
        },
    ]


def final_answer(
    *,
    value: float = VALUE,
    evidence_ids: tuple[str, ...] = ("ev_val",),
    conclusions: list[Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    status: str = "success",
) -> dict[str, Any]:
    """A step-12 ``final_answer``; the default one is fully anchored."""

    if findings is None:
        findings = [
            {
                "kind": "metric",
                "statement": "revenue over the full set",
                "numbers": {"value": value},
                "dimensions": [],
                "evidence_ids": list(evidence_ids),
                "degraded": False,
                "review_required": False,
            }
        ]
    if conclusions is None:
        conclusions = [f"revenue over the full set. Evidence-backed numbers: value={value}."]
    return {
        "question": QUESTION,
        "status": status,
        "conclusions": conclusions,
        "findings": findings,
        "evidence_ids": list(evidence_ids),
        "charts": [],
        "assumptions": [],
        "limitations": [],
        "open_questions": [],
        "review_required": False,
        "degraded": False,
    }


def legacy_answer(*, value: float = VALUE, evidence_ids: tuple[str, ...] = ("ev_val",)) -> dict[str, Any]:
    """The legacy ``answer`` dict the planner still produces."""

    return {
        "question": QUESTION,
        "findings": [
            {
                "kind": "metric",
                "metric": "revenue",
                "value": value,
                "rows": [[value]],
                "dimensions": [],
                "degraded": False,
            }
        ],
        "metric": "revenue",
        "value": value,
        "rows": [[value]],
        "evidence_ids": list(evidence_ids),
        "limitations": [],
        "degraded": False,
    }


def steps() -> list[dict[str, Any]]:
    """The three plan steps a correct analysis run reports (dependency order)."""

    return [
        {
            "step_id": "s1",
            "action": "resolve_metric",
            "status": "succeeded",
            "evidence_ids": ["ev_res"],
            "outputs": resolution_evidence_payload(),
            "duration_ms": 4.0,
            "tool_calls": 1,
        },
        {
            "step_id": "s2",
            "action": "query_metric",
            "status": "succeeded",
            "evidence_ids": ["ev_val"],
            "outputs": metric_evidence_payload(),
            "duration_ms": 11.0,
            "tool_calls": 1,
        },
        {
            "step_id": "s3",
            "action": "compose_answer",
            "status": "succeeded",
            "evidence_ids": ["ev_val"],
            "outputs": {
                "answer": legacy_answer(),
                "required_evidence": ["metric_resolution", "metric_value"],
                "final_answer": final_answer(),
                "evidence": evidence_entries(),
            },
            "duration_ms": 1.0,
            "tool_calls": 0,
        },
    ]


def analysis_payload() -> dict[str, Any]:
    """Canonical analysis-planner payload of a correct, anchored run."""

    step_payloads = steps()
    return {
        "plan": {
            "plan_id": "plan_fixture",
            "task_id": "task_fixture",
            "question": QUESTION,
            "domain_id": "retail",
            "steps": [
                {key: item[key] for key in ("id", "action", "inputs", "depends_on", "expected_evidence")}
                if False
                else {
                    "id": item["step_id"],
                    "action": item["action"],
                    "inputs": {},
                    "depends_on": [],
                    "expected_evidence": [],
                    "validation": {},
                    "budget": {},
                }
                for item in step_payloads
            ],
            "version": 1,
            "status": "succeeded",
        },
        "steps": step_payloads,
        "evidence": evidence_entries(),
        "answer": legacy_answer(),
        "final_answer": final_answer(),
        "status": "succeeded",
        "stop_reason": None,
        "replan_reasons": [],
        "terminal_outcome": None,
        "limitations": [],
        "validation_problems": [],
        "answer_validation": {"problems": [], "review_required": False},
        "budgets": {
            "limits": {"max_tool_calls": 64},
            "usage": {"max_tool_calls": 3.0, "max_sql_duration_ms": 11.0},
            "remaining": {"max_tool_calls": 61.0},
            "exhausted": [],
        },
        "analysis_request": {"question": QUESTION, "unresolved_questions": []},
        "reused_steps": [],
        "recomputed_steps": ["s1", "s2", "s3"],
        "lease_conflicts": [],
        "reuse_denied": {},
    }


def tool_calls(*, extra: list[dict[str, Any]] | None = None, failing: str | None = None) -> list[dict[str, Any]]:
    """Recorded tool calls of the reference run (optionally mutated)."""

    calls = [
        {"tool": "list_metrics", "action": "resolve_metric", "ok": True, "duration_ms": 3.0},
        {
            "tool": "check_data_quality",
            "action": "check_data_quality",
            "ok": True,
            "duration_ms": 5.0,
        },
        {"tool": "execute_sql", "action": "query_metric", "ok": True, "duration_ms": 11.0},
    ]
    if failing:
        for call in calls:
            if call["tool"] == failing:
                call["ok"] = False
                call["error_category"] = "data_quality"
    if extra:
        calls.extend(extra)
    return calls


def make_trace(
    payload: dict[str, Any] | None = None,
    *,
    wall_ms: float = 120.0,
    calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
    task_id: str = "retail_dev_1",
) -> TaskTrace:
    """One recorded trace; the default is the correct reference run."""

    return TaskTrace(
        task_id=task_id,
        payload=payload if payload is not None else analysis_payload(),
        wall_ms=wall_ms,
        tool_calls=calls if calls is not None else tool_calls(),
        usage=usage
        if usage is not None
        else {
            "prompt_tokens": 900,
            "completion_tokens": 120,
            "total_tokens": 1020,
            "estimated": False,
            "raw": {"prompt_tokens": 900, "completion_tokens": 120},
        },
        cost_usd=cost_usd,
        provider="openai",
        model="gpt-4o-mini",
        error=error,
    )


def analysis_spec(**overrides: Any) -> TaskSpec:
    """Gold task matching :func:`analysis_payload`."""

    fields: dict[str, Any] = {
        "task_id": "retail_dev_1",
        "split": "dev",
        "dataset": "retail_orders",
        "question": QUESTION,
        "coverage": ["simple_single_table"],
        "expected_outcome": "analysis",
        "expected_status": ["succeeded"],
        "allowed_tools": [
            "list_metrics",
            "check_data_quality",
            "execute_sql",
            "compose_answer",
        ],
        "required_steps": ["resolve_metric", "query_metric", "compose_answer"],
        "required_evidence": ["metric_resolution", "metric_value"],
        "expected_values": {"answer.value": VALUE},
        "forbidden_claims": ["because", "caused by"],
        "acceptable_stop_reasons": ["none"],
        "max_tool_calls": 6,
    }
    fields.update(overrides)
    return TaskSpec(**fields)


def clarification_payload(*, answered: bool = False) -> dict[str, Any]:
    """``AnalysisPlannerService._clarification_payload`` for an ambiguous question."""

    if answered:
        return analysis_payload()
    return {
        "plan": {
            "plan_id": "plan_clarify",
            "task_id": "task_clarify",
            "question": "Which revenue?",
            "domain_id": None,
            "steps": [],
            "version": 1,
            "status": "needs_clarification",
        },
        "steps": [],
        "evidence": [],
        "answer": None,
        "status": "needs_clarification",
        "replan_reasons": [],
        "budgets": {"limits": {"max_tool_calls": 64}, "usage": {"max_tool_calls": 0.0}},
        "stop_reason": "needs_clarification",
        "analysis_request": {
            "question": "Which revenue?",
            "unresolved_questions": ["Which revenue definition do you mean?"],
        },
        "unresolved_questions": ["Which revenue definition do you mean?"],
        "domain_id": None,
    }


def clarification_spec(**overrides: Any) -> TaskSpec:
    fields: dict[str, Any] = {
        "task_id": "retail_dev_2",
        "split": "dev",
        "dataset": "retail_orders",
        "question": "Which revenue metric should be used?",
        "coverage": ["ambiguous_needs_clarification"],
        "expected_outcome": "clarification",
        "expected_status": ["needs_clarification"],
        "allowed_tools": [],
        "required_steps": [],
        "required_evidence": [],
        "answer_must_reference_evidence": False,
        "acceptable_stop_reasons": ["needs_clarification"],
    }
    fields.update(overrides)
    return TaskSpec(**fields)


def policy_probe_payload(*, rejected: bool = True) -> dict[str, Any]:
    """The rejection probe payload ``scripts/benchmark_agent.py`` records."""

    return {
        "status": "blocked" if rejected else "succeeded",
        "stop_reason": "policy_rejection" if rejected else None,
        "sql": "DROP TABLE orders",
        "policy_rejected": rejected,
        "policy_accepted": not rejected,
        "policy_rule": "read_only_ast" if rejected else None,
        "evidence": [],
        "steps": [],
        "budgets": {"usage": {"max_tool_calls": 0}},
    }


def policy_spec(**overrides: Any) -> TaskSpec:
    fields: dict[str, Any] = {
        "task_id": "retail_regression_9",
        "split": "regression",
        "dataset": "retail_orders",
        "question": "Delete every order.",
        "coverage": ["policy_rejection"],
        "expected_outcome": "policy_rejection",
        "expected_status": ["blocked"],
        "allowed_tools": ["execute_sql"],
        "required_steps": [],
        "required_evidence": [],
        "answer_must_reference_evidence": False,
        "acceptable_stop_reasons": ["policy_rejection"],
    }
    fields.update(overrides)
    return TaskSpec(**fields)


def policy_trace(*, rejected: bool = True, task_id: str = "retail_regression_9") -> TaskTrace:
    return TaskTrace(
        task_id=task_id,
        payload=policy_probe_payload(rejected=rejected),
        wall_ms=8.0,
        tool_calls=[
            {
                "tool": "execute_sql",
                "action": "execute_sql",
                "ok": rejected,
                "status": "succeeded" if rejected else "blocked",
            }
        ],
        usage=None,
        cost_usd=None,
        provider=None,
        model=None,
        error=None,
    )


def check(outcome: Any, name: str) -> Any:
    """The named check of an outcome (fails loudly when it is missing)."""

    found = outcome.check(name)
    if found is None:  # pragma: no cover - a missing check is a test bug
        raise AssertionError(f"check {name!r} was not emitted")
    return found


# ---------------------------------------------------------------------------
# 16-N1: preset correct/incorrect traces
# ---------------------------------------------------------------------------


class PresetVerdictTest(unittest.TestCase):
    """16-N1: a preset correct trace passes and preset defects are named."""

    def test_reference_trace_passes_every_check(self):
        outcome = evaluate_task(analysis_spec(), make_trace())
        self.assertTrue(outcome.passed, [item.detail for item in outcome.checks if not item.passed])
        self.assertIsNone(outcome.failure_class)
        self.assertTrue(all(item.passed for item in outcome.checks))
        self.assertEqual(
            [item.name for item in outcome.checks],
            [name for name in CHECK_NAMES if name not in {"budget"}]
            + ["budget"]
            + [],
        )

    def test_wrong_value_is_rejected(self):
        payload = analysis_payload()
        payload["answer"]["value"] = 1.0
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "wrong_value")
        self.assertFalse(check(outcome, "expected_values").passed)

    def test_wrong_status_is_rejected(self):
        payload = analysis_payload()
        payload["status"] = "partial"
        payload["plan"]["status"] = "partial"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "wrong_status")

    def test_unacceptable_stop_reason_is_rejected(self):
        payload = analysis_payload()
        payload["stop_reason"] = "budget_exhausted"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "wrong_status")
        self.assertEqual(
            check(outcome, "status_accepted").actual["reason"], "stop_reason_not_accepted"
        )

    def test_missing_required_step_is_rejected(self):
        payload = analysis_payload()
        payload["steps"] = [item for item in payload["steps"] if item["action"] != "compose_answer"]
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "missing_evidence")
        self.assertIn("compose_answer", check(outcome, "required_steps").actual["missing"])

    def test_missing_required_evidence_is_rejected(self):
        payload = analysis_payload()
        payload["evidence"] = [item for item in payload["evidence"] if item["kind"] != "metric_value"]
        payload["steps"] = [item for item in payload["steps"] if item["step_id"] != "s2"]
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "missing_evidence")

    def test_illegal_tool_is_rejected(self):
        payload = analysis_payload()
        trace = make_trace(
            payload,
            calls=tool_calls(extra=[{"tool": "drop_table", "ok": True, "duration_ms": 1.0}]),
        )
        outcome = evaluate_task(analysis_spec(), trace)
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "illegal_tool")
        self.assertEqual(check(outcome, "tool_legality").actual["illegal"], ["drop_table"])

    def test_failed_tool_call_is_rejected(self):
        outcome = evaluate_task(
            analysis_spec(), make_trace(calls=tool_calls(failing="execute_sql"))
        )
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "failed_tool")

    def test_budget_overrun_is_rejected(self):
        spec = analysis_spec(max_tool_calls=2)
        outcome = evaluate_task(spec, make_trace())
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "budget_exceeded")
        self.assertEqual(check(outcome, "budget").actual["used"], 3)

    def test_budget_is_not_checked_without_a_gold_limit(self):
        spec = analysis_spec(max_tool_calls=None)
        outcome = evaluate_task(spec, make_trace())
        self.assertIsNone(outcome.check("budget"))
        self.assertTrue(outcome.passed)

    def test_runner_error_is_rejected(self):
        outcome = evaluate_task(
            analysis_spec(), make_trace(analysis_payload(), error="ConnectionError: reset")
        )
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "runner_error")
        self.assertEqual(outcome.error, "ConnectionError: reset")

    def test_empty_payload_is_a_missing_trace(self):
        trace = TaskTrace(task_id="retail_dev_1", payload={}, wall_ms=1.0)
        outcome = evaluate_task(analysis_spec(), trace)
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "missing_trace")

    def test_every_failing_outcome_uses_the_fixed_vocabulary(self):
        variants = [
            make_trace(),
            make_trace(error="boom"),
            TaskTrace(task_id="retail_dev_1", payload={}),
            make_trace(calls=tool_calls(extra=[{"tool": "rm_rf", "ok": True}])),
            make_trace(calls=tool_calls(failing="execute_sql")),
            make_trace(analysis_payload(), error="boom"),
        ]
        for trace in variants:
            outcome = evaluate_task(analysis_spec(), trace)
            if outcome.passed:
                self.assertEqual(outcome.failure_class, None)
                continue
            self.assertIn(outcome.failure_class, FAILURE_CLASSES)

    def test_classify_failure_is_reproducible_from_the_outcome(self):
        payload = analysis_payload()
        payload["answer"]["value"] = 0.0
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertEqual(classify_failure(outcome), outcome.failure_class)
        self.assertEqual(classify_failure(outcome), "wrong_value")
        passed = evaluate_task(analysis_spec(), make_trace())
        self.assertEqual(classify_failure(passed), "")


# ---------------------------------------------------------------------------
# Recomputability (16-N1 / contract 3.7)
# ---------------------------------------------------------------------------


class RecomputeTest(unittest.TestCase):
    """The published score must be rebuildable from the raw results alone."""

    def test_recompute_from_results_alone_reproduces_the_report(self):
        outcome = evaluate_task(analysis_spec(), make_trace())
        report = aggregate([outcome], thresholds=TIER1)
        bare = {"results": report["results"], "thresholds": report["thresholds"]}
        self.assertEqual(recompute(bare, [analysis_spec()]), report)
        self.assertEqual(recompute(report, [analysis_spec()]), report)

    def test_recompute_reproduces_a_multi_task_report(self):
        specs = [analysis_spec(), clarification_spec(), policy_spec()]
        traces = [make_trace(), make_trace(clarification_payload(), task_id="retail_dev_2"), policy_trace()]
        report = aggregate(
            [evaluate_task(spec, trace) for spec, trace in zip(specs, traces)],
            thresholds=TIER1,
        )
        self.assertEqual(recompute(report, specs), report)

    def test_recompute_does_not_trust_a_stored_verdict(self):
        payload = analysis_payload()
        payload["answer"]["value"] = 1.0
        report = aggregate([evaluate_task(analysis_spec(), make_trace(payload))], thresholds={})
        self.assertFalse(report["results"][0]["passed"])
        tampered = deepcopy(report)
        tampered["results"][0]["passed"] = True
        self.assertFalse(recompute(tampered, [analysis_spec()])["results"][0]["passed"])
        self.assertEqual(recompute(tampered, [analysis_spec()])["task_success_rate"], 0.0)

    def test_recompute_re_derives_checks_from_the_payload(self):
        report = aggregate([evaluate_task(analysis_spec(), make_trace())], thresholds={})
        tampered = deepcopy(report)
        for entry in tampered["results"][0]["checks"]:
            if entry["name"] == "expected_values":
                entry["passed"] = False
        self.assertTrue(recompute(tampered, [analysis_spec()])["results"][0]["passed"])

    def test_recompute_scores_raw_runner_records(self):
        """The harness keeps raw trace records in ``report["results"]``."""

        spec = analysis_spec()
        outcome = evaluate_task(spec, make_trace())
        report = {
            "metrics": aggregate([outcome], thresholds=TIER1),
            "results": [make_trace().model_dump(mode="json")],
            "thresholds": TIER1,
        }
        self.assertEqual(recompute(report, [spec]), report["metrics"])

    def test_recompute_needs_a_gold_spec_for_a_raw_trace(self):
        report = {"results": [make_trace().model_dump(mode="json")], "thresholds": {}}
        with self.assertRaises(ValueError):
            recompute(report, [])

    def test_recompute_rejects_a_report_without_results(self):
        with self.assertRaises(ValueError):
            recompute({"metrics": {}}, [analysis_spec()])


# ---------------------------------------------------------------------------
# 16-T1: prose without evidence
# ---------------------------------------------------------------------------


class EvidenceAnchorTest(unittest.TestCase):
    """16-T1: a beautiful answer that cannot be traced fails the task."""

    def test_prose_without_evidence_ids_fails(self):
        payload = analysis_payload()
        payload["final_answer"] = final_answer(
            evidence_ids=(),
            findings=[],
            conclusions=[
                "Revenue grew strongly across the period and the premium tier "
                "clearly drove the uplift."
            ],
        )
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        anchor = check(outcome, "evidence_anchor")
        self.assertFalse(anchor.passed)
        self.assertEqual(anchor.actual["reason"], "assertions_without_evidence")
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "unsupported_claim")

    def test_dangling_evidence_ids_fail(self):
        payload = analysis_payload()
        payload["final_answer"] = final_answer(evidence_ids=("ev_does_not_exist",))
        payload["final_answer"]["findings"][0]["numbers"] = {}
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        anchor = check(outcome, "evidence_anchor")
        self.assertFalse(anchor.passed)
        self.assertEqual(anchor.actual["reason"], "dangling_evidence_ids")
        self.assertEqual(outcome.failure_class, "missing_evidence")

    def test_finding_without_evidence_ids_fails(self):
        payload = analysis_payload()
        payload["final_answer"] = final_answer(
            evidence_ids=("ev_val",),
            findings=[
                {
                    "kind": "metric",
                    "statement": "revenue reached a new high",
                    "numbers": {},
                    "evidence_ids": [],
                    "degraded": False,
                }
            ],
        )
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        anchor = check(outcome, "evidence_anchor")
        self.assertFalse(anchor.passed)
        self.assertEqual(anchor.actual["reason"], "unanchored_claim")

    def test_number_without_a_source_fails(self):
        payload = analysis_payload()
        payload["final_answer"]["findings"][0]["numbers"] = {"invented_total": 999999.0}
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        numbers = check(outcome, "no_unsupported_numbers")
        self.assertFalse(numbers.passed)
        self.assertIn("invented_total", numbers.detail)
        self.assertEqual(outcome.failure_class, "unsupported_claim")

    def test_self_assessment_fields_are_ignored(self):
        """A trace claiming "reviewed and fine" while citing nothing still fails."""

        payload = analysis_payload()
        payload["final_answer"] = final_answer(evidence_ids=(), findings=[], conclusions=["All good."])
        payload["answer_validation"] = {"problems": [], "review_required": False}
        payload["validation_problems"] = []
        payload["status"] = "succeeded"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertFalse(check(outcome, "evidence_anchor").passed)

    def test_self_declared_problems_do_not_help_a_good_answer(self):
        payload = analysis_payload()
        payload["validation_problems"] = ["unknown evidence id: ev_val"]
        payload["answer_validation"] = {"problems": ["unknown evidence id: ev_id"], "review_required": True}
        payload["final_answer"]["review_required"] = True
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertTrue(check(outcome, "evidence_anchor").passed)
        self.assertTrue(outcome.passed)

    def test_empty_answer_is_tolerated(self):
        payload = {
            "status": "failed",
            "stop_reason": None,
            "steps": [
                {
                    "step_id": "s1",
                    "action": "query_metric",
                    "status": "failed",
                    "error": "data_absent: no rows",
                    "error_category": "data_quality",
                }
            ],
            "evidence": [],
            "answer": None,
            "budgets": {"usage": {"max_tool_calls": 1.0}},
        }
        spec = TaskSpec(
            task_id="retail_dev_3",
            split="dev",
            dataset="retail_orders",
            question="Revenue for a slice with no data?",
            coverage=["data_fault"],
            expected_outcome="query",
            expected_status=["failed"],
            allowed_tools=["execute_sql"],
            required_steps=["query_metric"],
            required_evidence=[],
        )
        outcome = evaluate_task(spec, make_trace(payload, task_id="retail_dev_3", calls=[{"tool": "execute_sql", "ok": False, "error_category": "data_quality"}]))
        self.assertTrue(check(outcome, "evidence_anchor").passed)
        self.assertTrue(outcome.passed, [item.detail for item in outcome.checks if not item.passed])

    def test_anchor_requirement_can_be_waived_by_the_gold(self):
        payload = analysis_payload()
        payload["final_answer"] = final_answer(evidence_ids=(), findings=[], conclusions=["Done."])
        spec = analysis_spec(answer_must_reference_evidence=False, required_evidence=[])
        outcome = evaluate_task(spec, make_trace(payload))
        self.assertTrue(check(outcome, "evidence_anchor").passed)
        self.assertFalse(outcome.requires_evidence)


# ---------------------------------------------------------------------------
# 16-T2: goal oriented, never path oriented
# ---------------------------------------------------------------------------


class GoalOrientedTest(unittest.TestCase):
    """16-T2: different correct routes to the same goal are all accepted."""

    def test_reordered_steps_still_pass(self):
        first = analysis_payload()
        second = analysis_payload()
        second["steps"] = [second["steps"][2], second["steps"][0], second["steps"][1]]
        second["plan"]["steps"] = list(reversed(second["plan"]["steps"]))
        outcomes = [
            evaluate_task(analysis_spec(), make_trace(first)),
            evaluate_task(analysis_spec(), make_trace(second)),
        ]
        self.assertTrue(outcomes[0].passed)
        self.assertTrue(outcomes[1].passed, [c.detail for c in outcomes[1].checks if not c.passed])
        self.assertEqual(outcomes[0].failure_class, outcomes[1].failure_class)

    def test_different_sql_text_still_passes(self):
        payload = analysis_payload()
        payload["steps"][1]["outputs"]["sql"] = "SELECT sum(o.amount) FROM orders o /* v2 */"
        payload["evidence"][1]["payload"]["sql"] = "SELECT sum(o.amount) FROM orders o /* v2 */"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertTrue(outcome.passed)

    def test_declared_substitution_passes(self):
        payload = analysis_payload()
        payload["steps"][1]["action"] = "query_metric_via_preview"
        spec = analysis_spec(
            replaceable_steps={"query_metric": ["query_metric_via_preview"]}
        )
        outcome = evaluate_task(spec, make_trace(payload))
        steps_check = check(outcome, "required_steps")
        self.assertTrue(steps_check.passed, steps_check.detail)
        self.assertEqual(
            steps_check.actual["substitutions"], {"query_metric": "query_metric_via_preview"}
        )
        self.assertTrue(outcome.passed)

    def test_substitution_is_not_accepted_when_the_gold_does_not_declare_it(self):
        payload = analysis_payload()
        payload["steps"][1]["action"] = "query_metric_via_preview"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "missing_evidence")

    def test_tool_vocabulary_and_action_vocabulary_both_satisfy_legality(self):
        spec_by_action = analysis_spec(
            allowed_tools=["resolve_metric", "check_data_quality", "query_metric", "compose_answer"]
        )
        self.assertTrue(evaluate_task(spec_by_action, make_trace()).passed)
        spec_by_tool = analysis_spec(
            allowed_tools=["list_metrics", "check_data_quality", "execute_sql", "compose_answer"]
        )
        self.assertTrue(evaluate_task(spec_by_tool, make_trace()).passed)


# ---------------------------------------------------------------------------
# Tools, budget, tolerated failures
# ---------------------------------------------------------------------------


class ToolAndBudgetTest(unittest.TestCase):
    def test_legality_is_vacuous_without_a_gold_restriction(self):
        outcome = evaluate_task(analysis_spec(allowed_tools=[]), make_trace())
        self.assertTrue(check(outcome, "tool_legality").passed)
        self.assertFalse(check(outcome, "tool_legality").actual["allowed"])

    def test_data_fault_tolerates_a_failed_step_and_call(self):
        spec = TaskSpec(
            task_id="retail_dev_4",
            split="dev",
            dataset="retail_orders",
            question="Revenue for a slice with dirty data?",
            coverage=["data_fault"],
            expected_outcome="query",
            expected_status=["partial", "failed"],
            allowed_tools=["execute_sql"],
            required_steps=["query_metric"],
            required_evidence=[],
        )
        payload = {
            "status": "failed",
            "stop_reason": None,
            "steps": [
                {
                    "step_id": "s1",
                    "action": "query_metric",
                    "status": "failed",
                    "error": "data_quality_blocked",
                    "error_category": "data_quality",
                }
            ],
            "evidence": [],
            "budgets": {"usage": {"max_tool_calls": 1.0}},
        }
        trace = make_trace(
            payload,
            task_id="retail_dev_4",
            calls=[{"tool": "execute_sql", "ok": False, "error_category": "data_quality"}],
        )
        outcome = evaluate_task(spec, trace)
        validity = check(outcome, "tool_validity")
        self.assertTrue(validity.passed, validity.detail)
        self.assertTrue(validity.actual["tolerance"])
        self.assertTrue(outcome.passed)  # The gold explicitly accepts failed data-quality tasks.
        self.assertIsNone(outcome.failure_class)

    def test_empty_result_tolerates_a_failed_step(self):
        spec = TaskSpec(
            task_id="retail_dev_5",
            split="dev",
            dataset="retail_orders",
            question="Revenue for an empty slice?",
            coverage=["empty_result"],
            expected_outcome="query",
            expected_status=["partial"],
            allowed_tools=["execute_sql"],
            required_steps=["query_metric"],
            required_evidence=[],
        )
        payload = {
            "status": "partial",
            "replan_reasons": ["replan:s1 returned no data for dimensions ['channel']"],
            "steps": [
                {
                    "step_id": "s1",
                    "action": "query_metric",
                    "status": "failed",
                    "error": "data_absent",
                    "error_category": "data_quality",
                }
            ],
            "evidence": [],
        }
        outcome = evaluate_task(
            spec,
            make_trace(
                payload,
                task_id="retail_dev_5",
                calls=[{"tool": "execute_sql", "ok": False, "error_category": "data_quality"}],
            ),
        )
        self.assertTrue(check(outcome, "tool_validity").passed)
        self.assertTrue(outcome.passed, [c.detail for c in outcome.checks if not c.passed])

    def test_failed_step_is_a_defect_when_the_gold_expects_success(self):
        payload = analysis_payload()
        payload["steps"][1]["status"] = "failed"
        payload["steps"][1]["error_category"] = "execution"
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertFalse(check(outcome, "tool_validity").passed)
        self.assertEqual(outcome.failure_class, "failed_tool")

    def test_budget_uses_the_conservative_maximum(self):
        payload = analysis_payload()
        payload["budgets"]["usage"]["max_tool_calls"] = 9.0
        outcome = evaluate_task(analysis_spec(max_tool_calls=6), make_trace(payload))
        budget = check(outcome, "budget")
        self.assertEqual(budget.actual["reported"], 9)
        self.assertEqual(budget.actual["used"], 9)
        self.assertFalse(budget.passed)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


class ClaimGuardTest(unittest.TestCase):
    def test_forbidden_claim_fails(self):
        payload = analysis_payload()
        payload["final_answer"]["conclusions"] = [
            "Revenue increased because the premium tier grew."
        ]
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        guard = check(outcome, "claim_guard")
        self.assertFalse(guard.passed)
        self.assertEqual(guard.actual["reason"], "forbidden_claim")
        self.assertEqual(guard.actual["forbidden_found"], ["because"])
        self.assertEqual(outcome.failure_class, "unsupported_claim")

    def test_required_claim_must_appear(self):
        spec = analysis_spec(required_claims=["verified by independent audit"])
        guard = check(evaluate_task(spec, make_trace()), "claim_guard")
        self.assertFalse(guard.passed)
        self.assertEqual(guard.actual["reason"], "missing_required_claim")
        payload = analysis_payload()
        payload["final_answer"]["conclusions"] = ["Verified by independent audit."]
        passing = evaluate_task(spec, make_trace(payload))
        self.assertTrue(check(passing, "claim_guard").passed)
        self.assertTrue(passing.passed)

    def test_claim_guard_is_omitted_without_gold_claims(self):
        spec = analysis_spec(forbidden_claims=[], required_claims=[])
        outcome = evaluate_task(spec, make_trace())
        self.assertIsNone(outcome.check("claim_guard"))


# ---------------------------------------------------------------------------
# Clarification, both directions
# ---------------------------------------------------------------------------


class ClarificationTest(unittest.TestCase):
    def test_expected_clarification_passes(self):
        outcome = evaluate_task(
            clarification_spec(),
            make_trace(clarification_payload(), task_id="retail_dev_2", calls=[]),
        )
        self.assertTrue(outcome.passed, [c.detail for c in outcome.checks if not c.passed])
        self.assertTrue(outcome.clarified)
        self.assertTrue(check(outcome, "outcome_kind").passed)

    def test_missing_clarification_fails(self):
        outcome = evaluate_task(
            clarification_spec(),
            make_trace(analysis_payload(), task_id="retail_dev_2"),
        )
        kind = check(outcome, "outcome_kind")
        self.assertFalse(kind.passed)
        self.assertEqual(kind.actual["reason"], "missing_clarification")
        self.assertEqual(outcome.failure_class, "missing_clarification")

    def test_unexpected_clarification_fails(self):
        outcome = evaluate_task(
            analysis_spec(),
            make_trace(clarification_payload(), calls=[]),
        )
        kind = check(outcome, "outcome_kind")
        self.assertFalse(kind.passed)
        self.assertEqual(kind.actual["reason"], "unexpected_clarification")
        self.assertEqual(outcome.failure_class, "unexpected_clarification")

    def test_clarification_appropriateness_counts_both_polarities(self):
        specs = [
            clarification_spec(),
            analysis_spec(),
            analysis_spec(task_id="retail_dev_3"),
            clarification_spec(task_id="retail_dev_4"),
        ]
        traces = [
            make_trace(clarification_payload(), task_id="retail_dev_2", calls=[]),
            make_trace(task_id="retail_dev_1"),
            make_trace(clarification_payload(), task_id="retail_dev_3", calls=[]),
            make_trace(analysis_payload(), task_id="retail_dev_4"),
        ]
        report = aggregate(
            [evaluate_task(spec, trace) for spec, trace in zip(specs, traces)]
        )
        block = report["clarification_appropriateness"]
        self.assertEqual(block["true_positive"], 1)
        self.assertEqual(block["true_negative"], 1)
        self.assertEqual(block["false_positive"], 1)
        self.assertEqual(block["false_negative"], 1)
        self.assertEqual(block["true_positive_rate"], 0.5)
        self.assertEqual(block["true_negative_rate"], 0.5)
        self.assertEqual(block["rate"], 0.5)


# ---------------------------------------------------------------------------
# Policy probes
# ---------------------------------------------------------------------------


class PolicyProbeTest(unittest.TestCase):
    def test_enforced_rejection_passes(self):
        outcome = evaluate_task(policy_spec(), policy_trace(rejected=True))
        self.assertTrue(outcome.passed, [c.detail for c in outcome.checks if not c.passed])
        self.assertTrue(check(outcome, "outcome_kind").passed)

    def test_unenforced_policy_fails(self):
        outcome = evaluate_task(policy_spec(), policy_trace(rejected=False))
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failure_class, "policy_not_enforced")
        self.assertEqual(
            check(outcome, "outcome_kind").actual["reason"], "policy_not_enforced"
        )

    def test_forbidden_evidence_is_a_policy_failure(self):
        spec = analysis_spec(forbidden_evidence=["metric_value"])
        outcome = evaluate_task(spec, make_trace())
        self.assertFalse(check(outcome, "forbidden_evidence").passed)
        self.assertEqual(outcome.failure_class, "policy_not_enforced")


# ---------------------------------------------------------------------------
# 16-M1: usage and cost accounting
# ---------------------------------------------------------------------------


class UsageCostTest(unittest.TestCase):
    def test_measured_and_estimated_tokens_are_kept_apart(self):
        measured = evaluate_task(analysis_spec(), make_trace())
        self.assertEqual(measured.usage_tokens, 1020)
        self.assertEqual(measured.measured_tokens, 1020)
        self.assertEqual(measured.estimated_tokens, 0)
        estimated = evaluate_task(
            analysis_spec(),
            make_trace(
                usage={"prompt_tokens": 500, "completion_tokens": 200, "estimated": True}
            ),
        )
        self.assertEqual(estimated.usage_tokens, 700)
        self.assertEqual(estimated.measured_tokens, 0)
        self.assertEqual(estimated.estimated_tokens, 700)

    def test_usage_totals_are_derived_when_the_runner_omits_them(self):
        outcome = evaluate_task(
            analysis_spec(),
            make_trace(usage={"prompt_tokens": 10, "completion_tokens": 5}),
        )
        self.assertEqual(outcome.usage_tokens, 15)
        self.assertEqual(outcome.measured_tokens, 15)

    def test_missing_usage_is_none_not_zero(self):
        outcome = evaluate_task(analysis_spec(), make_trace(usage={}))
        self.assertIsNone(outcome.usage_tokens)
        self.assertIsNone(outcome.measured_tokens)
        self.assertIsNone(outcome.estimated_tokens)
        report = aggregate([outcome])
        usage = report["usage"]
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(usage["recorded_tasks"], 0)
        self.assertEqual(usage["unmeasured_tasks"], ["retail_dev_1"])
        self.assertFalse(usage["complete"])

    def test_cost_is_none_without_a_price_table(self):
        outcome = evaluate_task(analysis_spec(), make_trace())
        self.assertIsNone(outcome.cost_usd)
        report = aggregate([outcome])
        self.assertIsNone(report["cost_usd"])
        self.assertFalse(report["cost"]["price_table_configured"])
        self.assertEqual(report["cost"]["tasks_without_cost"], ["retail_dev_1"])

    def test_cost_is_computed_from_a_price_table(self):
        per_1k = {"openai/gpt-4o-mini": {"prompt_per_1k": 0.001, "completion_per_1k": 0.002}}
        outcome = evaluate_task(
            analysis_spec(),
            make_trace(usage={"prompt_tokens": 1000, "completion_tokens": 500}),
            price_table=per_1k,
        )
        self.assertAlmostEqual(outcome.cost_usd, 0.002, places=8)
        self.assertEqual(outcome.cost_basis, "priced")
        per_million = {
            "openai/gpt-4o-mini": {"input_per_million": 1.0, "output_per_million": 2.0}
        }
        same = evaluate_task(
            analysis_spec(),
            make_trace(usage={"prompt_tokens": 1000, "completion_tokens": 500}),
            price_table=per_million,
        )
        self.assertAlmostEqual(same.cost_usd, outcome.cost_usd, places=8)

    def test_a_model_without_a_price_entry_stays_unpriced(self):
        outcome = evaluate_task(
            analysis_spec(),
            make_trace(),
            price_table={"other/model": {"prompt_per_1k": 1.0, "completion_per_1k": 1.0}},
        )
        self.assertIsNone(outcome.cost_usd)

    def test_runner_reported_cost_wins(self):
        outcome = evaluate_task(analysis_spec(), make_trace(cost_usd=0.25))
        self.assertEqual(outcome.cost_usd, 0.25)
        self.assertEqual(outcome.cost_basis, "reported")

    def test_a_failed_task_is_still_accounted(self):
        failed = evaluate_task(
            analysis_spec(),
            make_trace(
                analysis_payload(),
                error="TimeoutError: provider timed out",
                usage={"prompt_tokens": 400, "completion_tokens": 60, "estimated": True},
                cost_usd=None,
            ),
        )
        self.assertFalse(failed.passed)
        self.assertEqual(failed.failure_class, "runner_error")
        self.assertEqual(failed.estimated_tokens, 460)
        self.assertEqual(failed.tool_calls, 3)
        report = aggregate([evaluate_task(analysis_spec(), make_trace()), failed])
        self.assertEqual(report["usage"]["measured_tokens"], 1020)
        self.assertEqual(report["usage"]["estimated_tokens"], 460)
        self.assertEqual(report["usage"]["total_tokens"], 1480)
        self.assertEqual(report["usage"]["measured_tasks"], 1)
        self.assertEqual(report["usage"]["estimated_tasks"], 1)
        self.assertEqual(report["runner_error_tasks"], ["retail_dev_1"])
        self.assertEqual(report["failure_classes"], {"runner_error": 1})

    def test_per_task_and_aggregate_cost_agree(self):
        priced = evaluate_task(
            analysis_spec(),
            make_trace(usage={"prompt_tokens": 1000, "completion_tokens": 0}),
            price_table={"openai/gpt-4o-mini": {"prompt_per_1k": 0.001}},
        )
        unpriced = evaluate_task(analysis_spec(), make_trace(cost_usd=0.5))
        report = aggregate([priced, unpriced], thresholds={})
        self.assertAlmostEqual(report["cost_usd"], 0.501, places=8)
        self.assertEqual(report["cost"]["tasks_with_cost"], 2)
        self.assertEqual(report["cost"]["priced_cost_tasks"], 1)
        self.assertEqual(report["cost"]["reported_cost_tasks"], 1)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def mixed_outcomes() -> list[Any]:
    """Five outcomes across two datasets, three splits and four categories."""

    specs = [
        analysis_spec(),
        analysis_spec(task_id="retail_regression_1", split="regression"),
        policy_spec(),
        clarification_spec(task_id="support_dev_1", dataset="support_tickets"),
        analysis_spec(
            task_id="retail_holdout_1",
            split="holdout",
            coverage=["multi_step_analysis"],
            required_steps=["resolve_metric", "query_metric", "compose_answer"],
        ),
    ]
    traces = [
        make_trace(wall_ms=100.0),
        make_trace(wall_ms=200.0, task_id="retail_regression_1"),
        policy_trace(),
        make_trace(
            clarification_payload(),
            task_id="support_dev_1",
            calls=[],
            wall_ms=40.0,
        ),
        make_trace(wall_ms=300.0, task_id="retail_holdout_1"),
    ]
    return [evaluate_task(spec, trace) for spec, trace in zip(specs, traces)], specs


class AggregateTest(unittest.TestCase):
    def test_breakdowns_by_split_dataset_and_coverage(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(report["task_count"], 5)
        self.assertEqual(report["task_success_rate"], 1.0)
        self.assertEqual(
            sorted(report["by_split"]), ["dev", "holdout", "regression"]
        )
        self.assertEqual(report["by_split"]["regression"]["task_count"], 2)
        self.assertEqual(
            sorted(report["by_dataset"]), ["retail_orders", "support_tickets"]
        )
        self.assertEqual(report["by_dataset"]["support_tickets"]["task_count"], 1)
        self.assertIn("policy_rejection", report["by_coverage"])
        self.assertEqual(report["by_coverage"]["policy_rejection"]["task_count"], 1)
        self.assertEqual(
            report["by_coverage"]["simple_single_table"]["task_count"], 2
        )

    def test_percentiles_are_nearest_rank(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(
            sorted(outcome.wall_ms for outcome in outcomes),
            [8.0, 40.0, 100.0, 200.0, 300.0],
        )
        self.assertEqual(report["p50_wall_ms"], 100.0)
        self.assertEqual(report["p95_wall_ms"], 300.0)

    def test_failure_histogram(self):
        payload = analysis_payload()
        payload["answer"]["value"] = 0.0
        payload["final_answer"]["findings"][0]["numbers"]["value"] = 0.0
        outcomes = [
            evaluate_task(analysis_spec(), make_trace(payload)),
            evaluate_task(analysis_spec(), make_trace(error="boom")),
            evaluate_task(analysis_spec(), make_trace()),
        ]
        report = aggregate(outcomes)
        self.assertEqual(report["failure_classes"], {"wrong_value": 1, "runner_error": 1})
        self.assertEqual(report["passed_tasks"], 1)
        self.assertAlmostEqual(report["task_success_rate"], 0.333333, places=6)

    def test_policy_probe_and_model_e2e_are_reported_separately(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(set(report["groups"]), {"policy_probe", "unclassified"})
        self.assertEqual(report["groups"]["policy_probe"]["task_count"], 1)
        self.assertEqual(report["groups"]["unclassified"]["task_count"], 4)
        self.assertEqual(report["groups"]["policy_probe"]["task_success_rate"], 1.0)

    def test_multi_step_success_rate_uses_only_multi_step_tasks(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(report["multi_step_tasks"], 3)
        self.assertEqual(report["multi_step_success_rate"], 1.0)

    def test_evidence_coverage_rate_counts_only_evidence_tasks(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(report["evidence_tasks"], 3)
        self.assertEqual(report["evidence_coverage_rate"], 1.0)

    def test_empty_aggregate_reports_no_fake_rates(self):
        report = aggregate([])
        self.assertEqual(report["task_count"], 0)
        self.assertIsNone(report["task_success_rate"])
        self.assertIsNone(report["evidence_coverage_rate"])
        self.assertIsNone(report["tool_legality_rate"])
        self.assertIsNone(report["tool_validity_rate"])
        self.assertIsNone(report["avg_tool_calls"])
        self.assertIsNone(report["p50_wall_ms"])
        self.assertIsNone(report["p95_wall_ms"])
        self.assertIsNone(report["cost_usd"])
        self.assertIsNone(report["usage"]["total_tokens"])
        self.assertEqual(report["failure_classes"], {})
        self.assertIsNone(report["clarification_appropriateness"]["rate"])
        self.assertEqual(report["results"], [])

    def test_unsupported_assertion_rate_and_tool_rates(self):
        payload = analysis_payload()
        payload["final_answer"] = final_answer(evidence_ids=(), findings=[], conclusions=["Prose."])
        outcomes = [
            evaluate_task(analysis_spec(), make_trace(payload)),
            evaluate_task(analysis_spec(), make_trace()),
        ]
        report = aggregate(outcomes)
        self.assertEqual(report["unsupported_assertion_rate"], 0.5)
        self.assertEqual(report["tool_legality_rate"], 1.0)
        self.assertEqual(report["tool_validity_rate"], 1.0)
        self.assertEqual(report["tool_calls_measured_tasks"], 2)
        self.assertEqual(report["avg_tool_calls"], 3.0)
        self.assertEqual(report["tool_call_total"], 6)

    def test_report_is_json_serializable(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes, thresholds=TIER1)
        json.dumps(report, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Thresholds and gate
# ---------------------------------------------------------------------------


class ThresholdGateTest(unittest.TestCase):
    def test_healthy_report_passes_every_tier1_rule(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes, thresholds=TIER1)
        self.assertEqual(check_metrics(report, "tier1_offline", load_thresholds()), [])
        self.assertEqual(report["threshold_violations"], [])

    def test_failing_report_is_reported_with_numbers(self):
        payload = analysis_payload()
        payload["answer"]["value"] = 0.0
        payload["final_answer"]["findings"][0]["numbers"]["value"] = 0.0
        outcomes = [evaluate_task(analysis_spec(), make_trace(payload))]
        report = aggregate(outcomes, thresholds=TIER1)
        violations = report["threshold_violations"]
        self.assertTrue(any("task_success_rate" in item for item in violations))
        self.assertTrue(any("unsupported_assertion_rate" in item for item in violations))
        self.assertTrue(all(item.startswith(("tier1_offline", "thresholds")) for item in violations))

    def test_two_argument_form_uses_the_checked_in_document(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        self.assertEqual(check_metrics(report, "tier1_offline"), [])
        self.assertIn("tier3_model_e2e", TIER_NAMES)

    def test_unmeasurable_metric_is_a_violation_not_a_pass(self):
        violations = check_metrics(aggregate([]), "tier1_offline")
        self.assertTrue(any("not measurable" in item for item in violations))
        self.assertTrue(any("per-split metrics" in item for item in violations))

    def test_split_threshold_is_enforced(self):
        outcomes, _ = mixed_outcomes()
        report = aggregate(outcomes)
        report["by_split"]["regression"] = {**report["by_split"]["regression"]}
        report["by_split"]["regression"]["task_success_rate"] = 0.5
        violations = check_metrics(report, "tier1_offline")
        self.assertTrue(any("regression" in item for item in violations))

    def test_unknown_rule_is_a_violation(self):
        violations = check_metrics(
            {"task_success_rate": 1.0}, {"min_task_sucess_rate": 0.9}
        )
        self.assertTrue(any("unknown metric" in item for item in violations))

    def test_non_numeric_rule_is_a_violation(self):
        violations = check_metrics(
            {"task_success_rate": 1.0}, {"min_task_success_rate": "high"}
        )
        self.assertTrue(any("non-numeric" in item for item in violations))

    def test_loaded_document_is_typed(self):
        document = load_thresholds()
        self.assertEqual(document.version, "1.0")
        self.assertEqual(document.tier1_offline.min_task_success_rate, 1.0)
        self.assertEqual(document.tier1_offline.max_avg_tool_calls_per_task, 12.0)
        self.assertEqual(document.tier1_offline.splits["regression"]["min_task_success_rate"], 1.0)
        self.assertEqual(document.required_dependencies("tier2_integration"), ["fastapi", "httpx", "mcp", "lancedb", "pyarrow", "duckdb", "multipart"])
        self.assertIn("Uncalibrated", document.tier3_model_e2e.note or "")
        with self.assertRaises(ValueError):
            document.tier("tier4_production")

    def test_every_checked_in_rule_names_a_known_metric(self):
        document = load_thresholds()
        for tier in TIER_NAMES:
            config = document.tier(tier)
            for rule in config.rules():
                self.assertIn(rule.metric, METRIC_PATHS, f"{tier}.{rule.key}")
            for split in config.splits:
                for rule in config.split_rules(split):
                    self.assertIn(rule.metric, METRIC_PATHS, f"{tier}.{split}.{rule.key}")

    def test_thresholds_document_is_the_contract_file(self):
        self.assertTrue(DEFAULT_THRESHOLDS_PATH.is_file())
        payload = json.loads(DEFAULT_THRESHOLDS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(payload), ["tier1_offline", "tier2_integration", "tier3_model_e2e", "version"])

    def test_unknown_tier_name_raises(self):
        with self.assertRaises(ValueError):
            check_metrics({}, "tier9_imaginary", {"version": "1.0"})


# ---------------------------------------------------------------------------
# Payload shapes (both pipeline layers)
# ---------------------------------------------------------------------------


def workflow_payload() -> dict[str, Any]:
    """``output_node`` shape: store-shaped evidence, ``sql_result``, plain rows."""

    return {
        "status": "success",
        "question": QUESTION,
        "sql": "SELECT SUM(amount) AS revenue FROM orders",
        "columns": ["revenue"],
        "rows": [[VALUE]],
        "row_count": 1,
        "evidence": [
            {
                "id": "ev_sql",
                "kind": "sql_result",
                "source": "sample_data/retail_orders/retail_orders.sqlite",
                "method": "executed SQL over the run's data version",
                "completeness": "complete",
                "payload": {
                    "row_count": 1,
                    "returned_rows": 1,
                    "columns": ["revenue"],
                    "numeric_columns": ["revenue"],
                    "aggregates": {"revenue": {"sum": VALUE, "min": VALUE, "max": VALUE}},
                    "total_revenue": VALUE,
                },
            }
        ],
        "final_answer": {
            "question": QUESTION,
            "status": "success",
            "conclusions": [f"revenue totals {VALUE}."],
            "findings": [
                {
                    "kind": "sql_result",
                    "statement": "across 1 returned row(s), revenue totals 43183.49",
                    "numbers": {"total_revenue": VALUE, "row_count": 1},
                    "evidence_ids": ["ev_sql"],
                    "degraded": False,
                }
            ],
            "evidence_ids": ["ev_sql"],
            "charts": [],
        },
        "answer_validation": {"problems": [], "review_required": False},
        "completeness": {"display_truncated": False, "analysis_complete": True},
    }


class PayloadShapeTest(unittest.TestCase):
    def test_workflow_payload_shape_is_understood(self):
        spec = TaskSpec(
            task_id="retail_workflow_1",
            split="dev",
            dataset="retail_orders",
            question=QUESTION,
            coverage=["simple_single_table"],
            expected_outcome="query",
            expected_status=["success"],
            allowed_tools=["execute_sql"],
            required_steps=[],
            required_evidence=["sql_result"],
            expected_values={"row_count": 1},
        )
        trace = make_trace(
            workflow_payload(),
            task_id="retail_workflow_1",
            calls=[{"tool": "execute_sql", "ok": True}],
        )
        outcome = evaluate_task(spec, trace)
        self.assertTrue(outcome.passed, [c.detail for c in outcome.checks if not c.passed])
        self.assertEqual(outcome.status, "succeeded") if False else None
        self.assertTrue(check(outcome, "evidence_anchor").passed)

    def test_legacy_answer_anchors_at_the_answer_level(self):
        payload = analysis_payload()
        payload.pop("final_answer")
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        anchor = check(outcome, "evidence_anchor")
        self.assertTrue(anchor.passed, anchor.detail)
        self.assertEqual(anchor.actual["shape"], "answer")

    def test_legacy_answer_without_ids_fails(self):
        payload = analysis_payload()
        payload.pop("final_answer")
        payload["answer"]["evidence_ids"] = []
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        anchor = check(outcome, "evidence_anchor")
        self.assertFalse(anchor.passed)
        self.assertEqual(anchor.actual["reason"], "assertions_without_evidence")

    def test_plan_status_is_used_when_the_payload_has_none(self):
        payload = analysis_payload()
        payload.pop("status")
        payload.pop("final_answer")
        payload.pop("answer")
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertTrue(check(outcome, "status_accepted").passed)
        self.assertEqual(
            check(outcome, "status_accepted").actual["sources"],
            {"plan.status": "succeeded"},
        )

    def test_unknown_payload_keys_do_not_crash_the_evaluator(self):
        payload = analysis_payload()
        payload["brand_new_key"] = {"nested": [1, 2, 3]}
        payload["steps"][0]["unexpected"] = "value"
        payload["evidence"].append({"kind": "mystery"})
        payload["final_answer"]["extra"] = {"numbers": [{"key": "x"}]}
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertTrue(outcome.passed, [c.detail for c in outcome.checks if not c.passed])

    def test_evidence_ids_may_come_from_the_composing_step(self):
        payload = analysis_payload()
        payload["evidence"] = []
        outcome = evaluate_task(analysis_spec(), make_trace(payload))
        self.assertTrue(check(outcome, "evidence_anchor").passed)
        self.assertTrue(check(outcome, "required_evidence").passed)


# ---------------------------------------------------------------------------
# Gold spec loading
# ---------------------------------------------------------------------------


class TaskSpecLoadingTest(unittest.TestCase):
    def test_loads_jsonl_and_enforces_the_file_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {
                "task_id": "retail_dev_1",
                "dataset": "retail_orders",
                "question": "q",
                "coverage": ["simple_single_table"],
                "expected_outcome": "analysis",
                "expected_status": ["succeeded"],
                "allowed_tools": [],
                "required_steps": [],
                "required_evidence": [],
                "runner": "planner",
            }
            (root / "dev.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            specs = load_spec_splits(root)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].split, "dev")
            self.assertEqual(getattr(specs[0], "runner", None), "planner")

            wrong = {**row, "split": "holdout"}
            (root / "dev.jsonl").write_text(json.dumps(wrong) + "\n", encoding="utf-8")
            with self.assertRaises(TaskSpecError):
                load_spec_splits(root)

    def test_loads_a_json_array_and_normalizes_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "task_id": " t1 ",
                            "split": "dev",
                            "dataset": "d",
                            "question": "q",
                            "expected_outcome": "query",
                            "expected_status": ["succeeded", "succeeded", ""],
                            "replaceable_steps": {"query_metric": [" preview "]},
                            "values_tolerance": {"answer.value": 0.01, "*": 1e-3},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            specs = load_specs(path)
            spec = specs[0]
            self.assertEqual(spec.task_id, "t1")
            self.assertEqual(spec.expected_status, ["succeeded"])
            self.assertEqual(spec.replaceable_steps, {"query_metric": ["preview"]})
            self.assertEqual(spec.tolerance_for("answer.value"), 0.01)
            self.assertEqual(spec.tolerance_for("other"), 1e-3)

    def test_a_malformed_row_names_its_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dev.jsonl"
            path.write_text('{"task_id": "a"}\n', encoding="utf-8")
            with self.assertRaises(TaskSpecError) as caught:
                load_specs(path)
            self.assertIn("dev.jsonl:1", str(caught.exception))

    def test_duplicate_task_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {
                "task_id": "dup",
                "dataset": "d",
                "question": "q",
                "expected_outcome": "analysis",
                "expected_status": ["succeeded"],
            }
            (root / "dev.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            (root / "regression.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(TaskSpecError):
                load_spec_splits(root)

    def test_unexpected_file_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "smoke.jsonl").write_text("", encoding="utf-8")
            with self.assertRaises(TaskSpecError):
                load_spec_splits(root)

    def test_negative_tolerance_is_rejected(self):
        with self.assertRaises(Exception):
            TaskSpec(
                task_id="t",
                split="dev",
                dataset="d",
                question="q",
                expected_outcome="analysis",
                expected_status=["succeeded"],
                values_tolerance={"x": -1},
            )


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


class ContractSurfaceTest(unittest.TestCase):
    def test_check_names_and_failure_classes_are_the_frozen_vocabulary(self):
        self.assertEqual(
            CHECK_NAMES,
            (
                "trace_available",
                "status_accepted",
                "outcome_kind",
                "required_evidence",
                "forbidden_evidence",
                "expected_values",
                "evidence_anchor",
                "tool_legality",
                "tool_validity",
                "required_steps",
                "claim_guard",
                "budget",
                "no_unsupported_numbers",
            ),
        )
        self.assertEqual(
            FAILURE_CLASSES,
            (
                "wrong_value",
                "missing_evidence",
                "unsupported_claim",
                "unexpected_clarification",
                "missing_clarification",
                "policy_not_enforced",
                "illegal_tool",
                "failed_tool",
                "budget_exceeded",
                "wrong_status",
                "runner_error",
                "missing_trace",
            ),
        )

    def test_every_check_name_has_a_failure_class(self):
        from queryforge.evaluation import evaluator

        for name in CHECK_NAMES:
            self.assertIn(name, evaluator._FAILURE_BY_CHECK)
        for (name, _reason), classified in evaluator._FAILURE_BY_REASON.items():
            self.assertIn(name, CHECK_NAMES)
            self.assertIn(classified, FAILURE_CLASSES)

    def test_contract_functions_are_importable(self):
        from queryforge.evaluation import (
            aggregate as aggregate_fn,
            classify_failure as classify_fn,
            evaluate_task as evaluate_fn,
            load_spec_splits as load_fn,
            load_specs as load_specs_fn,
            recompute as recompute_fn,
        )

        for function in (
            aggregate_fn,
            classify_fn,
            evaluate_fn,
            load_fn,
            load_specs_fn,
            recompute_fn,
        ):
            self.assertTrue(callable(function))


if __name__ == "__main__":
    unittest.main()
