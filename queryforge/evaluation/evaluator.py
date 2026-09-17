"""Isolated, recomputable scoring of one agent trace against one gold task.

Step 16 of the optimization plan.  Three properties drive every decision here:

**Goal oriented, never path oriented.**  A task is scored on the observable
outcome -- the status the pipeline reported, the evidence it produced, the
values it published, the claims it made -- and *never* on SQL text or on the
order in which steps ran.  Two runs that reach the same goal by different routes
both pass (16-T2).

**Nothing self-assessed is trusted.**  ``validation_problems``,
``answer_validation.review_required``, a payload's own ``passed`` flag and any
``success`` marker are ignored: number traceability, evidence anchoring, tool
legality and the task verdict are re-derived here from the raw payload and the
recorded tool calls (16-T1).

**Recomputable.**  :func:`aggregate` embeds the raw trace of every result, so
:func:`recompute` can rebuild the whole report from ``report["results"]`` alone
-- the step's exit gate ("从原始 case 与 trace 可重算报告") is executable, not
aspirational.

This module imports nothing but the standard library and pydantic: the evaluator
must not be able to share a bug with the code it grades (see
``tests/test_evaluation_isolation.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from queryforge.evaluation.tasks import TaskSpec, spec_index
from queryforge.evaluation.thresholds import check_metrics
from queryforge.evaluation.trace import (
    TaskTrace,
    as_bool,
    as_int,
    as_mapping,
    as_number,
    as_sequence,
    as_text,
    iter_mappings,
    payload_path,
    tool_action,
    tool_error,
    tool_error_category,
    tool_name,
    tool_ok,
)

#: Fixed failure vocabulary (contract 3.5).  Every failing outcome is classified
#: with exactly one of these names, which is what makes a failure histogram
#: comparable across runs.
FAILURE_CLASSES: tuple[str, ...] = (
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
)

#: Fixed check names (contract 3.3), in reporting order.  ``claim_guard`` and
#: ``budget`` are only emitted when the gold declares the corresponding
#: constraint, so a rate computed over a check never divides by tasks the check
#: did not apply to.
CHECK_NAMES: tuple[str, ...] = (
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
)

#: Container keys searched when a finding cites a number by a column name, e.g.
#: ``total_revenue`` living under ``aggregates``.
_NUMBER_CONTAINERS: tuple[str, ...] = (
    "numbers",
    "values",
    "metrics",
    "aggregates",
    "totals",
    "summary",
    "result",
    "measurements",
    "stats",
    "groups",
)

#: Statuses that mean "reached at all" for the presence of a required step.  A
#: step that ran and failed, or that was skipped by an upstream failure, still
#: appeared in the trace; what it was supposed to *produce* is graded by
#: ``required_evidence``/``expected_values`` instead.
_REACHED_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "success", "failed", "blocked", "skipped"}
)

#: Statuses of a step that did not produce its result.
_FAILED_STEP_STATUSES: frozenset[str] = frozenset({"failed", "blocked"})

#: Status words the evaluator treats as interchangeable with the plan vocabulary.
_STATUS_ALIASES: dict[str, str] = {"success": "succeeded", "succeed": "succeeded"}

#: Statuses that mean "the gold already accepts a degraded run".  When a task
#: declares one of these, a failed tool call is part of the accepted contract
#: (empty results, data faults, policy probes) rather than a defect.
_DEGRADED_STATUSES: frozenset[str] = frozenset(
    {"failed", "partial", "blocked", "needs_clarification"}
)

#: Coverage categories whose whole point is that something cannot be answered,
#: so a failing step is expected (contract 3.4, extended to empty results by the
#: step-16 coverage matrix).
_TOLERANT_COVERAGE: frozenset[str] = frozenset({"data_fault", "empty_result"})

#: Error categories that denote a governance denial rather than a defect.
_POLICY_CATEGORIES: frozenset[str] = frozenset(
    {
        "permission",
        "policy",
        "security",
        "policy_rejection",
        "policy_denied",
        "denied",
        "forbidden",
        "authorization",
        "access_denied",
    }
)

#: Words that mark a policy denial in an error message (last-resort signal).
_POLICY_MARKERS: tuple[str, ...] = (
    "policy",
    "not permitted",
    "not allowed",
    "denied",
    "forbidden",
    "permission",
    "read-only",
    "readonly",
)

#: Failure-class priority: the first failing check in this order names the class,
#: so the most specific, most actionable cause wins (an unexpected clarification
#: is a clarification failure even though the status also mismatched).
_FAILURE_PRIORITY: tuple[str, ...] = (
    "trace_available",
    "tool_legality",
    "tool_validity",
    "budget",
    "outcome_kind",
    "status_accepted",
    "expected_values",
    "forbidden_evidence",
    "evidence_anchor",
    "required_evidence",
    "no_unsupported_numbers",
    "claim_guard",
    "required_steps",
)

#: Fallback class per check name (used when a check carries no explicit reason).
_FAILURE_BY_CHECK: dict[str, str] = {
    "trace_available": "missing_trace",
    "status_accepted": "wrong_status",
    "outcome_kind": "wrong_status",
    "required_evidence": "missing_evidence",
    "forbidden_evidence": "policy_not_enforced",
    "expected_values": "wrong_value",
    "evidence_anchor": "unsupported_claim",
    "tool_legality": "illegal_tool",
    "tool_validity": "failed_tool",
    "required_steps": "missing_evidence",
    "claim_guard": "unsupported_claim",
    "budget": "budget_exceeded",
    "no_unsupported_numbers": "unsupported_claim",
}

#: Sub-reason overrides, so one check can report two genuinely different causes
#: (an unexpected clarification and a missing one are not the same defect).
_FAILURE_BY_REASON: dict[tuple[str, str], str] = {
    ("trace_available", "runner_error"): "runner_error",
    ("trace_available", "missing_trace"): "missing_trace",
    ("outcome_kind", "unexpected_clarification"): "unexpected_clarification",
    ("outcome_kind", "missing_clarification"): "missing_clarification",
    ("outcome_kind", "policy_not_enforced"): "policy_not_enforced",
    ("evidence_anchor", "assertions_without_evidence"): "unsupported_claim",
    ("evidence_anchor", "dangling_evidence_ids"): "missing_evidence",
    ("evidence_anchor", "unanchored_claim"): "unsupported_claim",
    ("claim_guard", "forbidden_claim"): "unsupported_claim",
    ("claim_guard", "missing_required_claim"): "missing_evidence",
}

#: How many names/values a detail string lists before summarizing the rest.
_DETAIL_LIMIT = 5


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class CheckResult(BaseModel):
    """One fixed-name check with the evidence for its verdict (contract 3.1)."""

    model_config = ConfigDict(extra="allow")

    name: str
    passed: bool
    detail: str
    expected: Any = None
    actual: Any = None


class TaskOutcome(BaseModel):
    """The scored outcome of one task (contract 3.1 plus what 3.6 aggregates).

    The extra fields are metadata the aggregate needs (per split/dataset/coverage
    breakdowns, clarification rates, measured vs estimated usage) and the raw
    trace that makes the report recomputable.  They are all *observed* facts, so
    none of them is a self-assessment the evaluator trusts.
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    split: str
    dataset: str
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    failure_class: str | None = None
    tool_calls: int = 0
    usage_tokens: int | None = None
    cost_usd: float | None = None
    wall_ms: float = 0.0

    coverage: list[str] = Field(default_factory=list)
    expected_outcome: str | None = None
    expected_status: list[str] = Field(default_factory=list)
    requires_evidence: bool = False
    multi_step: bool = False
    clarified: bool = False
    expects_clarification: bool = False
    measured_tokens: int | None = None
    estimated_tokens: int | None = None
    cost_basis: str | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    tool_calls_observed: int = 0
    usage_source: str | None = None
    trace: dict[str, Any] = Field(default_factory=dict)
    schema_precision: float | None = None
    schema_recall: float | None = None

    def check(self, name: str) -> CheckResult | None:
        """The named check, or ``None`` when it did not apply to this task."""

        return next((item for item in self.checks if item.name == name), None)


