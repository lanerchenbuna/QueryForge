"""Dependency-ordered execution of an analysis plan with bounded replanning.

The executor is deliberately deterministic about *control flow* and agnostic
about *who* produced the plan: independent steps run concurrently on a thread
pool that shares exactly one :class:`~queryforge.orchestration.tools.budget.BudgetManager`,
dependent steps wait for their dependencies, and every failure is classified
through :mod:`queryforge.workflow.errors`.

Stopping is evidence driven: the task only reports ``succeeded`` when
``compose_answer`` completed *and* every promised evidence kind was produced.
A missing intermediate evidence kind yields ``partial``/``failed`` — never
``succeeded``.  When a metric query fails because the requested slice has no
data, the executor may replan at most ``max_replans`` times by swapping in a
legal alternative dimension that the semantic model declares.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from queryforge.core.observability import (
    Span,
    SpanRecorder,
    sanitize_attributes,
    stable_digest,
)
from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.semantic.schemas import MetricMatch
from queryforge.domain.semantic.sql_validator import QuerySpec, QuerySpecCompiler
from queryforge.orchestration.planner.plan import (
    LOCAL_ACTIONS,
    AnalysisPlan,
    PlanStep,
    PlanValidator,
    PlanViolation,
)
from queryforge.orchestration.runtime.execution_journal import (
    IDEMPOTENCY_POLICY,
    ExecutionJournal,
    RunNotResumable,
)
from queryforge.orchestration.tools.budget import BudgetManager
from queryforge.orchestration.tools.registry import ToolRegistry
from queryforge.orchestration.tools.specs import (
    ToolBudgetError,
    ToolContext,
    ToolDenied,
    ToolObservation,
    ToolUnavailable,
)
from queryforge.workflow.errors import WorkflowErrorCategory, categorize_error

StepStatus = Literal["pending", "running", "succeeded", "failed", "blocked", "skipped"]


class UnsupportedAnalysisError(ValueError):
    """The request cannot be answered with the governed semantic model.

    Raised for a breakdown the semantic model cannot express (an undeclared
    dimension, no governed join path, or a fan-out risk). It is deliberately not
    a generic ``ValueError``: the failure must surface as an honest unsupported
    request instead of degrading into a preview that answers something else.
    """


def _entity_of_dimension(model: Any, reference: str) -> Any | None:
    """The entity named by a dimension reference (``entity`` or ``entity.dimension``)."""
    text = str(reference).strip()
    entity_name, _, dimension_name = text.partition(".")
    for entity in getattr(model, "entities", ()) or ():
        if entity_name and getattr(entity, "name", None) == entity_name:
            if not dimension_name:
                return entity
            if any(
                getattr(dimension, "name", None) == dimension_name
                for dimension in getattr(entity, "dimensions", ()) or ()
            ):
                return entity
    if entity_name and getattr(model, "entities", None) is None:  # pragma: no cover
        return None
    if dimension_name:
        # A bare ``entity.dimension`` whose entity is unknown is not resolvable;
        # fall back to a name-only lookup so a legacy bare dimension still works.
        return None
    for entity in getattr(model, "entities", ()) or ():
        for dimension in getattr(entity, "dimensions", ()) or ():
            if getattr(dimension, "name", None) == text:
                return entity
    return None


class DataAbsentError(ValueError):
    """The requested slice genuinely has no data (typed as a data-quality gap).

    Kept separate from a generic ``ValueError`` so the failure is classified
    ``data_quality`` by the shared taxonomy, which is what makes it eligible for
    the bounded replan below instead of silently reporting an execution error.
    """


def _with_alternative_dimensions(message: str, suggestions: Sequence[str]) -> str:
    """Append the legal untried dimensions a data-absence failure could retry.

    The executor replans onto one of them, so naming them on the error is what
    makes the suggestion visible to the caller; an empty suggestion list leaves
    the message untouched.
    """

    if not suggestions:
        return message
    return f"{message}; legal alternative dimensions: {', '.join(suggestions)}"


#: Month names as the conformed calendar dimension exposes them (``month_name``).
_MONTH_ORDINALS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

#: Calendar shapes a governed time result can label its points with:
#: ``2024-11-03`` / ``2024-11`` / ``2024/11``, ``2024-Q3`` / ``Q3 2024``, ``2024``.
_PERIOD_PATTERNS = (
    (re.compile(r"(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?"), "month"),
    (re.compile(r"(\d{4})[\s\-/]?[Qq]([1-4])"), "quarter"),
    (re.compile(r"[Qq]([1-4])[\s\-/](\d{4})"), "quarter_reversed"),
    (re.compile(r"(\d{4})"), "year"),
)


def _calendar_position(label: str) -> tuple[int, int] | None:
    """Sortable ``(year, month)`` when ``label`` names a calendar period.

    A bare month name (``"September"``, as the calendar dimension's
    ``month_name`` column renders it) carries no year and is reported as year
    ``0``: the caller then only requires the months to advance, so a
    December -> January step still counts as time order. ``None`` means the
    label is not a calendar period at all, which is what a categorical series
    (devices, regions, categories) looks like.
    """

    text = str(label).strip()
    if not text:
        return None
    ordinal = _MONTH_ORDINALS.get(text.casefold())
    if ordinal is not None:
        return (0, ordinal)
    for pattern, kind in _PERIOD_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        groups = match.groups()
        if kind == "month":
            month = int(groups[1])
            return (int(groups[0]), month) if 1 <= month <= 12 else None
        if kind == "quarter":
            return (int(groups[0]), (int(groups[1]) - 1) * 3 + 1)
        if kind == "quarter_reversed":
            return (int(groups[1]), (int(groups[0]) - 1) * 3 + 1)
        return (int(groups[0]), 1)
    return None


#: Evidence kind produced by each action's successful step.
EVIDENCE_KINDS: dict[str, str] = {
    "resolve_metric": "metric_resolution",
    "check_data_quality": "data_quality",
    "query_metric": "metric_value",
    "compare_periods": "period_comparison",
    "drill_down": "drill_down",
    "calculate_contribution": "contribution",
    "detect_anomaly": "anomaly",
    "render_chart": "chart",
    "compose_answer": "answer",
}

#: Step 11 actions whose tool inputs are assembled from governed results.
STEP11_ACTIONS: frozenset[str] = frozenset(
    {
        "compare_periods",
        "drill_down",
        "calculate_contribution",
        "detect_anomaly",
        "render_chart",
    }
)

#: Span status a finished step publishes. The span vocabulary is exactly
#: ``success``/``failed``/``cancelled``
#: (:data:`queryforge.core.observability.SPAN_STATUSES`), so a ``blocked`` step
#: cannot invent a fourth status: it is reported as ``failed`` and its exact step
#: status stays visible in the ``step_status`` attribute rather than being dropped.
_STEP_SPAN_STATUS: dict[str, str] = {
    "succeeded": "success",
    "failed": "failed",
    "blocked": "failed",
    "skipped": "failed",
}


class StepResult(BaseModel):
    """Final state of one executed plan step."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    action: str
    status: StepStatus = "pending"
    evidence_ids: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_category: str | None = None
    duration_ms: float = 0.0
    tool_calls: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "blocked", "skipped"}

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnalysisExecutionResult(BaseModel):
    """Structured outcome of one plan execution."""

    model_config = ConfigDict(extra="forbid")

    plan: AnalysisPlan
    steps: list[StepResult] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    answer: dict[str, Any] | None = None
    status: str = "pending"
    replan_reasons: list[str] = Field(default_factory=list)
    budgets: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    reused_steps: list[str] = Field(default_factory=list)
    recomputed_steps: list[str] = Field(default_factory=list)
    lease_conflicts: list[str] = Field(default_factory=list)
    reuse_denied: dict[str, str] = Field(default_factory=dict)
    terminal_outcome: str | None = None

    def step(self, step_id: str) -> StepResult | None:
        return next((item for item in self.steps if item.step_id == step_id), None)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnalysisExecutor:
    """Execute a validated plan, sharing one budget across parallel steps."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        budget_manager: BudgetManager | None = None,
        max_replans: int = 2,
        max_workers: int = 4,
        mode: str = "execute",
        tool_context_factory: Callable[[], ToolContext] | None = None,
        max_steps: int = 32,
        clock: Callable[[], float] = time.monotonic,
        journal: ExecutionJournal | None = None,
        worker_id: str = "worker",
        lease_ttl_seconds: float = 60.0,
        cancel_check: Callable[[], bool] | None = None,
        force_resume: bool = False,
        compile_spec: bool = True,
        evidence_layer: bool = True,
        span_recorder: SpanRecorder | None = None,
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must be zero or greater")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.registry = registry
        self.budget_manager = budget_manager or registry.budget_manager
        self.max_replans = max_replans
        self.max_workers = max_workers
        self.mode = mode
        self.tool_context_factory = tool_context_factory
        self.max_steps = max_steps
        self._clock = clock
        self.journal = journal
        self.worker_id = worker_id
        self.lease_ttl_seconds = max(float(lease_ttl_seconds), 1.0)
        self.cancel_check = cancel_check
        self.force_resume = force_resume
        # Step-16 ablation switches. ``compile_spec=False`` renders SQL through
        # the degraded template instead of the semantic compiler, and
        # ``evidence_layer=False`` skips the step-12 evidence-anchored answer;
        # both are defaults-on production behaviour.
        self.compile_spec = bool(compile_spec)
        self.evidence_layer = bool(evidence_layer)
        # Step 17 (section 八 item 3): the planned-analysis path reported no usage
        # and no latency at all, so ``/analyze`` had no cost a caller could
        # reconcile. The executor records spans on the run's existing
        # :class:`SpanRecorder` instead of growing a second telemetry system;
        # ``None`` keeps every existing caller (tests, benchmarks) unobserved.
        self.span_recorder = span_recorder

    # ---------------------------------------------------------- observability

    def _begin_span(
        self, name: str, kind: str, attributes: dict[str, Any] | None = None
    ) -> Span | None:
        """Start a span of this run, or return ``None`` when observed by nobody."""

        if self.span_recorder is None:
            return None
        return self.span_recorder.begin(name, kind, attributes=attributes)

    def _end_span(
        self,
        span: Span | None,
        *,
        status: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Finish a span, sanitizing late attributes exactly like ``begin`` does.

        ``attributes`` are known only *after* the observed work ran (row counts,
        error categories). They still go through
        :func:`~queryforge.core.observability.sanitize_attributes` so a
        payload-bearing value cannot enter the span through the back door.
        """

        if span is None or self.span_recorder is None:
            return
        if attributes:
            span.attributes.update(sanitize_attributes(attributes))
        self.span_recorder.end(span, status=status)

    def _budget_category(self, tool: str) -> str | None:
        """The declared budget category of ``tool`` (``None`` when undeclared)."""

        try:
            return str(self.registry.resolve(tool).budget_category)
        except Exception:  # pragma: no cover - an unknown tool is reported by the call
            return None

    @staticmethod
    def _tool_span_status(observation: ToolObservation) -> str:
        """Map one tool observation onto the span status vocabulary."""

        return "success" if observation.status == "succeeded" else "failed"

    def _execute_tool(
        self,
        tool: str,
        params: Mapping[str, Any],
        *,
        context: ToolContext,
        step: PlanStep,
    ) -> ToolObservation:
        """Call one registry tool and record its ``tool`` span.

        The span carries the cache/budget outcome of the call: ``cache`` is always
        ``miss`` because the planner's only reuse mechanism is journal step reuse,
        which returns before any tool is dispatched (a reused step publishes
        ``cache=hit`` on its step span and produces no tool span at all), and
        ``budget_outcome`` says whether the shared budget admitted the call.
        """

        span = self._begin_span(
            f"tool.{tool}",
            "tool",
            {
                "tool": tool,
                "action": step.action,
                "step_id": step.id,
                "mode": self.mode,
                "cache": "miss",
                "budget_category": self._budget_category(tool),
            },
        )
        try:
            observation = self.registry.execute(
                tool, params, context=context, mode=self.mode
            )
        except BaseException as exc:
            # ``execute`` returns typed observations, so this only fires on a
            # defect; the span must still be closed instead of vanishing.
            self._end_span(
                span,
                status="failed",
                attributes={"error_type": type(exc).__name__},
            )
            raise
        call = observation.call
        self._end_span(
            span,
            status=self._tool_span_status(observation),
            attributes={
                "tool_status": observation.status,
                "error_category": observation.error_category,
                "budget_outcome": (
                    "denied"
                    if observation.error_category == WorkflowErrorCategory.budget.value
                    else "allowed"
                ),
                "budget_remaining_tool_calls": self.budget_manager.remaining(
                    "max_tool_calls"
                ),
                "truncated": observation.truncated,
                "duration_ms": observation.duration_ms,
                "output_rows": getattr(call, "output_rows", None),
            },
        )
        return observation

    def _execute_sql(
        self,
        tool: str,
        sql: str,
        *,
        context: ToolContext,
        step: PlanStep,
        limit: int | None = None,
    ) -> ToolObservation:
        """Run one governed SQL tool with a ``sql`` span around it.

        The SQL text is never recorded: the span keeps its length and a short
        digest, so a statement (which may embed literals) can be correlated with
        the logs without being copied into the observability payload.
        """

        statement = sql if isinstance(sql, str) else str(sql)
        span = self._begin_span(
            "sql.execute",
            "sql",
            {
                "tool": tool,
                "step_id": step.id,
                "statement_chars": len(statement),
                "statement_digest": stable_digest(statement),
            },
        )
        params: dict[str, Any] = {"sql": statement}
        if limit is not None:
            params["limit"] = limit
        try:
            observation = self._execute_tool(tool, params, context=context, step=step)
        except BaseException as exc:
            self._end_span(
                span,
                status="failed",
                attributes={"error_type": type(exc).__name__},
            )
            raise
        payload = observation.result if isinstance(observation.result, dict) else {}
        row_count = payload.get("row_count")
        self._end_span(
            span,
            status=self._tool_span_status(observation),
            attributes={
                "tool_status": observation.status,
                "row_count": row_count if isinstance(row_count, int) else None,
                "truncated": observation.truncated,
            },
        )
        return observation

    # ----------------------------------------------------------------- execute

    def execute(
        self,
        plan: AnalysisPlan,
        *,
        tool_context: ToolContext | None = None,
        validate: bool = True,
    ) -> AnalysisExecutionResult:
        """Run ``plan`` to completion (or to a documented stop condition)."""

        if validate:
            PlanValidator.validate(plan, self.registry, self.mode)
        if len(plan.steps) > self.max_steps:
            raise PlanViolation(
                [f"too_many_steps: {len(plan.steps)} > {self.max_steps}"]
            )

        state = _ExecutionState(plan=plan, generated_by=self.budget_manager)
        if self.journal is not None:
            if self.journal.journal.terminal() and not self.force_resume:
                raise RunNotResumable(
                    f"run {self.journal.journal.run_id!r} already ended as "
                    f"{self.journal.journal.terminal_outcome!r}; refusing to revive it"
                )
            self.journal.expire_leases()
            self.journal.register_plan(plan)
            state.reuse_allowed, state.reuse_denied = self._reuse_plan(plan)
        plan.status = "running"
        base_context = self._base_context(tool_context)

        while True:
            if self._cancelled():
                state.stop_reason = "cancelled"
                break
            # Recomputed every round: a replan appends new steps to the plan.
            ordered = [step.id for step in PlanValidator.topological_order(plan)]
            ready = self._ready_steps(ordered, state)
            if not ready:
                break
            if state.stop_reason is not None:
                break
            self._run_batch(ready, state, base_context)
            if state.stop_reason is not None:
                break
            if state.clarification is not None:
                state.stop_reason = "needs_clarification"
                break
            if state.blocked_reason is not None:
                state.stop_reason = "blocked"
                break
            if state.budget_exhausted:
                state.stop_reason = "budget_exhausted"
                break
            if state.no_progress_repeat:
                state.stop_reason = "no_progress_repeat"
                break
            if not self._replan_failed_metric_steps(state):
                continue
            if not self._ready_steps(
                [item.id for item in PlanValidator.topological_order(plan)], state
            ):
                break

        self._finalize(plan, state)
        result = state.result(self.budget_manager)
        result.reused_steps = list(state.reused_steps)
        result.recomputed_steps = list(state.recomputed_steps)
        result.lease_conflicts = list(state.lease_conflicts)
        result.reuse_denied = dict(state.reuse_denied)
        if self.journal is not None:
            self.journal.record_budget(self.budget_manager.snapshot())
            outcome = self._terminal_outcome(result, state)
            self.journal.mark_terminal(outcome)
            result.terminal_outcome = outcome
        return result

    # ------------------------------------------------------- durable execution

    def _cancelled(self) -> bool:
        if self.cancel_check is None:
            return False
        try:
            return bool(self.cancel_check())
        except Exception:  # pragma: no cover - a broken cancel probe never cancels
            return False

    @staticmethod
    def _terminal_outcome(result: AnalysisExecutionResult, state: "_ExecutionState") -> str:
        if state.stop_reason == "cancelled":
            return "cancelled"
        mapping = {
            "succeeded": "success",
            "partial": "partial",
            "blocked": "blocked",
            "failed": "failed",
        }
        return mapping.get(result.status, "failed")

    def _reuse_step(
        self,
        step: PlanStep,
        step_id: str,
        context: ToolContext,
        state: "_ExecutionState",
        result: StepResult,
    ) -> bool:
        """Reuse a recorded success instead of executing the step again."""
        assert self.journal is not None
        record = self.journal.step(step_id)
        if record is None or not record.reusable():
            return False
        kind = EVIDENCE_KINDS.get(step.action, step.action)
        outputs = dict(record.outputs or {})
        if step.action not in LOCAL_ACTIONS:
            evidence_id = (
                record.evidence_ids[0] if record.evidence_ids else context.evidence_id(kind)
            )
            state.record_evidence(step, evidence_id, kind, outputs)
            result.evidence_ids = [evidence_id]
        else:
            result.evidence_ids = list(record.evidence_ids)
            if outputs.get("answer") is not None:
                state.answer = outputs.get("answer")
        result.outputs = outputs
        result.status = "succeeded"
        result.duration_ms = 0.0
        result.tool_calls = 0
        state.reused_steps.append(step_id)
        return True

    def _reuse_plan(
        self, plan: AnalysisPlan
    ) -> tuple[dict[str, bool], dict[str, str]]:
        """Decide which steps may reuse a recorded success.

        A step may be reused only when

        * it succeeded before and its input fingerprint still matches,
        * every upstream step is reusable too — once any upstream step must be
          recomputed (new inputs, new plan version, failure, uncertainty), the
          dependent work is invalidated and computed again, and
        * its action is still authorised *now*: a persisted success is not a
          standing permission. Authorisation and capability are re-checked
          against the registry this run built, so a policy or capability that
          was revoked between the crash and the resume forces a fresh attempt
          (which is then denied honestly) instead of replaying old evidence.

        Returns the allowed map plus, for every step that was refused, the
        reason — the reason is surfaced in the result so a resume is auditable.
        """
        allowed: dict[str, bool] = {}
        denied: dict[str, str] = {}
        assert self.journal is not None
        for step in PlanValidator.topological_order(plan):
            fingerprint = self.journal.fingerprint_step(
                step.action, dict(step.inputs or {}), plan_version=plan.version
            )
            blocker = self._reuse_blocker(step)
            if blocker is not None:
                allowed[step.id] = False
                denied[step.id] = blocker
                continue
            upstream_ok = all(
                allowed.get(dependency, False) for dependency in step.depends_on
            )
            record = self.journal.reusable_step(step.id, fingerprint)
            allowed[step.id] = bool(upstream_ok and record is not None)
            if record is not None and not upstream_ok:
                denied[step.id] = (
                    "upstream step must be recomputed; this step's inputs are "
                    "unproven"
                )
        return allowed, denied

    def _non_repeatable_blocker(self, step: PlanStep) -> str | None:
        """Refuse to blindly repeat a side effect with an unknown outcome (15-E1).

        A step that was interrupted mid-flight has ``outcome_certain=False``: the
        tool may or may not have applied its effect. For an action whose
        idempotency policy says ``safe_to_repeat=False`` (asset publication),
        running it again could double-apply the effect, so the run stops blocked
        and asks an operator to verify the external state. ``force_resume`` is the
        documented escape hatch once that verification happened.
        """
        if self.journal is None or self.force_resume:
            return None
        record = self.journal.step(step.id)
        if record is None or record.outcome_certain:
            return None
        policy = IDEMPOTENCY_POLICY.get(record.idempotency_class) or {}
        if policy.get("safe_to_repeat", True):
            return None
        max_attempts = int(policy.get("max_attempts", 1) or 1)
        if record.attempt < max_attempts:
            return None
        return (
            f"side_effect_not_repeatable: step {step.id!r} was interrupted after "
            f"{record.attempt} attempt(s) of {record.idempotency_class.value} with an "
            "unknown outcome; verify the external state before resuming "
            "(pass force_resume to confirm the verification)"
        )

    def _reuse_blocker(self, step: PlanStep) -> str | None:
        """Why this step must not be reused right now, or ``None`` when it may."""
        from queryforge.orchestration.planner.plan import ACTION_TOOL_MAP

        tool = ACTION_TOOL_MAP.get(step.action)
        if tool is None:
            # Local actions (e.g. ``compose_answer``) touch no governed tool.
            return None
        try:
            if not self.registry.is_available(tool):
                return f"tool {tool!r} is no longer implemented"
            if not self.registry.allows(tool, self.mode):
                return (
                    f"tool {tool!r} is not authorised in mode {self.mode!r} any more"
                )
        except Exception as exc:  # pragma: no cover - a broken registry never reuses
            return f"authorisation could not be verified: {exc}"
        return None

    # ------------------------------------------------------------------ batching

    def _ready_steps(self, ordered: Sequence[str], state: "_ExecutionState") -> list[str]:
        ready: list[str] = []
        for step_id in ordered:
            if state.results[step_id].status != "pending":
                continue
            step = state.plan.step(step_id)
            assert step is not None
            dependencies = [state.results[item] for item in step.depends_on]
            if any(not item.terminal for item in dependencies):
                continue
            failed = [item for item in dependencies if not item.succeeded]
            if failed:
                state.results[step_id].status = "skipped"
                state.results[step_id].error = (
                    "upstream_failure: " + ", ".join(item.step_id for item in failed)
                )
                state.results[step_id].error_category = (
                    failed[0].error_category or WorkflowErrorCategory.unknown.value
                )
                continue
            if self._is_repeat(step, state):
                state.results[step_id].status = "skipped"
                state.results[step_id].error = "no_progress_repeat: identical step already ran"
                state.results[step_id].error_category = WorkflowErrorCategory.budget.value
                state.no_progress_repeat = True
                continue
            ready.append(step_id)
        return ready

    def _run_batch(
        self,
        step_ids: Sequence[str],
        state: "_ExecutionState",
        base_context: ToolContext,
    ) -> None:
        if not step_ids:
            return
        if len(step_ids) == 1:
            self._execute_step(step_ids[0], state, base_context)
            return
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._execute_step, step_id, state, base_context): step_id
                for step_id in step_ids
            }
            for future in futures:
                future.result()

    # -------------------------------------------------------------- step runner

    def _execute_step(
        self, step_id: str, state: "_ExecutionState", base_context: ToolContext
    ) -> None:
        """Run one plan step and record its ``step`` span around the attempt.

        The span is opened here, outside the reuse/lease/journal bookkeeping, so a
        step that is reused or blocked is still visible with its real duration
        instead of disappearing from the run's timeline. It is named after the
        step id and is closed on every exit path (including a raised defect),
        which is what keeps the span count equal to the number of steps that were
        actually attempted.
        """

        step = state.plan.step(step_id)
        if step is None:  # pragma: no cover - defensive
            return
        span = self._begin_span(
            f"step.{step.id}",
            "step",
            {
                "action": step.action,
                "step_id": step.id,
                "plan_version": state.plan.version,
                "cache": "miss",
            },
        )
        try:
            self._run_step(step_id, state, base_context)
        finally:
            result = state.results[step_id]
            self._end_span(
                span,
                status=_STEP_SPAN_STATUS.get(result.status, "failed"),
                attributes={
                    "step_status": result.status,
                    "cache": "hit" if step_id in state.reused_steps else "miss",
                    "tool_calls": result.tool_calls,
                    "error_category": result.error_category,
                    "duration_ms": result.duration_ms,
                },
            )

    def _run_step(
        self, step_id: str, state: "_ExecutionState", base_context: ToolContext
    ) -> None:
        step = state.plan.step(step_id)
        if step is None:  # pragma: no cover - defensive
            return
        result = state.results[step_id]
        result.status = "running"
        started = self._clock()
        context = self._step_context(base_context, step, state)
        if self.journal is not None:
            fingerprint = self.journal.fingerprint_step(
                step.action, dict(step.inputs or {}), plan_version=state.plan.version
            )
            if state.reuse_allowed.get(step_id):
                reused = self._reuse_step(step, step_id, context, state, result)
                if reused:
                    return
            lease = self.journal.acquire_lease(
                step_id, owner=self.worker_id, ttl_seconds=self.lease_ttl_seconds
            )
            if lease is None:
                result.status = "blocked"
                result.error = "lease_held_by_another_worker"
                result.error_category = WorkflowErrorCategory.budget.value
                state.lease_conflicts.append(step_id)
                state.stop_reason = "lease_conflict"
                return
            if self.journal is not None:
                blocker = self._non_repeatable_blocker(step)
                if blocker is not None:
                    result.status = "blocked"
                    result.error = blocker
                    result.error_category = WorkflowErrorCategory.permission.value
                    state.blocked_reason = blocker
                    state.stop_reason = "blocked"
                    self.journal.record_failure(
                        step_id, error=blocker, status="blocked"
                    )
                    return
            state.recomputed_steps.append(step_id)
            self.journal.begin_attempt(
                step_id,
                action=step.action,
                fingerprint=fingerprint,
                budget=dict(step.budget or {}),
            )
        before_calls = len(self.registry.journal)
        try:
            outputs = self._dispatch(step, context, state, result)
        except ToolBudgetError as exc:
            self._fail(
                result,
                exc,
                WorkflowErrorCategory.budget.value,
                state,
                status="failed",
            )
            state.budget_exhausted = True
        except ToolDenied as exc:
            self._fail(result, exc, WorkflowErrorCategory.permission.value, state, status="blocked")
            state.blocked_reason = str(exc)
        except ToolUnavailable as exc:
            self._fail(
                result, exc, WorkflowErrorCategory.unsupported.value, state, status="failed"
            )
        except Exception as exc:  # every other failure is classified, never leaked
            category = (
                WorkflowErrorCategory.data_quality
                if isinstance(exc, DataAbsentError)
                else categorize_error(exc)
            )
            status = "blocked" if category is WorkflowErrorCategory.permission else "failed"
            self._fail(result, exc, category.value, state, status=status)
            if status == "blocked":
                state.blocked_reason = str(exc)
        else:
            result.outputs = outputs
            evidence_id = context.evidence_id(EVIDENCE_KINDS.get(step.action, step.action))
            if step.action not in LOCAL_ACTIONS:
                state.record_evidence(
                    step, evidence_id, EVIDENCE_KINDS.get(step.action, step.action), outputs
                )
                result.evidence_ids = [evidence_id]
            elif outputs.get("answer") is not None:
                result.evidence_ids = state.answer_evidence_ids(step, outputs)
            result.status = "succeeded"
            if self.journal is not None:
                self.journal.record_success(
                    step_id,
                    evidence_ids=list(result.evidence_ids),
                    outputs=dict(outputs or {}),
                )
        finally:
            result.duration_ms = round(max(0.0, (self._clock() - started) * 1000.0), 3)
            result.tool_calls = max(0, len(self.registry.journal) - before_calls)
            if self.journal is not None:
                self.journal.release_lease(step_id, owner=self.worker_id)

    def _fail(
        self,
        result: StepResult,
        error: Exception,
        category: str,
        state: "_ExecutionState",
        *,
        status: StepStatus,
    ) -> None:
        result.status = status
        result.error = str(error)
        result.error_category = category
        state.error_categories.append(category)
        if status == "failed" and category == WorkflowErrorCategory.data_quality.value:
            result.outputs["data_absent"] = True
        if self.journal is not None:
            journal_status = (
                "cancelled"
                if state.stop_reason == "cancelled"
                else "blocked"
                if status == "blocked"
                else "failed"
            )
            self.journal.record_failure(
                result.step_id,
                error=str(error),
                error_category=category,
                status=journal_status,
            )

    # ------------------------------------------------------------- dispatch

    def _dispatch(
        self,
        step: PlanStep,
        context: ToolContext,
        state: "_ExecutionState",
        result: StepResult,
    ) -> dict[str, Any]:
        action = step.action
        if action == "resolve_metric":
            return self._resolve_metric(step, context, state)
        if action == "check_data_quality":
            return self._check_data_quality(step, context, state)
        if action == "query_metric":
            return self._query_metric(step, context, state, result)
        if action == "compose_answer":
            return self._compose_answer(step, state)
        if action in STEP11_ACTIONS:
            return self._dispatch_computed(action, step, context, state)
        # Unknown/declared actions go through the registry unchanged.
        observation = self._execute_tool(
            self._tool_for(action), dict(step.inputs), context=context, step=step
        )
        if not observation.ok:
            raise self._error_for_observation(observation)
        return dict(observation.result or {})

    # ------------------------------------------------- step 11 value assembly

    def _dispatch_computed(
        self,
        action: str,
        step: PlanStep,
        context: ToolContext,
        state: "_ExecutionState",
    ) -> dict[str, Any]:
        """Assemble runtime values from governed results, then call the tool.

        The planner only declares knobs; every number handed to a step-11 tool
        is derived here from the governed ``query_metric`` evidence, so no
        value is invented at planning time.
        """
        tool = self._tool_for(action)
        # Declared-but-unimplemented tools must keep failing as `unsupported`
        # instead of being masked by a value-assembly error.
        try:
            implemented = bool(self.registry.is_available(tool))
        except Exception:  # pragma: no cover - defensive
            implemented = False
        if not implemented:
            observation = self._execute_tool(
                tool, dict(step.inputs), context=context, step=step
            )
            if not observation.ok:
                raise self._error_for_observation(observation)
            return dict(observation.result or {})
        params = self._assemble_step11(action, step, state)
        observation = self._execute_tool(tool, params, context=context, step=step)
        if not observation.ok:
            raise self._error_for_observation(observation)
        payload = dict(observation.result or {})
        payload.setdefault("method", payload.get("method"))
        payload.setdefault("parameters", {"assembled": True, **{k: v for k, v in params.items() if k not in {"series", "buckets", "rows"}}})
        return payload

    def _assemble_step11(
        self, action: str, step: PlanStep, state: "_ExecutionState"
    ) -> dict[str, Any]:
        knobs = dict(step.validation.get("knobs") or {})
        dataset = state.evidence_payload("metric_value") or {}
        resolution = state.evidence_payload("metric_resolution") or {}
        metric_kind = self._metric_kind(resolution)
        if action == "compare_periods":
            self._require_single_series_dimension(action, dataset)
            series = self._series(dataset)
            if len(series) < 2:
                raise DataAbsentError(
                    "data_absent: period comparison needs at least two ordered points"
                )
            # A single dimension is not enough: it must also be a *time*
            # dimension, otherwise "the last two points" are two categories
            # (observed: "Tablet -> Web") presented as a period comparison.
            self._require_ordered_time_series(
                action, [str(point["period"]) for point in series]
            )
            return {
                "current": series[-1]["value"],
                "baseline": series[-2]["value"],
                "label": f"{series[-2]['period']} -> {series[-1]['period']}",
                # Explicit values: schema validation may materialise omitted
                # optional parameters as None, which would override the tool's
                # own defaults.
                "method": str(knobs.get("method") or "absolute_relative"),
            }
        if action == "drill_down":
            buckets = self._buckets(dataset)
            if not buckets:
                raise DataAbsentError("data_absent: no categorical buckets to drill into")
            params: dict[str, Any] = {
                "buckets": buckets,
                "total": sum(bucket["value"] for bucket in buckets),
                "max_categories": int(knobs.get("max_categories") or 10),
            }
            if knobs.get("min_sample") is not None:
                params["min_sample"] = knobs["min_sample"]
            if knobs.get("dimension"):
                params["dimension"] = knobs["dimension"]
            return params
        if action == "calculate_contribution":
            contribution = self._contribution_buckets(dataset)
            if contribution is None:
                raise DataAbsentError(
                    "data_absent: contribution needs per-category values for two "
                    "comparable periods in one result"
                )
            return {
                "buckets": contribution["buckets"],
                "metric_kind": metric_kind,
                "expected_total_delta": contribution.get("expected_total_delta"),
                "additive": True,
            }
        if action == "detect_anomaly":
            self._require_single_series_dimension(action, dataset)
            series = self._series(dataset)
            if not series:
                raise DataAbsentError("data_absent: no ordered series to analyse")
            return {
                "series": series,
                "method": str(knobs.get("method") or "baseline_deviation"),
                "min_points": int(knobs.get("min_points") or 4),
                "seasonality": str(knobs.get("seasonality") or "none"),
                "missing": str(knobs.get("missing") or "skip"),
                "threshold": float(knobs.get("threshold") or 2.0),
            }
        if action == "render_chart":
            rows = list(dataset.get("rows") or [])
            columns = [str(column) for column in (dataset.get("columns") or [])]
            if not rows or not columns:
                raise DataAbsentError("data_absent: no result rows to chart")
            return {
                "rows": rows,
                "columns": columns,
                "metric_kind": metric_kind,
                "grain": knobs.get("time_grain"),
            }
        raise ToolUnavailable(
            f"plan action {action!r} has no value assembly", tool=action
        )

    @staticmethod
    def _metric_kind(resolution: dict[str, Any]) -> str:
        aggregation = str(resolution.get("aggregation") or "sum").casefold()
        if aggregation in {"ratio", "average", "avg"}:
            return "ratio"
        if aggregation in {"count_distinct", "distinct"}:
            return "distinct"
        return "additive"

    @classmethod
    def _require_single_series_dimension(
        cls, action: str, dataset: dict[str, Any]
    ) -> None:
        """Refuse to build an ordered series from a multi-dimension result.

        ``_series`` collapses any result to ``(label, value)`` points by taking the
        first non-numeric column as the label. For a result grouped by month *and*
        device that produces 60 points in which every month appears once per device,
        so "the last two points" are two rows of the SAME period — a comparison of
        two categories presented as a period comparison (observed label:
        ``"September -> September"``). Series-based actions therefore require a
        single ordered dimension and fail honestly otherwise.

        A single dimension is necessary but not sufficient for a *period*
        comparison: :meth:`_require_ordered_time_series` additionally proves the
        points are calendar periods in time order, which a categorical dimension
        (devices, regions) cannot satisfy.
        """
        dimensions = [
            str(item) for item in (dataset.get("dimensions") or []) if str(item).strip()
        ]
        if len(dimensions) > 1:
            raise UnsupportedAnalysisError(
                f"unsupported_grain: {action} needs a single ordered dimension, "
                f"but the metric result is grouped by {dimensions}"
            )

    @classmethod
    def _require_ordered_time_series(
        cls, action: str, labels: Sequence[str]
    ) -> None:
        """Refuse a period comparison whose points are not ordered periods.

        The series labels are the evidence that the single dimension really is a
        time dimension, so every label must name a calendar period *and* the
        sequence must advance in time. Two shapes are refused instead of turned
        into a result:

        * a categorical series (observed label ``"Tablet -> Web"``): the
          "periods" are two categories of one period, so a category gap would be
          reported as a change over time;
        * a series that goes backwards or repeats a period (a descending result,
          or month names left in alphabetical order): the last two points are not
          the latest two periods, and a descending series would silently swap
          current and baseline.

        Only ascending order is accepted. A skipped period is allowed (a month
        with no rows is simply absent from the result), and a December -> January
        step is allowed for labels that carry no year. An ascending subsequence
        is accepted because those two points really are ordered periods; the
        ambiguous case the ordering check exists for — a categorical or
        alphabetically re-ordered series — cannot pass it.
        """

        positions = [_calendar_position(label) for label in labels]
        if any(position is None for position in positions):
            unlabelled = [
                str(label)
                for label, position in zip(labels, positions)
                if position is None
            ]
            raise UnsupportedAnalysisError(
                f"unsupported_grain: {action} needs a single ordered time "
                "dimension, but the series labels are not calendar periods: "
                f"{unlabelled[:4]}"
            )
        for previous, current in zip(positions, positions[1:]):
            if previous is None or current is None:  # pragma: no cover - checked above
                continue
            if current > previous:
                continue
            wrapped_month = (
                previous[0] == 0
                and current[0] == 0
                and current[1] == previous[1] % 12 + 1
            )
            if wrapped_month:
                continue
            raise UnsupportedAnalysisError(
                f"unsupported_grain: {action} needs a single ordered time "
                f"dimension, but the series is not in time order "
                f"({labels[0]!r} ... {labels[-1]!r})"
            )

    @classmethod
    def _series(cls, dataset: dict[str, Any]) -> list[dict[str, Any]]:
        """Ordered (period, value) points from a governed metric result."""
        columns = [str(column) for column in (dataset.get("columns") or [])]
        rows = list(dataset.get("rows") or [])
        if len(columns) < 2:
            return []
        value_index = cls._numeric_index(columns, rows)
        if value_index is None:
            return []
        label_index = next(
            (index for index in range(len(columns)) if index != value_index), 0
        )
        series: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) <= value_index:
                continue
            value = row[value_index]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            series.append(
                {"period": str(row[label_index]), "value": value}
            )
        return series

    @classmethod
    def _buckets(cls, dataset: dict[str, Any]) -> list[dict[str, Any]]:
        series = cls._series(dataset)
        return [
            {"category": point["period"], "value": point["value"]}
            for point in series
            if point["value"] is not None
        ]

    @classmethod
    def _contribution_buckets(
        cls, dataset: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Per-category current/baseline pairs when the result carries both."""
        columns = [str(column) for column in (dataset.get("columns") or [])]
        rows = list(dataset.get("rows") or [])
        if len(columns) < 3 or not rows:
            return None
        numeric = [
            index
            for index in range(len(columns))
            if all(
                isinstance(row[index], (int, float)) and not isinstance(row[index], bool)
                for row in rows
                if isinstance(row, (list, tuple)) and len(row) > index
            )
        ]
        if len(numeric) < 2:
            return None
        baseline_index, current_index = numeric[0], numeric[1]
        label_index = next(
            index for index in range(len(columns)) if index not in numeric
        )
        buckets = [
            {
                "category": str(row[label_index]),
                "current": row[current_index],
                "baseline": row[baseline_index],
            }
            for row in rows
            if isinstance(row, (list, tuple)) and len(row) > max(numeric)
        ]
        if not buckets:
            return None
        return {
            "buckets": buckets,
            "expected_total_delta": sum(
                bucket["current"] - bucket["baseline"] for bucket in buckets
            ),
        }

    @staticmethod
    def _numeric_index(
        columns: list[str], rows: list[list[Any]]
    ) -> int | None:
        for index in range(len(columns)):
            values = [
                row[index]
                for row in rows
                if isinstance(row, (list, tuple)) and len(row) > index
            ]
            if values and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                return index
        return None

    def _tool_for(self, action: str) -> str:
        from queryforge.orchestration.planner.plan import ACTION_TOOL_MAP

        tool = ACTION_TOOL_MAP.get(action)
        if tool is None:
            raise ToolUnavailable(
                f"plan action {action!r} has no tool binding",
                tool=action,
                reason="not_implemented",
            )
        return tool

    @staticmethod
    def _error_for_observation(observation: ToolObservation) -> Exception:
        message = (observation.call.error if observation.call else None) or (
            f"tool {observation.tool!r} failed"
        )
        category = observation.error_category
        if category == WorkflowErrorCategory.unsupported.value:
            return ToolUnavailable(message, tool=observation.tool, reason="not_implemented")
        if category == WorkflowErrorCategory.permission.value:
            return ToolDenied(message, tool=observation.tool)
        if category == WorkflowErrorCategory.budget.value:
            return ToolBudgetError(message)
        if observation.status == "timeout":
            return TimeoutError(message)
        return ValueError(message)

    # ----------------------------------------------------------- action handlers

    def _resolve_metric(
        self, step: PlanStep, context: ToolContext, state: "_ExecutionState"
    ) -> dict[str, Any]:
        model_context = context.semantic_model
        if model_context is None:
            raise ToolUnavailable(
                "resolve_metric needs a governed semantic model",
                tool="resolve_metric",
                reason="semantic_model_unavailable",
            )
        term = str(step.inputs.get("term") or "").strip()
        question = str(step.inputs.get("question") or context.question or "").strip()
        matches = SemanticModelLoader.match_metrics(model_context.model, term or question)
        requested = [str(item) for item in (step.inputs.get("dimensions") or [])]
        if not matches:
            state.clarification = (
                f"No governed metric matches {term or question!r}; confirm the "
                "intended metric definition before running business SQL."
            )
            raise ValueError(f"needs_clarification: no governed metric matches {term or question!r}")
        entity_names = {match.metric.name for match in matches}
        if len(entity_names) > 1 and not term:
            state.clarification = (
                f"Multiple governed metrics match the question: {sorted(entity_names)}; "
                "confirm which one is intended."
            )
            raise ValueError("needs_clarification: ambiguous metric resolution")
        chosen = matches[0]
        metric = chosen.metric
        entities = {entity.name: entity for entity in model_context.model.entities}
        entity = entities.get(metric.entity)
        dimensions = [
            f"{metric.entity}.{dimension.name}"
            for dimension in (entity.dimensions if entity else [])
        ]
        legal = [item for item in (metric.allowed_dimensions or []) if item in dimensions] or dimensions
        return {
            "metric": metric.name,
            "aggregation": metric.aggregation,
            "entity": metric.entity,
            "table": entity.table if entity else None,
            "expression": metric.expression,
            "time_field": metric.time_field,
            "declared_dimensions": dimensions,
            "legal_dimensions": legal,
            "requested_dimensions": requested,
            "matched_term": chosen.matched_term,
            "semantic_version": model_context.model.version,
            "candidates": sorted(entity_names),
        }

    def _check_data_quality(
        self, step: PlanStep, context: ToolContext, state: "_ExecutionState"
    ) -> dict[str, Any]:
        params = dict(step.inputs)
        resolution = state.evidence_payload("metric_resolution")
        if not params.get("table_name") and resolution:
            params["table_name"] = resolution.get("table")
        observation = self._execute_tool(
            "check_data_quality", params, context=context, step=step
        )
        if not observation.ok:
            raise self._error_for_observation(observation)
        payload = dict(observation.result or {})
        if payload.get("status") == "error" or payload.get("blocking"):
            reason = ", ".join(
                f"{item.get('table')}.{item.get('check')}:{item.get('reason')}"
                for item in payload.get("errors", [])
            ) or "blocking quality failure"
            state.blocked_reason = f"data_quality_blocked: {reason}"
            raise ValueError(f"data_quality_blocked: {reason}")
        return payload

    def _query_metric(
        self,
        step: PlanStep,
        context: ToolContext,
        state: "_ExecutionState",
        result: StepResult,
    ) -> dict[str, Any]:
        resolution = state.evidence_payload("metric_resolution") or {}
        metric_name = step.inputs.get("metric") or resolution.get("metric")
        dimensions = [
            str(item) for item in (step.inputs.get("dimensions") or [])
        ]
        limit = int(step.inputs.get("limit") or 100)
        explicit_sql = step.inputs.get("sql")
        spec: QuerySpec | None = None
        if explicit_sql:
            sql = str(explicit_sql)
            degraded, degraded_reason = False, None
        else:
            spec = self._compile_spec(context, str(metric_name or ""), dimensions, limit, resolution)
            if spec is not None:
                sql = spec.sql
                degraded, degraded_reason = False, None
            elif dimensions:
                # A breakdown was requested and no governed query could be
                # compiled for it: a raw preview would answer a different
                # question, so the step fails instead of reporting success.
                raise UnsupportedAnalysisError(
                    "unsupported_dimension: no governed query could be compiled "
                    f"for metric {metric_name!r} by {', '.join(dimensions)}"
                )
            else:
                return self._query_metric_fallback(step, context, state, result, resolution)

        observation = self._execute_sql("execute_sql", sql, context=context, step=step)
        if not observation.ok:
            if observation.error_category == WorkflowErrorCategory.budget.value:
                raise ToolBudgetError(observation.call.error or "budget exhausted")
            raise self._error_for_observation(observation)
        payload = dict(observation.result or {})
        rows = list(payload.get("rows") or [])
        columns = list(payload.get("columns") or [])
        if not rows:
            suggestions = state.note_missing_dimension(
                metric_name, dimensions, resolution
            )
            raise DataAbsentError(
                _with_alternative_dimensions(
                    f"data_absent: metric {metric_name!r} returned no rows for "
                    f"dimensions {dimensions or ['<none>']}",
                    suggestions,
                )
            )
        value = None
        if len(rows) == 1 and len(columns) == 1:
            value = rows[0][0]
            if value is None:
                # A scalar aggregate is NULL when the requested scope has no rows
                # ("watch hours in Q1 1990"). Reporting that as a successful answer
                # with a null value hides an empty slice, so it is treated as data
                # absence: the plan ends partial with an explicit gap.
                suggestions = state.note_missing_dimension(
                    metric_name, dimensions, resolution
                )
                raise DataAbsentError(
                    _with_alternative_dimensions(
                        f"data_absent: metric {metric_name!r} has no value for the "
                        "requested scope",
                        suggestions,
                    )
                )
        prepared: dict[str, Any] = {
            "metric": metric_name,
            "aggregation": (spec.aggregation if spec else resolution.get("aggregation")),
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": int(payload.get("row_count") or len(rows)),
            "value": value,
            "dimensions": dimensions,
            "degraded": degraded,
            "semantic_version": resolution.get("semantic_version"),
            "query_spec": (
                {
                    "metric": spec.metric_name,
                    "aggregation": spec.aggregation,
                    "base_table": spec.base_table,
                    "group_by": list(spec.group_by),
                }
                if spec
                else None
            ),
        }
        if degraded and degraded_reason:
            prepared["degraded_reason"] = degraded_reason
        return prepared

    def _query_metric_fallback(
        self,
        step: PlanStep,
        context: ToolContext,
        state: "_ExecutionState",
        result: StepResult,
        resolution: dict[str, Any],
    ) -> dict[str, Any]:
        """Governed preview path used when no QuerySpec can be compiled."""

        table = step.inputs.get("table_name") or resolution.get("table")
        fallback_sql = step.inputs.get("fallback_sql") or (
            f'SELECT * FROM "{table}" LIMIT 5' if table else None
        )
        if not fallback_sql:
            raise ValueError(
                "query_metric requires a compilable metric or an explicit SQL statement"
            )
        observation = self._execute_sql(
            "preview_sql", str(fallback_sql), context=context, step=step, limit=5
        )
        if not observation.ok:
            raise self._error_for_observation(observation)
        payload = dict(observation.result or {})
        rows = list(payload.get("rows") or [])
        if not rows:
            suggestions = state.note_missing_dimension(
                step.inputs.get("metric") or resolution.get("metric"),
                [str(item) for item in (step.inputs.get("dimensions") or [])],
                resolution,
            )
            raise DataAbsentError(
                _with_alternative_dimensions(
                    "data_absent: governed preview returned no rows", suggestions
                )
            )
        return {
            "metric": step.inputs.get("metric") or resolution.get("metric"),
            "aggregation": resolution.get("aggregation"),
            "sql": str(fallback_sql),
            "columns": list(payload.get("columns") or []),
            "rows": rows,
            "row_count": int(payload.get("row_count") or len(rows)),
            "value": None,
            "dimensions": [str(item) for item in (step.inputs.get("dimensions") or [])],
            "degraded": True,
            "degraded_reason": "query_spec_unavailable: evidence came from a bounded preview",
            "semantic_version": resolution.get("semantic_version"),
            "query_spec": None,
        }

    def _compile_spec(
        self,
        context: ToolContext,
        metric_name: str,
        dimensions: list[str],
        limit: int,
        resolution: dict[str, Any],
    ) -> QuerySpec | None:
        """Compile the governed metric SQL, resolving dimension join paths.

        The compiler can only group by a dimension that lives on the metric's own
        entity unless it is handed the governed join path; without one it returns
        ``None`` and the old code fell back to a raw preview, which then counted as
        ``metric_value`` evidence. Resolving the paths here (exactly as
        ``metric_search_node`` does on the workflow path) is what makes a grouped
        question answerable at all, and an unresolvable dimension is refused
        instead of silently dropped.
        """
        model_context = context.semantic_model
        if not self.compile_spec:
            # Ablation (step 16): the semantic compiler is disabled, so the
            # governed metric SQL cannot be rendered and the degraded template
            # path is taken instead.
            return None
        if model_context is None or not metric_name:
            return None
        metric = next(
            (item for item in model_context.model.metrics if item.name == metric_name), None
        )
        if metric is None:
            return None
        paths, problems = self._dimension_join_paths(
            model_context, metric.entity, dimensions
        )
        if problems:
            raise UnsupportedAnalysisError("; ".join(problems))
        return QuerySpecCompiler.compile(
            semantic_model=model_context,
            metric_matches=[MetricMatch(matched_term=metric_name, metric=metric)],
            metric_join_paths=paths or None,
            requested_dimensions=dimensions,
            date_context=getattr(context, "date_context", None),
            limit=limit,
        )

    def _dimension_join_paths(
        self,
        model_context: Any,
        metric_entity: str | None,
        dimensions: Sequence[str],
    ) -> tuple[list[Any], list[str]]:
        """Governed join paths for ``dimensions``, plus every reason one is refused.

        Mirrors the workflow path's rules: the dimension must be declared, a
        declared join path must exist, and the traversal must not fan out the
        metric's grain. Anything else is reported so the step can fail honestly.
        """
        model = getattr(model_context, "model", None)
        if model is None:
            return [], []
        paths: list[Any] = []
        problems: list[str] = []
        for name in dimensions:
            entity = _entity_of_dimension(model, str(name))
            if entity is None:
                problems.append(
                    f"unsupported_dimension: {name!r} is not a declared dimension"
                )
                continue
            if not metric_entity or entity.name == metric_entity:
                continue
            resolved = SemanticModelLoader.resolve_join_path(
                model, metric_entity, entity.name
            )
            if resolved is None:
                problems.append(
                    f"unsupported_dimension: no governed join path from "
                    f"{metric_entity!r} to {entity.name!r} for {name!r}"
                )
                continue
            if not resolved.safe:
                problems.append(
                    f"fanout_risk: dimension {name!r} would fan out the metric grain: "
                    + "; ".join(resolved.fanout_steps)
                )
                continue
            if all(existing.name != resolved.name for existing in paths):
                paths.append(resolved)
        return paths, problems

    def _compose_answer(self, step: PlanStep, state: "_ExecutionState") -> dict[str, Any]:
        required = list(state.plan.expected_evidence())
        for extra in step.inputs.get("require_evidence") or step.validation.get(
            "require_evidence"
        ) or []:
            if str(extra) not in required:
                required.append(str(extra))
        missing = [kind for kind in required if kind not in state.evidence_by_kind]
        if missing:
            raise ValueError(
                "missing_evidence: " + ", ".join(sorted(missing))
            )
        for kind in step.inputs.get("require_outputs") or step.validation.get(
            "require_outputs"
        ) or []:
            if not state.evidence_payload(str(kind)):
                raise ValueError(f"missing_output: {kind}")
        metric_payload = state.evidence_payload("metric_value")
        resolution = state.evidence_payload("metric_resolution") or {}
        quality = state.evidence_payload("data_quality") or {}
        findings: list[dict[str, Any]] = []
        if metric_payload:
            findings.append(
                {
                    "kind": "metric",
                    "metric": metric_payload.get("metric"),
                    "value": metric_payload.get("value"),
                    "rows": metric_payload.get("rows"),
                    "dimensions": metric_payload.get("dimensions"),
                    "degraded": bool(metric_payload.get("degraded")),
                }
            )
        if quality:
            findings.append({"kind": "data_quality", "status": quality.get("status")})
        if resolution:
            findings.append(
                {
                    "kind": "semantic",
                    "metric": resolution.get("metric"),
                    "aggregation": resolution.get("aggregation"),
                    "semantic_version": resolution.get("semantic_version"),
                }
            )
        answer = {
            "question": state.plan.question,
            "findings": findings,
            "metric": metric_payload.get("metric") if metric_payload else None,
            "value": metric_payload.get("value") if metric_payload else None,
            "rows": metric_payload.get("rows") if metric_payload else None,
            "evidence_ids": state.evidence_ids_for(required),
            "limitations": self._limitations(metric_payload, quality),
            "degraded": bool(metric_payload and metric_payload.get("degraded")),
        }
        state.answer = answer
        payload: dict[str, Any] = {"answer": answer, "required_evidence": required}
        evidence_layer = (
            self._final_answer(step, state, answer, required)
            if self.evidence_layer
            else None
        )
        if evidence_layer is not None:
            payload.update(evidence_layer)
        return payload

    def _final_answer(
        self,
        step: PlanStep,
        state: "_ExecutionState",
        answer: dict[str, Any],
        required: list[str],
    ) -> dict[str, Any] | None:
        """Build the step-12 evidence-anchored answer alongside the legacy one.

        Numbers are resolved by the composer from the referenced evidence
        payloads, so the answer cannot invent a value; validation problems mark
        the answer ``review_required`` instead of being dropped.
        """
        try:
            from queryforge.domain.analysis.evidence import (
                AnswerComposer,
                Evidence,
                EvidenceStore,
                Finding,
                apply_validation,
                validate_answer,
            )
        except Exception:  # pragma: no cover - evidence layer is optional
            return None

        store = EvidenceStore()
        for entry in state.evidence:
            evidence_id = str(entry.get("evidence_id"))
            if not evidence_id:
                continue
            try:
                store.add(
                    Evidence(
                        id=evidence_id,
                        kind=str(entry.get("kind") or "unknown"),
                        source=str(entry.get("step_id") or entry.get("producer") or "plan"),
                        method=(
                            (entry.get("payload") or {}).get("method")
                            if isinstance(entry.get("payload"), dict)
                            else None
                        ),
                        validation=(
                            {"status": (entry.get("payload") or {}).get("status")}
                            if isinstance(entry.get("payload"), dict)
                            and (entry.get("payload") or {}).get("status")
                            else None
                        ),
                        completeness=self._evidence_completeness(entry),
                        payload=entry.get("payload") or {},
                    )
                )
            except Exception:
                continue

        findings: list[Finding] = []
        metric_payload = state.evidence_payload("metric_value") or {}
        metric_ids = state.evidence_ids_for(["metric_value"])
        if metric_payload and metric_ids:
            findings.append(
                Finding(
                    kind="metric",
                    statement=(
                        f"{metric_payload.get('metric')} over "
                        f"{', '.join(metric_payload.get('dimensions') or []) or 'the full set'}"
                    ),
                    numbers={},
                    dimensions=[str(item) for item in metric_payload.get("dimensions") or []],
                    evidence_ids=metric_ids,
                    degraded=bool(metric_payload.get("degraded")),
                )
            )
        quality_payload = state.evidence_payload("data_quality") or {}
        quality_ids = state.evidence_ids_for(["data_quality"])
        if quality_payload and quality_ids:
            findings.append(
                Finding(
                    kind="data_quality",
                    statement=f"data quality checks: {quality_payload.get('status', 'unknown')}",
                    numbers={},
                    evidence_ids=quality_ids,
                    review_required=str(quality_payload.get("status")) == "warning",
                )
            )
        step11_findings = (
            ("period_comparison", "period_comparison", "period-over-period comparison", {"delta": None, "relative_change": None}),
            ("drill_down", "drill_down", "dimension drill-down", {}),
            ("contribution", "contribution", "contribution breakdown", {"total_delta": None, "residual": None}),
            ("anomaly", "anomaly", "anomaly detection", {}),
        )
        for kind, evidence_kind, label, numbers in step11_findings:
            evidence_ids = state.evidence_ids_for([evidence_kind])
            payload = state.evidence_payload(evidence_kind)
            if not payload or not evidence_ids:
                continue
            findings.append(
                Finding(
                    kind=kind,
                    statement=label,
                    numbers=dict(numbers),
                    evidence_ids=evidence_ids,
                    degraded=bool(payload.get("degraded")),
                )
            )

        charts: list[dict[str, Any]] = []
        chart_ids = state.evidence_ids_for(["chart"])
        chart_payload = state.evidence_payload("chart")
        if chart_payload and chart_ids:
            charts.append(
                {
                    "chart_type": chart_payload.get("chart_type"),
                    "spec": chart_payload.get("spec"),
                    "reason": chart_payload.get("reason"),
                    "evidence_ids": chart_ids,
                }
            )

        gaps = [
            kind
            for kind in required
            if kind not in state.evidence_by_kind
        ]
        status = "success"
        if answer.get("degraded") or state.missing_evidence:
            status = "partial"
        if state.blocked_reason:
            status = "blocked"
        try:
            composer = AnswerComposer(store)
            final_answer = composer.compose(
                state.plan.question,
                findings,
                status=status,
                charts=charts,
                limitations=list(answer.get("limitations") or []),
                gaps=gaps,
                degraded=bool(answer.get("degraded")),
            )
            problems = validate_answer(final_answer, store)
            if problems:
                final_answer = apply_validation(final_answer, problems)
        except Exception as exc:  # never fail the run because of the answer layer
            return {
                "final_answer_error": str(exc),
            }
        return {
            "final_answer": final_answer.model_dump(mode="json"),
            "evidence": store.to_list(),
            "validation_problems": validate_answer(final_answer, store),
        }

    @staticmethod
    def _evidence_completeness(entry: dict[str, Any]) -> str | None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("truncated"):
            return "truncated"
        if payload.get("degraded"):
            return "degraded"
        return "complete"

    @staticmethod
    def _limitations(
        metric_payload: dict[str, Any] | None, quality: dict[str, Any] | None
    ) -> list[str]:
        limitations: list[str] = []
        if metric_payload is None:
            limitations.append("no metric evidence was produced")
        elif metric_payload.get("degraded"):
            limitations.append(
                "metric evidence is degraded: "
                + str(metric_payload.get("degraded_reason") or "bounded preview")
            )
        if quality:
            counts = quality.get("counts") or {}
            if counts.get("warning"):
                limitations.append("data quality reported warnings")
            if counts.get("unknown"):
                limitations.append("some data quality checks could not run (unknown)")
        elif metric_payload is not None:
            limitations.append("no data quality evidence was collected")
        return limitations

    # -------------------------------------------------------------- replanning

    def _replan_failed_metric_steps(self, state: "_ExecutionState") -> bool:
        """Swap in a legal alternative dimension for data-absent metric steps."""

        if state.replans >= self.max_replans:
            return False
        for step_id in list(state.results):
            result = state.results[step_id]
            step = state.plan.step(step_id)
            if step is None or step.action != "query_metric" or result.status != "failed":
                continue
            if not result.outputs.get("data_absent"):
                continue
            resolution = state.evidence_payload("metric_resolution") or {}
            metric = step.inputs.get("metric") or resolution.get("metric")
            tried = [str(item) for item in (step.inputs.get("dimensions") or [])]
            candidates = [
                item
                for item in (state.alternative_dimensions(metric, resolution) or [])
                if item not in tried
            ]
            if not candidates:
                continue
            alternative = candidates[0]
            capability = (str(metric), alternative)
            if capability in state.replanned_capabilities:
                continue
            self._append_replan(state, step, alternative, tried, result)
            return True
        return False

    def _append_replan(
        self,
        state: "_ExecutionState",
        step: PlanStep,
        alternative: str,
        tried: list[str],
        result: StepResult,
    ) -> None:
        state.replans += 1
        new_id = f"{step.id}~r{state.replans}"
        new_inputs = dict(step.inputs)
        new_inputs["dimensions"] = [alternative]
        new_step = PlanStep(
            id=new_id,
            action="query_metric",
            inputs=new_inputs,
            depends_on=[
                dependency for dependency in step.depends_on if dependency != step.id
            ],
            expected_evidence=list(step.expected_evidence),
            validation=dict(step.validation),
            budget=dict(step.budget),
        )
        state.plan.steps.append(new_step)
        state.plan.version += 1
        state.results[new_id] = StepResult(step_id=new_id, action="query_metric")
        # The replacement step takes the failed step's place: dependents that
        # were skipped because of the failure are rewired onto it and retried.
        for other in state.plan.steps:
            if other.id == new_id or step.id not in other.depends_on:
                continue
            other.depends_on = [
                new_id if dependency == step.id else dependency
                for dependency in other.depends_on
            ]
            dependent = state.results.get(other.id)
            if (
                dependent is not None
                and dependent.status == "skipped"
                and "upstream_failure" in (dependent.error or "")
            ):
                dependent.status = "pending"
                dependent.error = None
                dependent.error_category = None
        state.replanned_capabilities.add(
            (str(step.inputs.get("metric") or ""), alternative)
        )
        reason = (
            f"replan:{step.id} returned no data for dimensions {tried or ['<none>']}; "
            f"retried with declared alternative dimension {alternative!r} "
            f"(step {new_id}, plan version {state.plan.version})"
        )
        state.replan_reasons.append(reason)
        result.outputs = {**result.outputs, "replanned_as": new_id, "reason": reason}

    # ----------------------------------------------------------------- finalize

    def _finalize(self, plan: AnalysisPlan, state: "_ExecutionState") -> None:
        missing = [
            kind for kind in plan.expected_evidence() if kind not in state.evidence_by_kind
        ]
        compose = next(
            (item for item in state.results.values() if item.action == "compose_answer"), None
        )
        if state.clarification is not None:
            plan.status = "needs_clarification"
        elif state.blocked_reason is not None:
            plan.status = "blocked"
        elif compose is not None and compose.succeeded and not missing and state.answer is not None:
            plan.status = "succeeded"
        elif state.evidence_by_kind:
            plan.status = "partial"
        else:
            plan.status = "failed"
        state.missing_evidence = missing

    # ---------------------------------------------------------------- contexts

    def _base_context(self, tool_context: ToolContext | None) -> ToolContext:
        if tool_context is not None:
            return tool_context
        if self.tool_context_factory is not None:
            return self.tool_context_factory()
        return ToolContext()

    def _step_context(
        self, base: ToolContext, step: PlanStep, state: "_ExecutionState"
    ) -> ToolContext:
        """A per-step context; a factory call keeps DB handles thread-local."""

        context = self.tool_context_factory() if self.tool_context_factory else base
        context.evidence_prefix = f"{state.plan.plan_id}:{step.id}"
        return context

    def _is_repeat(self, step: PlanStep, state: "_ExecutionState") -> bool:
        signature = (step.action, _canonical(step.inputs))
        for existing_id, result in state.results.items():
            if existing_id == step.id or not result.succeeded:
                continue
            previous = state.plan.step(existing_id)
            if previous is None:
                continue
            if (previous.action, _canonical(previous.inputs)) == signature:
                return True
        return False


def _canonical(payload: Any) -> str:
    import json

    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(payload)


class _ExecutionState:
    """Mutable bookkeeping for one plan execution."""

    def __init__(self, plan: AnalysisPlan, generated_by: BudgetManager) -> None:
        self.plan = plan
        self.results: dict[str, StepResult] = {
            step.id: StepResult(step_id=step.id, action=step.action) for step in plan.steps
        }
        self.evidence: list[dict[str, Any]] = []
        self.evidence_by_kind: dict[str, str] = {}
        self.evidence_payloads: dict[str, dict[str, Any]] = {}
        self.answer: dict[str, Any] | None = None
        self.replan_reasons: list[str] = []
        self.error_categories: list[str] = []
        self.replans = 0
        self.max_replans = 2
        self.replanned_capabilities: set[tuple[str, str]] = set()
        self.missing_evidence: list[str] = []
        self.clarification: str | None = None
        self.blocked_reason: str | None = None
        self.stop_reason: str | None = None
        self.budget_exhausted = False
        self.no_progress_repeat = False
        self.budget_manager = generated_by
        # step 15: reuse decisions computed from the durable journal
        self.reuse_allowed: dict[str, bool] = {}
        self.reuse_denied: dict[str, str] = {}
        self.reused_steps: list[str] = []
        self.recomputed_steps: list[str] = []
        self.lease_conflicts: list[str] = []

    # --------------------------------------------------------------- evidence

    def record_evidence(
        self,
        step: PlanStep,
        evidence_id: str,
        kind: str,
        outputs: dict[str, Any],
    ) -> None:
        entry = {
            "evidence_id": evidence_id,
            "kind": kind,
            "step_id": step.id,
            "action": step.action,
            "domain_id": self.plan.domain_id,
            "payload": outputs,
        }
        self.evidence.append(entry)
        self.evidence_by_kind[kind] = evidence_id
        self.evidence_payloads[kind] = outputs

    def answer_evidence_ids(self, step: PlanStep, outputs: dict[str, Any]) -> list[str]:
        required = outputs.get("required_evidence") or []
        return self.evidence_ids_for([str(item) for item in required])

    def evidence_ids_for(self, kinds: Sequence[str]) -> list[str]:
        return [
            self.evidence_by_kind[kind] for kind in kinds if kind in self.evidence_by_kind
        ]

    def evidence_payload(self, kind: str) -> dict[str, Any] | None:
        return self.evidence_payloads.get(kind)

    def alternative_dimensions(
        self, metric: Any, resolution: dict[str, Any]
    ) -> list[str]:
        legal = [str(item) for item in (resolution.get("legal_dimensions") or [])]
        if not legal:
            legal = [str(item) for item in (resolution.get("declared_dimensions") or [])]
        return sorted(dict.fromkeys(legal))

    def note_missing_dimension(
        self, metric: Any, dimensions: Sequence[str], resolution: dict[str, Any]
    ) -> list[str]:
        """Return the legal dimensions that were not tried for this metric.

        The data-absence path raises immediately afterwards, so the suggestion
        travels on the error message (which becomes the step result, the journal
        entry and the plan limitation) instead of a state attribute nobody read:
        keeping it in state silently dropped the one actionable hint the caller
        could act on.
        """

        tried = set(dimensions)
        return [
            item
            for item in self.alternative_dimensions(metric, resolution)
            if item not in tried
        ]

    # ----------------------------------------------------------------- result

    def result(self, budget_manager: BudgetManager) -> AnalysisExecutionResult:
        # Reported in dependency order so a plan report shows the order the
        # executor actually had to respect (replanned steps included).
        ordered = [
            self.results[step.id]
            for step in PlanValidator.topological_order(self.plan)
            if step.id in self.results
        ]
        return AnalysisExecutionResult(
            plan=self.plan,
            steps=ordered,
            evidence=self.evidence,
            answer=self.answer,
            status=self.plan.status,
            replan_reasons=self.replan_reasons,
            budgets=budget_manager.snapshot(),
            stop_reason=self.stop_reason,
        )


__all__ = [
    "AnalysisExecutionResult",
    "DataAbsentError",
    "AnalysisExecutor",
    "EVIDENCE_KINDS",
    "StepResult",
    "StepStatus",
]
