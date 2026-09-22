"""Isolated evaluator package for the step-16 agent benchmark.

The package scores recorded agent traces against gold tasks and aggregates them
into a recomputable report.  It is deliberately isolated from the pipeline it
grades: it imports nothing but the standard library and pydantic, never
``queryforge.workflow``/``application``/``orchestration``/``interfaces``, so a
bug in the runtime cannot make the benchmark lenient (see
``tests/test_agent_task_gold.py::test_evaluator_does_not_import_runtime_being_graded``).

Typical use::

    from queryforge.evaluation import load_spec_splits, TaskTrace, evaluate_task, aggregate

    specs = load_spec_splits("evaluation/tasks")
    outcomes = [evaluate_task(spec, TaskTrace.model_validate(record)) for spec, record in pairs]
    report = aggregate(outcomes, thresholds=load_thresholds().tier1_offline.model_dump())

``recompute(report, specs)`` rebuilds that report from ``report["results"]``
alone, which is what makes a published score auditable.
"""

from queryforge.evaluation.evaluator import (
    CHECK_NAMES,
    FAILURE_CLASSES,
    AnswerView,
    CheckResult,
    Claim,
    TaskOutcome,
    aggregate,
    answer_view,
    classify_failure,
    estimate_cost,
    evaluate_task,
    evidence_entries,
    evidence_id_of,
    evidence_kind_of,
    evidence_kind_set,
    evidence_payloads,
    failure_tolerance,
    final_answer_of,
    is_clarification,
    legacy_answer_of,
    normalize_status,
    policy_denial,
    recompute,
    resolve_number,
    status_candidates,
    step_records,
    stop_reason,
)
from queryforge.evaluation.tasks import (
    DEFAULT_VALUES_TOLERANCE,
    KNOWN_SPLITS,
    TaskSpec,
    TaskSpecError,
    load_spec_splits,
    load_specs,
    spec_index,
)
from queryforge.evaluation.thresholds import (
    DEFAULT_THRESHOLDS_PATH,
    METRIC_PATHS,
    TIER_NAMES,
    Thresholds,
    TierConfig,
    check_metrics,
    load_thresholds,
)
from queryforge.evaluation.trace import (
    TaskTrace,
    as_mapping,
    payload_path,
    tool_action,
    tool_error_category,
    tool_name,
    tool_ok,
)

__all__ = [
    "CHECK_NAMES",
    "DEFAULT_THRESHOLDS_PATH",
    "DEFAULT_VALUES_TOLERANCE",
    "FAILURE_CLASSES",
    "KNOWN_SPLITS",
    "METRIC_PATHS",
    "TIER_NAMES",
    "AnswerView",
    "CheckResult",
    "Claim",
    "TaskOutcome",
    "TaskSpec",
    "TaskSpecError",
    "TaskTrace",
    "Thresholds",
    "TierConfig",
    "aggregate",
    "answer_view",
    "as_mapping",
    "check_metrics",
    "classify_failure",
    "estimate_cost",
    "evaluate_task",
    "evidence_entries",
    "evidence_id_of",
    "evidence_kind_of",
    "evidence_kind_set",
    "evidence_payloads",
    "failure_tolerance",
    "final_answer_of",
    "is_clarification",
    "legacy_answer_of",
    "load_spec_splits",
    "load_specs",
    "load_thresholds",
    "normalize_status",
    "payload_path",
    "policy_denial",
    "recompute",
    "resolve_number",
    "spec_index",
    "status_candidates",
    "step_records",
    "stop_reason",
    "tool_action",
    "tool_error_category",
    "tool_name",
    "tool_ok",
]