# ---------------------------------------------------------------------------
# Payload shape adapters (both pipeline layers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One claim of the answer that may have to be anchored to evidence.

    ``structured`` marks a claim the pipeline builds from evidence (a finding or
    a structured conclusion); prose conclusions are derived text and are only
    used to detect an answer that asserts something without citing anything.
    """

    label: str
    structured: bool
    evidence_ids: tuple[str, ...] = ()
    numbers: Mapping[str, Any] = field(default_factory=dict)
    degraded: bool = False
    text: str = ""


@dataclass(frozen=True)
class AnswerView:
    """How the answer of a payload is anchored (step-12 layer or legacy)."""

    shape: str
    claims: tuple[Claim, ...]
    answer_ids: tuple[str, ...]
    per_claim_required: bool


def _evidence_entries_from(value: Any) -> list[dict[str, Any]]:
    """Evidence records inside one ``evidence`` value (list or wrapper object)."""

    if isinstance(value, Mapping):
        wrapper = as_mapping(value)
        for key in ("evidence", "items"):
            if key in wrapper:
                return [as_mapping(item) for item in as_sequence(wrapper[key])]
        return []
    return [as_mapping(item) for item in as_sequence(value)]


def evidence_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Evidence records of either payload shape.

    The planner lifts the evidence store to ``payload["evidence"]``; a payload
    recorded before that lift still carries it on the composing step's outputs,
    so both are accepted (the first non-empty source wins).
    """

    entries = [item for item in _evidence_entries_from(payload.get("evidence")) if item]
    if entries:
        return entries
    for step in iter_mappings(as_sequence(payload.get("steps"))):
        outputs = as_mapping(step.get("outputs"))
        nested = _evidence_entries_from(outputs.get("evidence"))
        entries.extend(item for item in nested if item)
    return entries


def evidence_id_of(entry: Mapping[str, Any]) -> str:
    """Evidence id of one record (step-12 ``id`` or planner ``evidence_id``)."""

    for key in ("evidence_id", "id"):
        text = as_text(entry.get(key))
        if text:
            return text
    return ""


def evidence_kind_of(entry: Mapping[str, Any]) -> str:
    """Evidence kind of one record."""

    for key in ("kind", "evidence_kind"):
        text = as_text(entry.get(key))
        if text:
            return text
    return ""


def normalize_name(value: Any) -> str:
    """Normalize a tool/kind/step name for comparison."""

    return as_text(value).casefold()


def evidence_kind_set(entries: Iterable[Mapping[str, Any]]) -> set[str]:
    """Normalized kinds produced by a run."""

    return {
        normalize_name(evidence_kind_of(entry))
        for entry in entries
        if evidence_kind_of(entry)
    }


def evidence_payloads(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map every evidence id to its payload (the numbers a claim may cite)."""

    payloads: dict[str, dict[str, Any]] = {}
    for entry in entries:
        identifier = evidence_id_of(entry)
        if identifier:
            payloads[identifier] = as_mapping(entry.get("payload"))
    return payloads


def final_answer_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The step-12 ``final_answer`` mapping, or ``{}``."""

    candidate = payload.get("final_answer")
    return as_mapping(candidate) if isinstance(candidate, Mapping) else {}


def legacy_answer_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The legacy ``answer`` mapping, or ``{}``."""

    candidate = payload.get("answer")
    return as_mapping(candidate) if isinstance(candidate, Mapping) else {}


def _ids_of(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Evidence ids of one record: a list, or a single ``evidence_id``."""

    ids: list[str] = []
    for item in as_sequence(record.get("evidence_ids")):
        text = as_text(item)
        if text and text not in ids:
            ids.append(text)
    single = as_text(record.get("evidence_id"))
    if single and single not in ids:
        ids.append(single)
    return tuple(ids)


def _numbers_of(record: Mapping[str, Any]) -> dict[str, Any]:
    """Number claims of one record (mapping form or list-of-claims form)."""

    raw = record.get("numbers")
    if isinstance(raw, Mapping):
        return as_mapping(raw)
    numbers: dict[str, Any] = {}
    for item in as_sequence(raw):
        entry = as_mapping(item)
        key = as_text(entry.get("key") or entry.get("name") or entry.get("label"))
        if key:
            numbers[key] = entry.get("value")
    return numbers


def nested_evidence_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Ids cited by a mapping's own fields *and* by its nested numbers."""

    ids = list(_ids_of(record))
    raw = record.get("numbers")
    for item in as_sequence(raw):
        for identifier in _ids_of(as_mapping(item)):
            if identifier not in ids:
                ids.append(identifier)
    return tuple(ids)


def _claim_from_mapping(label: str, record: Mapping[str, Any]) -> Claim:
    return Claim(
        label=label,
        structured=True,
        evidence_ids=nested_evidence_ids(record),
        numbers=_numbers_of(record),
        degraded=bool(as_bool(record.get("degraded"))),
        text=as_text(
            record.get("statement") or record.get("conclusion") or record.get("text")
        ),
    )


def answer_view(payload: Mapping[str, Any]) -> AnswerView:
    """Describe how a payload's answer is anchored.

    The step-12 layer is preferred when present (its findings carry per-claim
    evidence ids); the legacy answer only has answer-level ids, so its findings
    are not required to cite individually.  A payload with neither shape still
    counts as an answer when it carries prose in ``answer``/``conclusions``.
    """

    final = final_answer_of(payload)
    if final:
        claims: list[Claim] = []
        for index, item in enumerate(as_sequence(final.get("findings"))):
            record = as_mapping(item)
            if record:
                claims.append(_claim_from_mapping(f"findings[{index}]", record))
        for index, item in enumerate(as_sequence(final.get("conclusions"))):
            if isinstance(item, Mapping):
                claims.append(_claim_from_mapping(f"conclusions[{index}]", as_mapping(item)))
            else:
                text = as_text(item)
                if text:
                    claims.append(
                        Claim(label=f"conclusions[{index}]", structured=False, text=text)
                    )
        return AnswerView(
            shape="final_answer",
            claims=tuple(claims),
            answer_ids=_ids_of(final),
            per_claim_required=True,
        )
    legacy = legacy_answer_of(payload)
    if legacy:
        return AnswerView(
            shape="answer",
            claims=tuple(_claim_from_mapping(f"findings[{i}]", as_mapping(item))
                         for i, item in enumerate(as_sequence(legacy.get("findings")))),
            answer_ids=_ids_of(legacy),
            per_claim_required=False,
        )
    return AnswerView(shape="none", claims=(), answer_ids=(), per_claim_required=False)


def answer_texts(view: AnswerView) -> list[str]:
    """Every conclusion/statement text of the answer (claim-guard corpus)."""

    texts = [claim.text for claim in view.claims if claim.text]
    if not texts and view.answer_ids:
        return []
    return texts


def step_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Reported plan steps of either shape (top level, else ``plan.steps``)."""

    steps = [as_mapping(item) for item in as_sequence(payload.get("steps")) if item]
    if steps:
        return [item for item in steps if item]
    plan = as_mapping(payload.get("plan"))
    return [as_mapping(item) for item in as_sequence(plan.get("steps")) if item]


def status_candidates(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Observed statuses in priority order (payload, then plan, then answer).

    Several sources may report a status; the first one that exists is the one
    the gold is compared against, and the rest are kept for the report so a
    mismatch can be diagnosed without re-reading the trace.
    """

    candidates: list[tuple[str, str]] = []
    for source, value in (
        ("payload.status", payload.get("status")),
        ("plan.status", as_mapping(payload.get("plan")).get("status")),
        ("final_answer.status", final_answer_of(payload).get("status")),
    ):
        text = as_text(value)
        if text:
            candidates.append((source, text))
    return candidates


def stop_reason(payload: Mapping[str, Any]) -> str | None:
    """The run's stop reason, when it reported one."""

    for key in ("stop_reason", "terminal_outcome"):
        text = as_text(payload.get(key))
        if text:
            return text
    return None


def is_clarification(payload: Mapping[str, Any]) -> bool:
    """Whether the run asked the user for clarification instead of answering."""

    if normalize_status(as_text(payload.get("status"))) == "needs_clarification":
        return True
    plan_status = as_mapping(payload.get("plan")).get("status")
    if normalize_status(as_text(plan_status)) == "needs_clarification":
        return True
    answer_status = final_answer_of(payload).get("status")
    if normalize_status(as_text(answer_status)) == "needs_clarification":
        return True
    questions = as_sequence(payload.get("unresolved_questions"))
    if any(as_text(item) for item in questions):
        return True
    request = as_mapping(payload.get("analysis_request"))
    return any(as_text(item) for item in as_sequence(request.get("unresolved_questions")))


def normalize_status(value: str) -> str:
    """Canonical status word (``success`` and ``succeeded`` are the same thing)."""

    text = as_text(value).casefold()
    return _STATUS_ALIASES.get(text, text)


def policy_denial(
    payload: Mapping[str, Any], tool_calls: Sequence[Any]
) -> tuple[bool, list[str]]:
    """Whether a governance rule -- not a defect -- stopped the run.

    Signals are collected from every shape that can carry a denial: the probe
    payload, step error categories, recorded tool calls, the SQL policy decisions
    and the stop reason.  A denial is never inferred from the status alone,
    because ``blocked`` also describes an unrelated blocked step.
    """

    signals: list[str] = []
    if as_bool(payload.get("policy_rejected")) is True:
        signals.append("payload.policy_rejected")
    for key in ("policy_rule", "policy_violation", "policy_name"):
        if as_text(payload.get(key)):
            signals.append(f"payload.{key}")
    reason = normalize_name(payload.get("stop_reason"))
    if "policy" in reason or reason in {"denied", "denied_by_policy"}:
        signals.append("payload.stop_reason")
    if normalize_name(payload.get("status")) in {"blocked", "failed"}:
        for step in step_records(payload):
            if normalize_name(step.get("error_category")) in _POLICY_CATEGORIES:
                signals.append(f"steps[{as_text(step.get('step_id'))}].error_category")
        for call in tool_calls:
            if normalize_name(tool_error_category(call)) in _POLICY_CATEGORIES:
                signals.append(f"tool_calls[{tool_name(call)}].error_category")
        security = as_mapping(payload.get("sql_security"))
        for index, decision in enumerate(as_sequence(security.get("decisions"))):
            record = as_mapping(decision)
            if record and as_bool(record.get("allowed")) is False:
                signals.append(f"sql_security.decisions[{index}].allowed")
        if not signals:
            text = " ".join(
                [as_text(payload.get("error")), as_text(payload.get("stop_reason"))]
                + [tool_error(call) for call in tool_calls]
                + [as_text(step.get("error")) for step in step_records(payload)]
            ).casefold()
            if any(marker in text for marker in _POLICY_MARKERS):
                signals.append("error_text")
    return bool(signals), signals


# ---------------------------------------------------------------------------
# Number traceability
# ---------------------------------------------------------------------------


def is_number(value: Any) -> bool:
    """Whether ``value`` is a real number (``bool`` is not a number here)."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def numbers_equal(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    """Absolute-tolerance number comparison used for traceability."""

    if not is_number(left) or not is_number(right):
        return False
    return abs(float(left) - float(right)) <= tolerance


def _recursive_matches(payload: Any, key: str, depth: int = 3) -> list[Any]:
    """Collect up to two values stored under ``key`` (bounded, deterministic)."""

    matches: list[Any] = []
    stack: list[tuple[Any, int]] = [(payload, depth)]
    while stack and len(matches) < 2:
        node, remaining = stack.pop()
        if not isinstance(node, Mapping):
            continue
        for name, value in node.items():
            if name == key:
                matches.append(value)
                if len(matches) >= 2:
                    break
            elif remaining > 0 and isinstance(value, Mapping):
                stack.append((value, remaining - 1))
    return matches


def resolve_number(payloads: Iterable[Any], key: str) -> tuple[bool, Any]:
    """Look one finding number up inside the payloads of the cited evidence.

    Accepted shapes, in order: an exact dotted path (``revenue.sum``), a flat key
    (``total_revenue``), a flat key inside a known container
    (``aggregates.revenue``), the container/column swap used by the aggregate
    payload (``sum.revenue`` for ``revenue.sum``) and finally a unique recursive
    key match of bounded depth.

    An *ambiguous* match counts as not found rather than guessed -- the
    evaluator's job is to prove a number has a source, so "probably this one" is
    not a proof.  This lookup is deliberately re-implemented here instead of
    imported from the pipeline: the benchmark must not inherit leniency from the
    code it grades.
    """

    if not key:
        return False, None
    parts = [part for part in str(key).split(".") if part]
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        found, value = payload_path(payload, key)
        if found:
            return True, value
        if len(parts) == 2:
            head, tail = parts
            for container in _NUMBER_CONTAINERS:
                group = payload.get(container)
                if not isinstance(group, Mapping):
                    continue
                head_value = group.get(head)
                if isinstance(head_value, Mapping) and tail in head_value:
                    return True, head_value[tail]
                tail_value = group.get(tail)
                if isinstance(tail_value, Mapping) and head in tail_value:
                    return True, tail_value[head]
        for container in _NUMBER_CONTAINERS:
            group = payload.get(container)
            if isinstance(group, Mapping) and key in group:
                return True, group[key]
        matches = _recursive_matches(payload, key)
        if len(matches) == 1:
            return True, matches[0]
    return False, None


# ---------------------------------------------------------------------------
# Value comparison
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Deterministic, short rendering of one compared value."""

    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (str, int, bool)) or value is None:
        return repr(value)
    return str(value)


