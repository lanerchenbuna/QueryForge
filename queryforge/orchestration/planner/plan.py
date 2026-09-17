"""Typed analysis plans and their pre-execution validator.

A plan is a small DAG of explicit actions.  Nothing runs until
:class:`PlanValidator` has proved that the plan is well formed (unique ids, an
acyclic dependency graph, existing dependencies), that every action maps onto a
tool the registry actually offers *in the requested mode*, that the inputs
satisfy that action's parameter contract, and that the requested per-step
budgets fit inside the shared budget manager.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from queryforge.orchestration.tools.budget import BUDGET_KEYS
from queryforge.orchestration.tools.registry import ToolRegistry
from queryforge.orchestration.tools.specs import validate_params

PlanStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "partial",
    "blocked",
    "cancelled",
    "needs_clarification",
]

#: The finite action vocabulary of the first planner version.
PLAN_ACTIONS: tuple[str, ...] = (
    "resolve_metric",
    "check_data_quality",
    "query_metric",
    "compare_periods",
    "drill_down",
    "calculate_contribution",
    "detect_anomaly",
    "render_chart",
    "compose_answer",
)

#: Planner action -> registered tool whose contract that action executes.
#: ``None`` marks an action the executor performs locally (answer composition).
#: Keeping the binding explicit is what lets the validator prove "this action is
#: available in this mode" without duplicating the registry catalogue.
ACTION_TOOL_MAP: dict[str, str | None] = {
    "resolve_metric": "list_metrics",
    "check_data_quality": "check_data_quality",
    "query_metric": "execute_sql",
    "compare_periods": "compare_periods",
    "drill_down": "drill_down",
    "calculate_contribution": "calculate_contribution",
    "detect_anomaly": "detect_anomaly",
    "render_chart": "render_chart",
    "compose_answer": None,
}

#: Actions the executor implements itself (no tool dispatch).
LOCAL_ACTIONS: frozenset[str] = frozenset({"compose_answer"})

_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}

#: Planner-level parameter contracts, used when the action's inputs are a
#: superset of the bound tool's own parameters (for example ``query_metric``
#: carries a metric reference and dimensions, not a finished SQL string).
ACTION_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "resolve_metric": {
        "type": "object",
        "properties": {
            "term": _STRING,
            "question": _STRING,
            "dimensions": _STRING_LIST,
            "domain_id": _STRING,
        },
        "additionalProperties": False,
    },
    "query_metric": {
        "type": "object",
        "properties": {
            "metric": _STRING,
            "metric_ids": _STRING_LIST,
            "dimensions": _STRING_LIST,
            "filters": {"type": "array", "items": {"type": "object"}},
            "time_range": _STRING,
            "time_grain": _STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "sql": _STRING,
            "fallback_sql": _STRING,
        },
        "additionalProperties": False,
    },
    "compose_answer": {
        "type": "object",
        "properties": {
            "require_evidence": _STRING_LIST,
            "require_outputs": _STRING_LIST,
        },
        "additionalProperties": False,
    },
    "compare_periods": {"type": "object", "properties": {}, "additionalProperties": False},
    "drill_down": {"type": "object", "properties": {}, "additionalProperties": False},
    "calculate_contribution": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "detect_anomaly": {"type": "object", "properties": {}, "additionalProperties": False},
    "render_chart": {"type": "object", "properties": {}, "additionalProperties": False},
}


class PlanStep(BaseModel):
    """One action in an analysis plan."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnalysisPlan(BaseModel):
    """A versioned, validated analysis plan."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:12]}")
    task_id: str | None = None
    question: str = Field(min_length=1)
    domain_id: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    status: PlanStatus = "pending"

    def step(self, step_id: str) -> PlanStep | None:
        return next((item for item in self.steps if item.id == step_id), None)

    def expected_evidence(self) -> list[str]:
        """Evidence kinds the task promised, excluding the composing step."""

        kinds: list[str] = []
        for item in self.steps:
            if item.action in LOCAL_ACTIONS:
                continue
            for kind in item.expected_evidence:
                if kind not in kinds:
                    kinds.append(kind)
        return kinds

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class PlanViolation(ValueError):
    """The plan is not executable; every reason is reported at once."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__("invalid analysis plan: " + "; ".join(self.violations))