def _values_match(actual: Any, expected: Any, tolerance: float) -> bool:
    """Compare one gold value with the observed one.

    Numbers use the task's *relative* tolerance (with an exact match required for
    a gold zero, where a relative tolerance is undefined), strings and booleans
    compare exactly, and lists compare as multisets (order is not an outcome).
    """

    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if is_number(expected):
        if not is_number(actual):
            return False
        try:
            return math.isclose(
                float(actual), float(expected), rel_tol=tolerance, abs_tol=0.0
            )
        except (OverflowError, ValueError):
            return False
    if isinstance(expected, list):
        if not isinstance(actual, (list, tuple)):
            return False
        if len(actual) != len(expected):
            return False
        remaining = [item for item in actual]
        for wanted in expected:
            index = next(
                (
                    position
                    for position, candidate in enumerate(remaining)
                    if _values_match(candidate, wanted, tolerance)
                ),
                None,
            )
            if index is None:
                return False
            remaining.pop(index)
        return True
    if isinstance(expected, dict):
        if not isinstance(actual, Mapping):
            return False
        return all(
            key in actual and _values_match(actual[key], value, tolerance)
            for key, value in expected.items()
        )
    return actual == expected


def _preview(values: Iterable[Any], limit: int = _DETAIL_LIMIT) -> str:
    """Render a bounded, deterministic list for a check detail."""

    items = [str(item) for item in values]
    if not items:
        return "none"
    if len(items) <= limit:
        return ", ".join(items)
    return f"{', '.join(items[:limit])} (+{len(items) - limit} more)"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _result(
    name: str,
    passed: bool,
    detail: str,
    *,
    expected: Any = None,
    actual: Any = None,
    reason: str | None = None,
) -> CheckResult:
    """Build one check result; ``reason`` is the machine-readable sub-cause."""

    if actual is None:
        record: Any = None
    elif isinstance(actual, Mapping):
        record = dict(actual)
    else:
        record = {"observed": actual}
    if reason is not None:
        record = dict(record or {})
        record["reason"] = reason
    return CheckResult(
        name=name, passed=passed, detail=detail, expected=expected, actual=record
    )


def _check_trace_available(trace: TaskTrace) -> CheckResult:
    """The runner must have produced a payload, and must not have reported an error."""

    if trace.error:
        return _result(
            "trace_available",
            False,
            f"the runner reported an error instead of a result: {trace.error}",
            actual={"error": trace.error},
            reason="runner_error",
        )
    if not trace.payload:
        return _result(
            "trace_available",
            False,
            "the trace carries no payload, so nothing could be scored",
            reason="missing_trace",
        )
    return _result(
        "trace_available", True, f"payload recorded ({len(trace.payload)} top-level keys)"
    )


def _check_status(
    spec: TaskSpec,
    statuses: Sequence[tuple[str, str]],
    stop: str | None,
) -> CheckResult:
    """``status_accepted``: status set plus acceptable stop reasons (3.3 #1)."""

    sources = {source: value for source, value in statuses}
    if not spec.expected_status:
        # A gold that constrains no status cannot fail this check; the trace
        # having no status at all is already reported by `trace_available`.
        return _result(
            "status_accepted",
            True,
            "the gold constrains no status",
            actual={"status": statuses[0][1] if statuses else None, "sources": sources},
        )
    accepted = {normalize_status(item) for item in spec.expected_status}
    if not statuses:
        return _result(
            "status_accepted",
            False,
            "the payload reports no status while the gold expects "
            + _preview(spec.expected_status),
            expected=spec.expected_status,
            reason="status_missing",
        )
    primary = normalize_status(statuses[0][1])
    if primary not in accepted:
        return _result(
            "status_accepted",
            False,
            f"status {statuses[0][1]!r} is not an accepted status "
            f"({_preview(spec.expected_status)})",
            expected=spec.expected_status,
            actual={"status": statuses[0][1], "sources": sources},
        )
    if spec.acceptable_stop_reasons:
        allowed = {normalize_status(item) for item in spec.acceptable_stop_reasons}
        # "No stop reason" is a legitimate success outcome, so the gold may spell
        # it "", "none" or "null"; all three mean the same thing here.
        silent = {"", "none", "null"}
        hit = (stop is not None and normalize_status(stop) in allowed) or (
            stop is None and bool(allowed & silent)
        )
        if not hit:
            return _result(
                "status_accepted",
                False,
                f"stop_reason {stop!r} is not acceptable "
                f"({_preview(spec.acceptable_stop_reasons)})",
                expected=spec.acceptable_stop_reasons,
                actual={"status": statuses[0][1], "stop_reason": stop},
                reason="stop_reason_not_accepted",
            )
    return _result(
        "status_accepted",
        True,
        f"status {statuses[0][1]!r} accepted with stop_reason {stop!r}",
        actual={"status": statuses[0][1], "stop_reason": stop, "sources": sources},
    )


def _check_outcome_kind(
    spec: TaskSpec,
    clarified: bool,
    denied: bool,
    denial_signals: Sequence[str],
) -> CheckResult:
    """``outcome_kind``: clarification / policy rejection / answer (3.3 #2).

    Both directions of clarification are graded: asking when the gold expected an
    answer, and answering when the gold expected a question.
    """

    actual = {
        "expected_outcome": spec.expected_outcome,
        "clarified": clarified,
        "policy_denied": denied,
        "policy_signals": list(denial_signals),
    }
    if spec.expected_outcome == "clarification":
        if clarified:
            return _result(
                "outcome_kind", True, "the run asked for clarification", actual=actual
            )
        return _result(
            "outcome_kind",
            False,
            "the gold expects a clarifying question but the run answered instead",
            expected="clarification",
            actual=actual,
            reason="missing_clarification",
        )
    if spec.expected_outcome == "policy_rejection":
        if denied:
            return _result(
                "outcome_kind",
                True,
                f"the run was rejected by policy ({_preview(denial_signals)})",
                actual=actual,
            )
        return _result(
            "outcome_kind",
            False,
            "the gold expects a policy rejection but nothing shows a governance "
            "denial (an enforced policy must be observable)",
            expected="policy_rejection",
            actual=actual,
            reason="policy_not_enforced",
        )
    if clarified:
        return _result(
            "outcome_kind",
            False,
            "the run asked for clarification while the gold expects an answered "
            f"task ({spec.expected_outcome})",
            expected=spec.expected_outcome,
            actual=actual,
            reason="unexpected_clarification",
        )
    return _result(
        "outcome_kind",
        True,
        f"the run answered the {spec.expected_outcome} task",
        actual=actual,
    )


def _check_required_evidence(
    spec: TaskSpec, kinds: set[str], entries: Sequence[Any]
) -> CheckResult:
    """``required_evidence``: every promised evidence kind was produced (3.3 #3)."""

    required = [normalize_name(item) for item in spec.required_evidence]
    missing = [item for item in required if item not in kinds]
    actual = {
        "required": list(spec.required_evidence),
        "evidence_kinds": sorted(kinds),
        "evidence_count": len(entries),
    }
    if missing:
        return _result(
            "required_evidence",
            False,
            f"missing required evidence kind(s): {_preview(missing)}",
            expected=list(spec.required_evidence),
            actual={**actual, "missing": missing},
        )
    if not required:
        return _result(
            "required_evidence", True, "the gold requires no evidence kind", actual=actual
        )
    return _result(
        "required_evidence",
        True,
        f"all required kinds produced ({_preview(required)})",
        actual=actual,
    )


def _check_forbidden_evidence(spec: TaskSpec, kinds: set[str]) -> CheckResult:
    """``forbidden_evidence``: no evidence kind the gold forbids (3.3 #4)."""

    forbidden = [normalize_name(item) for item in spec.forbidden_evidence]
    present = [item for item in forbidden if item in kinds]
    actual = {"forbidden": list(spec.forbidden_evidence), "evidence_kinds": sorted(kinds)}
    if present:
        return _result(
            "forbidden_evidence",
            False,
            f"forbidden evidence kind(s) produced: {_preview(present)}",
            expected=list(spec.forbidden_evidence),
            actual={**actual, "produced": present},
        )
    return _result(
        "forbidden_evidence", True, "no forbidden evidence kind produced", actual=actual
    )


def _check_expected_values(spec: TaskSpec, payload: Mapping[str, Any]) -> CheckResult:
    """``expected_values``: gold values by dotted path, with tolerance (3.3 #5)."""

    if not spec.expected_values:
        return _result(
            "expected_values", True, "the gold fixes no value", expected={}, actual={}
        )
    problems: list[str] = []
    observed: dict[str, Any] = {}
    missing: list[str] = []
    for key, expected in spec.expected_values.items():
        found, actual = payload_path(payload, key)
        if not found:
            # A single-segment path may name a value the payload nests one level
            # down (e.g. "value" for answer.value); the bounded lookup keeps that
            # from becoming "search everywhere".
            found, actual = resolve_number([payload], key)
        if not found:
            missing.append(key)
            problems.append(f"{key}: path not present in the payload")
            continue
        observed[key] = actual
        tolerance = spec.tolerance_for(key)
        if not _values_match(actual, expected, tolerance):
            problems.append(
                f"{key}: observed {_format_value(actual)}, expected "
                f"{_format_value(expected)} (relative tolerance {tolerance:g})"
            )
    detail = (
        f"all {len(spec.expected_values)} gold value(s) matched"
        if not problems
        else "; ".join(problems[:_DETAIL_LIMIT])
        + (
            f" (+{len(problems) - _DETAIL_LIMIT} more)"
            if len(problems) > _DETAIL_LIMIT
            else ""
        )
    )
    return _result(
        "expected_values",
        not problems,
        detail,
        expected=dict(spec.expected_values),
        actual={"observed": observed, "missing": missing},
    )


def _check_evidence_anchor(
    spec: TaskSpec, view: AnswerView, known_ids: set[str]
) -> CheckResult:
    """``evidence_anchor``: every claim is anchored to evidence that exists (3.3 #6).

    A beautiful answer that cites nothing fails (16-T1).  Two causes are told
    apart: assertions with no evidence id at all (``unsupported_claim``) and ids
    that do not exist in the payload (``missing_evidence``).
    """

    cited: list[str] = []
    for claim in view.claims:
        for identifier in claim.evidence_ids:
            if identifier not in cited:
                cited.append(identifier)
    for identifier in view.answer_ids:
        if identifier not in cited:
            cited.append(identifier)
    claims_summary = {
        "shape": view.shape,
        "claim_count": len(view.claims),
        "structured_claims": sum(1 for claim in view.claims if claim.structured),
        "cited": cited,
        "known": sorted(known_ids),
    }
    if not spec.answer_must_reference_evidence:
        return _result(
            "evidence_anchor",
            True,
            "the gold does not require evidence anchoring",
            actual=claims_summary,
        )
    if not view.claims and not view.answer_ids:
        # An empty answer asserts nothing, which is the honest outcome of an
        # empty result or a data fault (contract 3.4).
        return _result(
            "evidence_anchor",
            True,
            "the answer makes no claim, so there is nothing to anchor",
            actual=claims_summary,
        )
    if not cited:
        return _result(
            "evidence_anchor",
            False,
            "the answer states conclusions but cites no evidence id",
            expected="every conclusion anchored to evidence",
            actual=claims_summary,
            reason="assertions_without_evidence",
        )
    dangling = [identifier for identifier in cited if identifier not in known_ids]
    if dangling:
        return _result(
            "evidence_anchor",
            False,
            f"the answer cites evidence id(s) that the payload does not contain: "
            f"{_preview(dangling)}",
            expected=sorted(known_ids),
            actual={**claims_summary, "dangling": dangling},
            reason="dangling_evidence_ids",
        )
    if view.per_claim_required:
        unanchored = [
            claim.label
            for claim in view.claims
            if claim.structured and not claim.evidence_ids and not claim.degraded
        ]
        if unanchored:
            return _result(
                "evidence_anchor",
                False,
                f"claim(s) cite no evidence: {_preview(unanchored)}",
                expected="every finding anchored to evidence",
                actual={**claims_summary, "unanchored": unanchored},
                reason="unanchored_claim",
            )
    return _result(
        "evidence_anchor",
        True,
        f"every claim is anchored ({len(cited)} evidence id(s))",
        actual=claims_summary,
    )