class PlanValidator:
    """Static checks that must pass before any tool call happens."""

    @classmethod
    def validate(
        cls,
        plan: AnalysisPlan,
        registry: ToolRegistry,
        mode: str = "execute",
    ) -> None:
        """Raise :class:`PlanViolation` unless the plan is executable."""

        violations: list[str] = []
        cls._check_structure(plan, violations)
        if violations:
            # Dependencies are unreliable once ids/edges are broken.
            raise PlanViolation(violations)
        order = cls._topological_order(plan)  # raises on cycles
        cls._check_actions(plan, registry, mode, violations)
        cls._check_budgets(plan, registry, violations)
        if violations:
            raise PlanViolation(violations)
        assert len(order) == len(plan.steps)

    # ------------------------------------------------------------------ checks

    @classmethod
    def _check_structure(cls, plan: AnalysisPlan, violations: list[str]) -> None:
        if not plan.steps:
            violations.append("empty_plan: a plan must contain at least one step")
            return
        ids = [step.id for step in plan.steps]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            violations.append(f"duplicate_step_id: {', '.join(duplicates)}")
        known = set(ids)
        for step in plan.steps:
            if step.id in step.depends_on:
                violations.append(f"self_dependency: step {step.id!r} depends on itself")
            for dependency in step.depends_on:
                if dependency not in known:
                    violations.append(
                        f"unknown_dependency: step {step.id!r} depends on {dependency!r}"
                    )

    @classmethod
    def _check_actions(
        cls,
        plan: AnalysisPlan,
        registry: ToolRegistry,
        mode: str,
        violations: list[str],
    ) -> None:
        for step in plan.steps:
            if step.action not in PLAN_ACTIONS:
                violations.append(f"unknown_action: {step.action!r}")
                continue
            if step.action not in ACTION_TOOL_MAP:
                violations.append(f"unbound_action: {step.action!r}")
                continue
            tool = ACTION_TOOL_MAP[step.action]
            if tool is not None:
                if not registry.has(tool):
                    violations.append(
                        f"unregistered_tool: action {step.action!r} needs tool {tool!r}"
                    )
                    continue
                if not registry.allows(tool, mode):
                    violations.append(
                        f"mode_not_allowed: action {step.action!r} (tool {tool!r}) "
                        f"is not available in mode {mode!r}"
                    )
                    continue
            tool = ACTION_TOOL_MAP[step.action]
            try:
                if tool == step.action:
                    # The action's inputs are the registered tool's own contract,
                    # so the registry's validator is used verbatim.
                    registry.validate_params(tool, step.inputs)
                else:
                    validate_params(
                        step.action,
                        cls.param_schema(step.action, registry),
                        step.inputs,
                    )
            except ValueError as exc:
                violations.append(f"invalid_params: step {step.id!r}: {exc}")

    @classmethod
    def _check_budgets(
        cls, plan: AnalysisPlan, registry: ToolRegistry, violations: list[str]
    ) -> None:
        manager = registry.budget_manager
        limits = manager.limits.as_dict()
        total_calls = 0
        for step in plan.steps:
            for key, value in step.budget.items():
                if key not in BUDGET_KEYS:
                    violations.append(f"unknown_budget_key: step {step.id!r} uses {key!r}")
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    violations.append(
                        f"invalid_budget: step {step.id!r} {key}={value!r} must be >= 0"
                    )
                    continue
                if float(value) > float(limits[key]):
                    violations.append(
                        f"budget_exceeds_limit: step {step.id!r} {key}={value} > "
                        f"manager limit {limits[key]}"
                    )
            calls = step.budget.get("max_tool_calls")
            if isinstance(calls, (int, float)) and not isinstance(calls, bool):
                total_calls += int(calls)
        if total_calls > float(limits["max_tool_calls"]):
            violations.append(
                f"budget_exceeds_limit: plan requests {total_calls} tool calls but the "
                f"manager allows {int(limits['max_tool_calls'])}"
            )

    # ------------------------------------------------------------------- order

    @classmethod
    def topological_order(cls, plan: AnalysisPlan) -> list[PlanStep]:
        """Return steps in dependency order (raises :class:`PlanViolation`)."""

        violations: list[str] = []
        cls._check_structure(plan, violations)
        if violations:
            raise PlanViolation(violations)
        return cls._topological_order(plan)

    @classmethod
    def _topological_order(cls, plan: AnalysisPlan) -> list[PlanStep]:
        remaining = {step.id: set(step.depends_on) for step in plan.steps}
        by_id = {step.id: step for step in plan.steps}
        order: list[PlanStep] = []
        while remaining:
            ready = [step_id for step_id, deps in remaining.items() if not deps]
            if not ready:
                cyclic = ", ".join(sorted(remaining))
                raise PlanViolation([f"cycle_detected: {cyclic}"])
            for step_id in sorted(ready):
                order.append(by_id[step_id])
                del remaining[step_id]
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    @staticmethod
    def param_schema(action: str, registry: ToolRegistry) -> dict[str, Any]:
        """The contract an action's ``inputs`` must satisfy.

        Actions whose name is also a registered tool (for example
        ``check_data_quality``) validate against that tool's own parameter
        schema through :meth:`ToolRegistry.validate_params`; the remaining
        planner actions use the planner-level contract below, because their
        inputs are a superset of the bound tool's parameters (``query_metric``
        names a metric and dimensions rather than a finished SQL string).
        """

        tool = ACTION_TOOL_MAP.get(action)
        if tool is not None and tool == action and registry.has(tool):
            return registry.resolve(tool).parameter_schema
        return ACTION_PARAM_SCHEMAS.get(action, {"type": "object", "properties": {}})


__all__ = [
    "ACTION_PARAM_SCHEMAS",
    "ACTION_TOOL_MAP",
    "AnalysisPlan",
    "LOCAL_ACTIONS",
    "PLAN_ACTIONS",
    "PlanStatus",
    "PlanStep",
    "PlanValidator",
    "PlanViolation",
]