def _allowed_tool_names(spec: TaskSpec) -> set[str]:
    """The normalized allow list of the gold (empty means "no restriction")."""

    return {normalize_name(item) for item in spec.allowed_tools}


def _check_tool_legality(spec: TaskSpec, trace: TaskTrace) -> CheckResult:
    """``tool_legality``: no recorded call outside the allowed tools (3.3 #7).

    A recorded call may carry the governed tool name *and* the planner action; a
    call is legal when either name is on the allow list, because the gold may
    name the tool or the action and the runner records both.
    """

    names = [
        (tool_name(call), tool_action(call))
        for call in trace.tool_calls
        if tool_name(call) or tool_action(call)
    ]
    allowed = _allowed_tool_names(spec)
    observed = [
        name for name, action in names if name
    ] or [action for _, action in names if action]
    if not allowed:
        return _result(
            "tool_legality",
            True,
            f"the gold restricts no tool ({len(names)} call(s) recorded)",
            actual={"observed": observed, "allowed": list(spec.allowed_tools)},
        )
    illegal = [
        (name, action)
        for name, action in names
        if normalize_name(name) not in allowed and normalize_name(action) not in allowed
    ]
    actual = {
        "observed": observed,
        "allowed": list(spec.allowed_tools),
        "call_count": len(names),
    }
    if illegal:
        rendered = [name or action for name, action in illegal]
        return _result(
            "tool_legality",
            False,
            f"illegal tool call(s): {_preview(rendered)}",
            expected=list(spec.allowed_tools),
            actual={**actual, "illegal": rendered},
        )
    return _result(
        "tool_legality",
        True,
        f"every recorded tool call is allowed ({len(names)} call(s))",
        actual=actual,
    )


def failure_tolerance(spec: TaskSpec) -> tuple[bool, list[str]]:
    """Whether the gold accepts a failed step for this task, and why.

    A gold that lists a degraded status, a data fault / empty result, or a policy
    rejection *is* saying that a failing step is part of the expected outcome, so
    counting that failure against tool validity would grade the contract, not the
    agent.
    """

    reasons: list[str] = []
    statuses = {normalize_status(item) for item in spec.expected_status}
    if statuses & _DEGRADED_STATUSES:
        reasons.append(
            "the gold accepts a degraded status ("
            + _preview(sorted(statuses & _DEGRADED_STATUSES))
            + ")"
        )
    coverage = {normalize_name(item) for item in spec.coverage}
    if coverage & _TOLERANT_COVERAGE:
        reasons.append(
            "the task covers " + _preview(sorted(coverage & _TOLERANT_COVERAGE))
        )
    if spec.expected_outcome == "policy_rejection":
        reasons.append("the gold expects a policy rejection")
    return bool(reasons), reasons


def _observed_failures(
    trace: TaskTrace, steps: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every failed tool call or failed step the trace shows."""

    failures: list[dict[str, Any]] = []
    for call in trace.tool_calls:
        if tool_ok(call):
            continue
        failures.append(
            {
                "source": "tool_call",
                "tool": tool_name(call) or tool_action(call) or "<unnamed>",
                "error_category": tool_error_category(call),
                "error": tool_error(call),
            }
        )
    for step in steps:
        if normalize_name(step.get("status")) not in _FAILED_STEP_STATUSES:
            continue
        failures.append(
            {
                "source": "step",
                "step_id": as_text(step.get("step_id")) or as_text(step.get("id")),
                "action": as_text(step.get("action")),
                "error_category": as_text(step.get("error_category")),
                "error": as_text(step.get("error")),
            }
        )
    return failures


def _check_tool_validity(
    spec: TaskSpec, trace: TaskTrace, steps: Sequence[Mapping[str, Any]]
) -> CheckResult:
    """``tool_validity``: no failed tool call, unless the gold allows it (3.3 #8)."""

    failures = _observed_failures(trace, steps)
    tolerant, reasons = failure_tolerance(spec)
    actual = {
        "failures": failures,
        "tolerance": reasons,
        "recorded_calls": len(trace.tool_calls),
        "reported_steps": len(steps),
    }
    if not failures:
        return _result("tool_validity", True, "no failed tool call", actual=actual)
    if tolerant:
        return _result(
            "tool_validity",
            True,
            f"{len(failures)} failed call(s)/step(s) tolerated because "
            f"{_preview(reasons)}",
            actual=actual,
        )
    rendered = [
        f"{item.get('source')}:{item.get('tool') or item.get('action') or item.get('step_id')}"
        f"({item.get('error_category') or 'unclassified'})"
        for item in failures
    ]
    return _result(
        "tool_validity",
        False,
        f"failed tool call(s) in a task the gold expects to succeed: "
        f"{_preview(rendered)}",
        expected="no failed tool call",
        actual=actual,
    )


def _check_required_steps(
    spec: TaskSpec, steps: Sequence[Mapping[str, Any]]
) -> CheckResult:
    """``required_steps`` with ``replaceable_steps`` substitutions (3.3 #9).

    Step *order* is never part of the verdict (16-T2): a required action counts
    as present when it appears anywhere in the trace, or when the gold declares an
    accepted equivalent that appeared instead.
    """

    performed = {
        normalize_name(step.get("action"))
        for step in steps
        if normalize_name(step.get("action"))
        and normalize_name(step.get("status")) not in {"pending", "running"}
    }
    missing: list[str] = []
    substitutions: dict[str, str] = {}
    for action in spec.required_steps:
        key = normalize_name(action)
        if key in performed:
            continue
        alternatives = spec.replaceable_steps.get(action) or []
        hit = next(
            (item for item in alternatives if normalize_name(item) in performed), None
        )
        if hit is not None:
            substitutions[action] = hit
        else:
            missing.append(action)
    actual = {
        "required": list(spec.required_steps),
        "performed": sorted(performed),
        "substitutions": substitutions,
        "steps": [
            {
                "step_id": as_text(step.get("step_id")) or as_text(step.get("id")),
                "action": as_text(step.get("action")),
                "status": as_text(step.get("status")),
            }
            for step in steps
        ],
    }
    if missing:
        return _result(
            "required_steps",
            False,
            f"required step(s) never ran and have no accepted substitute: "
            f"{_preview(missing)}",
            expected=list(spec.required_steps),
            actual={**actual, "missing": missing},
        )
    if substitutions:
        return _result(
            "required_steps",
            True,
            "required steps satisfied by accepted substitutes: "
            + _preview(f"{key}->{value}" for key, value in substitutions.items()),
            actual=actual,
        )
    return _result(
        "required_steps",
        True,
        f"every required step ran ({_preview(spec.required_steps)})",
        actual=actual,
    )


def _check_claim_guard(spec: TaskSpec, view: AnswerView) -> CheckResult | None:
    """``claim_guard``: forbidden phrases absent, required phrases present (3.3 #10)."""

    if not spec.forbidden_claims and not spec.required_claims:
        return None
    texts = [claim.text for claim in view.claims if claim.text]
    haystack = " ".join(texts).casefold()
    forbidden_hits = [
        phrase for phrase in spec.forbidden_claims if phrase.casefold() in haystack
    ]
    required_missing = [
        phrase for phrase in spec.required_claims if phrase.casefold() not in haystack
    ]
    actual = {
        "texts": texts,
        "forbidden_found": forbidden_hits,
        "required_missing": required_missing,
    }
    if forbidden_hits:
        return _result(
            "claim_guard",
            False,
            f"forbidden claim phrase(s) in the answer: {_preview(forbidden_hits)}",
            expected={"forbidden_absent": list(spec.forbidden_claims)},
            actual=actual,
            reason="forbidden_claim",
        )
    if required_missing:
        return _result(
            "claim_guard",
            False,
            f"required claim phrase(s) absent: {_preview(required_missing)}",
            expected={"required_present": list(spec.required_claims)},
            actual=actual,
            reason="missing_required_claim",
        )
    return _result(
        "claim_guard",
        True,
        f"claim phrases respected ({len(texts)} conclusion text(s) checked)",
        actual=actual,
    )


def _reported_tool_calls(payload: Mapping[str, Any]) -> int | None:
    """Tool calls the payload itself accounted for (``budgets.usage``)."""

    budgets = as_mapping(payload.get("budgets"))
    usage = as_mapping(budgets.get("usage"))
    return as_int(usage.get("max_tool_calls"))


def _check_budget(
    spec: TaskSpec, trace: TaskTrace, payload: Mapping[str, Any]
) -> CheckResult | None:
    """``budget``: the task's tool-call budget was not exceeded (3.3 #11)."""

    if spec.max_tool_calls is None:
        return None
    recorded = len(trace.tool_calls)
    reported = _reported_tool_calls(payload)
    # The conservative maximum of the two accountings: the recorded calls plus,
    # when the payload claims more, what the run itself counted.
    used = max([recorded] + ([reported] if reported is not None else []))
    actual = {
        "recorded": recorded,
        "reported": reported,
        "used": used,
        "limit": spec.max_tool_calls,
    }
    if used > spec.max_tool_calls:
        return _result(
            "budget",
            False,
            f"{used} tool call(s) exceed the task budget of {spec.max_tool_calls}",
            expected=spec.max_tool_calls,
            actual=actual,
        )
    return _result(
        "budget",
        True,
        f"{used} tool call(s) within the task budget of {spec.max_tool_calls}",
        actual=actual,
    )


def _check_unsupported_numbers(
    view: AnswerView, payloads: Mapping[str, Mapping[str, Any]]
) -> CheckResult:
    """``no_unsupported_numbers``: every finding number has a source (3.3 #12).

    Re-derived here from the cited evidence payloads; the run's own
    ``validation_problems`` list is never consulted.
    """

    checked = 0
    problems: list[str] = []
    for claim in view.claims:
        numbers = {
            key: value for key, value in claim.numbers.items() if is_number(value)
        }
        if not numbers:
            continue
        checked += len(numbers)
        if not claim.evidence_ids:
            problems.append(
                f"{claim.label} reports {_preview(sorted(numbers))} without citing evidence"
            )
            continue
        cited = [payloads.get(identifier, {}) for identifier in claim.evidence_ids]
        for key, value in numbers.items():
            found, actual = resolve_number(cited, key)
            if not found:
                problems.append(
                    f"{claim.label}.{key}={_format_value(value)} is absent from the "
                    "cited evidence"
                )
            elif not is_number(actual):
                problems.append(
                    f"{claim.label}.{key} is not numeric in the cited evidence"
                )
            elif not numbers_equal(value, actual):
                problems.append(
                    f"{claim.label}.{key}={_format_value(value)} disagrees with the "
                    f"evidence value {_format_value(actual)}"
                )
    actual = {"checked": checked, "problems": problems}
    if checked == 0:
        return _result(
            "no_unsupported_numbers",
            True,
            "the answer publishes no numeric finding to trace",
            actual=actual,
        )
    if problems:
        return _result(
            "no_unsupported_numbers",
            False,
            "; ".join(problems[:_DETAIL_LIMIT])
            + (
                f" (+{len(problems) - _DETAIL_LIMIT} more)"
                if len(problems) > _DETAIL_LIMIT
                else ""
            ),
            expected="every published number traceable to its evidence",
            actual=actual,
        )
    return _result(
        "no_unsupported_numbers",
        True,
        f"all {checked} published number(s) traceable to their evidence",
        actual=actual,
    )


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def _reason_of(check: CheckResult) -> str:
    record = check.actual if isinstance(check.actual, Mapping) else {}
    return as_text(record.get("reason"))


def classify_failure(outcome: TaskOutcome) -> str:
    """Name the failure class of an outcome (contract 3.5, fixed vocabulary).

    The class is derived from the checks, in a fixed priority order, so it is
    reproducible: the same outcome always yields the same class.  A fully passing
    outcome has no class and returns ``""``.
    """

    for name in _FAILURE_PRIORITY:
        check = outcome.check(name)
        if check is None or check.passed:
            continue
        reason = _reason_of(check)
        classified = _FAILURE_BY_REASON.get((name, reason))
        if classified is None:
            classified = _FAILURE_BY_CHECK.get(name)
        if classified is None:
            # A check nobody classified is reported as such instead of silently
            # disappearing from the histogram (the test suite pins exhaustiveness).
            return "unclassified"
        return classified
    return ""


# ---------------------------------------------------------------------------
# Usage and cost accounting
# ---------------------------------------------------------------------------

#: Token keys providers use, in priority order (mirrors the observability layer).
_PROMPT_KEYS: tuple[str, ...] = ("prompt_tokens", "input_tokens", "promptTokenCount")
_COMPLETION_KEYS: tuple[str, ...] = (
    "completion_tokens",
    "output_tokens",
    "candidatesTokenCount",
    "outputTokenCount",
)
_TOTAL_KEYS: tuple[str, ...] = ("total_tokens", "totalTokenCount")


def _first_int(source: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = as_int(source.get(key))
        if value is not None:
            return value
    return None


def _usage_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run usage recorded on the payload (defensive fallback for a trace)."""

    for key in ("usage", "usage_summary"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping) and any(
            item in candidate for item in (*_PROMPT_KEYS, *_COMPLETION_KEYS, *_TOTAL_KEYS)
        ):
            return as_mapping(candidate)
    return {}


def usage_accounting(trace: TaskTrace) -> dict[str, Any]:
    """Normalize the token accounting of one trace.

    Measured and estimated tokens are reported separately and never blended: an
    estimated count is not a measurement, and a count nobody recorded is ``None``
    rather than zero (contract 3.6 / 16-B1).
    """

    usage = as_mapping(trace.usage)
    source = "trace" if usage else ""
    if not usage:
        usage = _usage_from_payload(trace.payload)
        source = "payload" if usage else ""
    prompt = _first_int(usage, _PROMPT_KEYS)
    completion = _first_int(usage, _COMPLETION_KEYS)
    total = _first_int(usage, _TOTAL_KEYS)
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    if total is None and prompt is None and completion is None:
        return {
            "recorded": False,
            "source": "",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "measured_tokens": None,
            "estimated_tokens": None,
            "estimated": None,
        }
    estimated = as_bool(usage.get("estimated"))
    is_estimate = bool(estimated)
    return {
        "recorded": True,
        "source": source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(total or 0),
        "measured_tokens": 0 if is_estimate else int(total or 0),
        "estimated_tokens": int(total or 0) if is_estimate else 0,
        "estimated": is_estimate,
    }


def _price_entry(
    price_table: Mapping[str, Any], provider: str, model: str
) -> Mapping[str, Any]:
    """Find the price entry of one model, or ``{}``.

    Two spellings are accepted because both exist in this repository: the
    observability layer's ``prompt_per_1k``/``completion_per_1k`` and the
    benchmark harness's ``input_per_million``/``output_per_million``.
    """

    candidates = [
        f"{provider}/{model}" if provider and model else "",
        model,
        provider,
        "*",
        "default",
    ]
    for key in candidates:
        if not key:
            continue
        entry = price_table.get(key)
        if isinstance(entry, Mapping):
            return as_mapping(entry)
    return {}


def estimate_cost(
    usage: Mapping[str, Any],
    price_table: Mapping[str, Any] | None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> float | None:
    """Cost of one task from a price table, or ``None`` when it cannot be priced.

    ``None`` -- never ``0`` -- means "no bill could be computed": no price table,
    no recorded tokens, or no entry for this model.  A missing price is not a
    free run (16-M1).
    """

    if not price_table or not usage.get("recorded"):
        return None
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    if int(prompt) + int(completion) <= 0:
        return None
    entry = _price_entry(price_table, as_text(provider), as_text(model))
    if not entry:
        return None
    per_1k_prompt = _first_number(entry, ("prompt_per_1k", "input_per_1k"))
    per_1k_completion = _first_number(entry, ("completion_per_1k", "output_per_1k"))
    if per_1k_prompt is None:
        per_million = _first_number(entry, ("prompt_per_million", "input_per_million"))
        per_1k_prompt = None if per_million is None else per_million / 1000.0
    if per_1k_completion is None:
        per_million = _first_number(entry, ("completion_per_million", "output_per_million"))
        per_1k_completion = None if per_million is None else per_million / 1000.0
    if per_1k_prompt is None and per_1k_completion is None:
        return None
    cost = int(prompt) / 1000.0 * (per_1k_prompt or 0.0)
    cost += int(completion) / 1000.0 * (per_1k_completion or 0.0)
    return round(cost, 8)


def _first_number(source: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = as_number(source.get(key))
        if value is not None:
            return value
    return None


def cost_accounting(
    trace: TaskTrace,
    usage: Mapping[str, Any],
    price_table: Mapping[str, Any] | None,
) -> tuple[float | None, str | None]:
    """Cost of one task plus its basis (``reported`` by the runner or ``priced``)."""

    if trace.cost_usd is not None:
        return round(float(trace.cost_usd), 8), "reported"
    priced = estimate_cost(
        usage, price_table, provider=trace.provider, model=trace.model
    )
    if priced is None:
        return None, None
    return priced, "priced"


# ---------------------------------------------------------------------------
# Task evaluation
# ---------------------------------------------------------------------------


def evaluate_task(
    spec: TaskSpec,
    trace: TaskTrace,
    *,
    price_table: Mapping[str, Any] | None = None,
) -> TaskOutcome:
    """Score one trace against one gold task (contract 3.2).

    Every check is derived from the raw payload and the recorded tool calls.  The
    outcome carries the raw trace so that :func:`recompute` can rebuild the whole
    report from the results alone.
    """

    payload = as_mapping(trace.payload)
    entries = evidence_entries(payload)
    kinds = evidence_kind_set(entries)
    payloads = evidence_payloads(entries)
    known_ids = set(payloads)
    view = answer_view(payload)
    steps = step_records(payload)
    statuses = status_candidates(payload)
    clarified = is_clarification(payload)
    denied, denial_signals = policy_denial(payload, trace.tool_calls)
    usage = usage_accounting(trace)
    cost, cost_basis = cost_accounting(trace, usage, price_table)

    checks: list[CheckResult] = [
        _check_trace_available(trace),
        _check_status(spec, statuses, stop_reason(payload)),
        _check_outcome_kind(spec, clarified, denied, denial_signals),
        _check_required_evidence(spec, kinds, entries),
        _check_forbidden_evidence(spec, kinds),
        _check_expected_values(spec, payload),
        _check_evidence_anchor(spec, view, known_ids),
        _check_tool_legality(spec, trace),
        _check_tool_validity(spec, trace, steps),
        _check_required_steps(spec, steps),
    ]
    for optional in (_check_claim_guard(spec, view),):
        if optional is not None:
            checks.append(optional)
    checks.append(_check_unsupported_numbers(view, payloads))
    budget_check = _check_budget(spec, trace, payload)
    if budget_check is not None:
        checks.append(budget_check)

    outcome = TaskOutcome(
        task_id=spec.task_id,
        split=spec.split,
        dataset=spec.dataset,
        passed=all(check.passed for check in checks),
        checks=checks,
        failure_class=None,
        tool_calls=len(trace.tool_calls),
        usage_tokens=usage["total_tokens"],
        cost_usd=cost,
        wall_ms=round(trace.wall_ms, 3),
        coverage=list(spec.coverage),
        expected_outcome=spec.expected_outcome,
        expected_status=list(spec.expected_status),
        requires_evidence=spec.requires_evidence(),
        multi_step=spec.is_multi_step(),
        clarified=clarified,
        expects_clarification=spec.expected_outcome == "clarification",
        measured_tokens=usage["measured_tokens"],
        estimated_tokens=usage["estimated_tokens"],
        cost_basis=cost_basis,
        provider=trace.provider,
        model=trace.model,
        error=trace.error,
        tool_calls_observed=len(trace.tool_calls) + len(steps),
        usage_source=usage["source"] or None,
        trace=trace.model_dump(mode="json"),
        schema_precision=(len(set(getattr(spec, "expected_tables", [])) & set(payload.get("relevant_tables") or [])) / len(payload["relevant_tables"])
                          if getattr(spec, "expected_tables", None) and payload.get("relevant_tables") else None),
        schema_recall=(len(set(getattr(spec, "expected_tables", [])) & set(payload.get("relevant_tables") or [])) / len(set(spec.expected_tables))
                       if getattr(spec, "expected_tables", None) else None),
    )
    outcome.failure_class = classify_failure(outcome) or None
    return outcome


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    """A ratio, or ``None`` when it cannot be measured (never a fake 0 or 1)."""

    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile: deterministic, no interpolation, no numpy."""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 3)


def _clarification_counts(outcomes: Sequence[TaskOutcome]) -> dict[str, Any]:
    """True/false positives and negatives of the clarification decision."""

    tp = fp = fn = tn = 0
    for outcome in outcomes:
        if outcome.expects_clarification and outcome.clarified:
            tp += 1
        elif outcome.expects_clarification and not outcome.clarified:
            fn += 1
        elif not outcome.expects_clarification and outcome.clarified:
            fp += 1
        else:
            tn += 1
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "expected_clarification": tp + fn,
        "not_expected_clarification": fp + tn,
        "true_positive_rate": _rate(tp, tp + fn),
        "true_negative_rate": _rate(tn, tn + fp),
        "rate": _rate(tp + tn, tp + fp + fn + tn),
    }


def _check_passed(outcome: TaskOutcome, name: str) -> bool:
    check = outcome.check(name)
    return bool(check is not None and check.passed)


def _has_unsupported_assertion(outcome: TaskOutcome) -> bool:
    """Whether the answer asserted something it could not support."""

    if not _check_passed(outcome, "evidence_anchor"):
        return True
    if not _check_passed(outcome, "no_unsupported_numbers"):
        return True
    guard = outcome.check("claim_guard")
    return bool(
        guard is not None
        and not guard.passed
        and _reason_of(guard) == "forbidden_claim"
    )


def _usage_bundle(outcomes: Sequence[TaskOutcome]) -> dict[str, Any]:
    """Measured vs estimated token accounting, kept apart (contract 3.6)."""

    measured = 0
    estimated = 0
    measured_tasks = 0
    estimated_tasks = 0
    unmeasured: list[str] = []
    for outcome in outcomes:
        if outcome.measured_tokens is None and outcome.estimated_tokens is None:
            unmeasured.append(outcome.task_id)
            continue
        measured += int(outcome.measured_tokens or 0)
        estimated += int(outcome.estimated_tokens or 0)
        if outcome.estimated_tokens:
            estimated_tasks += 1
        if not outcome.estimated_tokens and outcome.measured_tokens:
            measured_tasks += 1
    recorded = len(outcomes) - len(unmeasured)
    if recorded == 0:
        return {
            "recorded_tasks": 0,
            "unmeasured_tasks": sorted(unmeasured),
            "measured_tasks": 0,
            "estimated_tasks": 0,
            "measured_tokens": None,
            "estimated_tokens": None,
            "total_tokens": None,
            "complete": False,
        }
    return {
        "recorded_tasks": recorded,
        "unmeasured_tasks": sorted(unmeasured),
        "measured_tasks": measured_tasks,
        "estimated_tasks": estimated_tasks,
        "measured_tokens": measured,
        "estimated_tokens": estimated,
        "total_tokens": measured + estimated,
        "complete": not unmeasured,
    }


def _cost_bundle(outcomes: Sequence[TaskOutcome]) -> dict[str, Any]:
    """Cost accounting with its basis; ``None`` -- never ``0`` -- when unpriced."""

    costs = [float(outcome.cost_usd) for outcome in outcomes if outcome.cost_usd is not None]
    without = sorted(
        outcome.task_id for outcome in outcomes if outcome.cost_usd is None
    )
    reported = sum(1 for outcome in outcomes if outcome.cost_basis == "reported")
    priced = sum(1 for outcome in outcomes if outcome.cost_basis == "priced")
    return {
        "cost_usd": round(sum(costs), 8) if costs else None,
        "tasks_with_cost": len(costs),
        "reported_cost_tasks": reported,
        "priced_cost_tasks": priced,
        "price_table_configured": priced > 0,
        "tasks_without_cost": without,
        "note": (
            "cost is None, not 0, when no price could be computed; a task without "
            "recorded usage or without a price entry stays unpriced"
        ),
    }


def _failure_histogram(outcomes: Sequence[TaskOutcome]) -> dict[str, int]:
    """Failure-class histogram over the failing outcomes (stable order)."""

    counts: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.passed:
            continue
        key = outcome.failure_class or "unclassified"
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts, key=lambda item: (-counts[item], item))}


def _metrics_bundle(outcomes: Sequence[TaskOutcome]) -> dict[str, Any]:
    """Every metric of contract 3.6 for one set of outcomes."""

    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome.passed)
    multi = [outcome for outcome in outcomes if outcome.multi_step]
    evidence_tasks = [outcome for outcome in outcomes if outcome.requires_evidence]
    legality = [outcome for outcome in outcomes if outcome.tool_calls > 0]
    validity = [outcome for outcome in outcomes if outcome.tool_calls_observed > 0]
    errors = sorted(outcome.task_id for outcome in outcomes if outcome.error)
    walls = [float(outcome.wall_ms) for outcome in outcomes]
    tool_calls = [int(outcome.tool_calls) for outcome in outcomes]
    cost = _cost_bundle(outcomes)
    return {
        "task_count": total,
        "sql_result_accuracy": _rate(sum(_check_passed(o, "expected_values") and _check_passed(o, "trace_available")
                                         for o in outcomes if o.expected_outcome == "query"),
                                     sum(o.expected_outcome == "query" for o in outcomes)),
        "schema_linking_precision": (sum(o.schema_precision for o in outcomes if o.schema_precision is not None) /
                                     sum(o.schema_precision is not None for o in outcomes)
                                     if any(o.schema_precision is not None for o in outcomes) else None),
        "schema_linking_recall": (sum(o.schema_recall for o in outcomes if o.schema_recall is not None) /
                                  sum(o.schema_recall is not None for o in outcomes)
                                  if any(o.schema_recall is not None for o in outcomes) else None),
        "passed_tasks": passed,
        "task_success_rate": _rate(passed, total),
        "multi_step_success_rate": _rate(
            sum(1 for outcome in multi if outcome.passed), len(multi)
        ),
        "multi_step_tasks": len(multi),
        "clarification_appropriateness": _clarification_counts(outcomes),
        "evidence_coverage_rate": _rate(
            sum(
                1
                for outcome in evidence_tasks
                if _check_passed(outcome, "required_evidence")
                and _check_passed(outcome, "evidence_anchor")
            ),
            len(evidence_tasks),
        ),
        "evidence_tasks": len(evidence_tasks),
        "unsupported_assertion_rate": _rate(
            sum(1 for outcome in outcomes if _has_unsupported_assertion(outcome)), total
        ),
        "unsupported_tasks": [
            outcome.task_id for outcome in outcomes if _has_unsupported_assertion(outcome)
        ],
        "tool_legality_rate": _rate(
            sum(1 for outcome in legality if _check_passed(outcome, "tool_legality")),
            len(legality),
        ),
        "tool_validity_rate": _rate(
            sum(1 for outcome in validity if _check_passed(outcome, "tool_validity")),
            len(validity),
        ),
        "tool_calls_measured_tasks": len(legality),
        "tool_calls_observed_tasks": len(validity),
        "avg_tool_calls": round(sum(tool_calls) / total, 3) if total else None,
        "tool_call_total": sum(tool_calls),
        "p50_wall_ms": _percentile(walls, 0.5),
        "p95_wall_ms": _percentile(walls, 0.95),
        "wall_ms_total": round(sum(walls), 3) if total else None,
        "usage": _usage_bundle(outcomes),
        "cost_usd": cost["cost_usd"],
        "cost": cost,
        "failure_classes": _failure_histogram(outcomes),
        "runner_error_tasks": errors,
    }


def _group_by(outcomes: Sequence[TaskOutcome], key_of: Any) -> dict[str, list[TaskOutcome]]:
    """Group outcomes by key (per-category lists sorted for determinism)."""

    buckets: dict[str, list[TaskOutcome]] = {}
    for outcome in outcomes:
        for key in sorted(set(key_of(outcome))):
            buckets.setdefault(key, []).append(outcome)
    return {key: buckets[key] for key in sorted(buckets)}


def _coverage_categories(outcome: TaskOutcome) -> list[str]:
    return list(outcome.coverage) or ["unclassified"]


def _group_of(outcome: TaskOutcome) -> str:
    """``policy_probe`` vs ``model_e2e``: two safety metrics, never blended (3.6)."""

    categories = {normalize_name(item) for item in outcome.coverage}
    runner = outcome.trace.get("runner")
    if "policy_rejection" in categories and runner != "model_e2e":
        return "policy_probe"
    return runner or "unclassified"


def _threshold_mapping(thresholds: Any) -> dict[str, Any]:
    """Normalize a tier config/mapping to a plain JSON-serializable mapping."""

    if thresholds is None:
        return {}
    if isinstance(thresholds, Mapping):
        return {str(key): value for key, value in thresholds.items()}
    dump = getattr(thresholds, "model_dump", None)
    if callable(dump):
        return as_mapping(dump())
    raise ValueError(
        "thresholds must be a mapping, a TierConfig, or None"
    )


def aggregate(
    outcomes: Sequence[TaskOutcome],
    *,
    thresholds: Any = None,
) -> dict[str, Any]:
    """Aggregate outcomes into the benchmark report (contract 3.2/3.6).

    Rates that cannot be measured are ``None`` (no tasks, no recorded tool calls,
    no usage, no price) instead of a flattering 0 or 1, and the per-split /
    per-dataset / per-coverage breakdowns plus the two safety groups are reported
    side by side so a score is never mixed up with another population.
    """

    items = list(outcomes)
    report = _metrics_bundle(items)
    report["results"] = [outcome.model_dump(mode="json") for outcome in items]
    report["by_split"] = {
        key: _metrics_bundle(group)
        for key, group in _group_by(
            items, lambda item: [item.split or "unclassified"]
        ).items()
    }
    report["by_dataset"] = {
        key: _metrics_bundle(group)
        for key, group in _group_by(items, lambda item: [item.dataset]).items()
    }
    report["by_coverage"] = {
        key: _metrics_bundle(group)
        for key, group in _group_by(items, _coverage_categories).items()
    }
    report["groups"] = {
        key: _metrics_bundle(group)
        for key, group in _group_by(items, lambda item: [_group_of(item)]).items()
    }
    tier = _threshold_mapping(thresholds)
    report["thresholds"] = tier
    report["threshold_violations"] = check_metrics(report, tier) if tier else []
    return report


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------


def _is_outcome_record(record: Mapping[str, Any]) -> bool:
    """Whether one ``report["results"]`` entry is an outcome (vs a raw trace)."""

    return isinstance(record.get("checks"), (list, tuple)) and "passed" in record


def recompute(report: dict, specs: Sequence[TaskSpec]) -> dict:
    """Rebuild the whole report from ``report["results"]`` alone (contract 3.7).

    The step's exit gate is "从原始 case 与 trace 可重算报告", so this is a real
    re-derivation, not a re-serialization:

    * every check is re-run from the raw payload and the recorded tool calls
      (``evaluate_task``), which also proves a stored ``passed`` flag is never
      trusted;
    * a result stored as a raw runner record (``scripts/benchmark_agent.py``
      keeps those) is scored against its gold spec the same way;
    * **only** the runner-recorded usage and cost facts are carried over from the
      stored result, because re-pricing tokens is a billing input, not a
      self-assessment -- the numbers themselves are never re-invented.
    """

    results = report.get("results")
    if not isinstance(results, (list, tuple)):
        raise ValueError('report["results"] must be a list of task results')
    index = spec_index(specs)
    outcomes: list[TaskOutcome] = []
    for entry in results:
        record = as_mapping(entry)
        if not record:
            raise ValueError("report['results'] contains a non-object entry")
        spec = index.get(as_text(record.get("task_id")))
        if _is_outcome_record(record):
            stored = TaskOutcome.model_validate(record)
            raw = as_mapping(record.get("trace"))
        else:
            raw = as_mapping(record)
            stored = None
        if spec is None:
            if stored is None:
                raise ValueError(
                    f"no gold task spec for result {as_text(record.get('task_id'))!r}; "
                    "recompute cannot score a trace without its gold"
                )
            # No spec: keep the recorded checks but never the recorded verdict.
            fresh = stored.model_copy(
                update={"passed": all(check.passed for check in stored.checks)}
            )
        else:
            trace = TaskTrace.model_validate(raw)
            fresh = evaluate_task(spec, trace)
            if stored is not None:
                fresh = fresh.model_copy(
                    update={
                        "usage_tokens": stored.usage_tokens,
                        "measured_tokens": stored.measured_tokens,
                        "estimated_tokens": stored.estimated_tokens,
                        "cost_usd": stored.cost_usd,
                        "cost_basis": stored.cost_basis,
                        "usage_source": stored.usage_source,
                    }
                )
        fresh = fresh.model_copy(update={"failure_class": classify_failure(fresh) or None})
        outcomes.append(fresh)
    thresholds = _threshold_mapping(report.get("thresholds"))
    return aggregate(outcomes, thresholds=thresholds or None)


__all__ = [
    "CHECK_NAMES",
    "FAILURE_CLASSES",
    "AnswerView",
    "CheckResult",
    "Claim",
    "TaskOutcome",
    "aggregate",
    "answer_view",
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
    "is_number",
    "legacy_answer_of",
    "normalize_name",
    "normalize_status",
    "numbers_equal",
    "policy_denial",
    "recompute",
    "resolve_number",
    "status_candidates",
    "step_records",
    "stop_reason",
]
