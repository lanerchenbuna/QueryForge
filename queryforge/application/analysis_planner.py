"""Application entry point for planned, evidence-gated analysis (step 10).

:class:`AnalysisPlannerService` is a *separate* entry point next to ``ask``: the
existing single-query workflow is untouched, while a multi-step question is
turned into a typed :class:`~queryforge.orchestration.planner.plan.AnalysisPlan`
and executed by :class:`~queryforge.orchestration.planner.executor.AnalysisExecutor`.

SQL substeps reuse the same governed kernel as the reflective workflow: every
statement runs through :class:`~queryforge.infrastructure.tools.database_tool.DatabaseTool`
(and therefore the shared AST policy engine) via the registry's ``execute_sql``
tool, with one read-only SQLite connection per worker thread.  The planner never
re-enters :class:`~queryforge.application.agent_service.AgentService`, so a plan
cannot recursively trigger full agent runs.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from queryforge.core.config import Config, load_config
from queryforge.core.observability import (
    SpanRecorder,
    discard_span_recorder,
    new_run_id,
    run_logging_context,
    start_span_recorder,
)
from queryforge.core.schemas.models import DateContext
from queryforge.domain.analysis import (
    AnalysisRequest,
    detect_comparison_baseline,
    detect_time_grain,
    detect_time_range,
    high_impact_question,
    is_high_impact_ambiguity,
)
from queryforge.domain.security import load_sql_policy
from queryforge.domain.semantic import MetricMatch, SemanticModelContext, SemanticModelLoader
from queryforge.infrastructure.db.adapters import open_database as SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.interfaces.transport_security import (
    NETWORK_ENTRYPOINTS,
    validate_transport_options,
)
from queryforge.orchestration.planner.executor import AnalysisExecutionResult, AnalysisExecutor
from queryforge.orchestration.planner.plan import AnalysisPlan, PlanStep
from queryforge.orchestration.runtime.resume import RunResumer
from queryforge.orchestration.tools.budget import BudgetLimits, BudgetManager
from queryforge.orchestration.tools.registry import build_default_registry
from queryforge.orchestration.tools.specs import ToolContext

#: Checks the default plan asks for; the step-08 tool returns ``unknown`` for
#: anything it cannot actually run, so a thin fixture still gets honest evidence.
DEFAULT_QUALITY_CHECKS: tuple[str, ...] = ("grain_unique", "null_rate")

LOGGER = logging.getLogger(__name__)

#: Vocabulary a run id may use. A run id is a directory name under the
#: orchestration state root and the network entrypoints now accept one, so it is
#: confined to exactly what ``AgentTeamStateStore.run_dir`` accepts (letters,
#: digits, ``_`` and ``-``): otherwise ``../..`` would place the plan, journal and
#: state files outside the state root.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_run_id(run_id: str) -> str:
    """Return a run id that is safe to use as a directory name.

    Raises ``ValueError`` for an empty or path-capable id so every entry point
    (CLI, REST, gateway) refuses to build a run directory outside the state root.
    """

    if not RUN_ID_PATTERN.match(run_id or ""):
        raise ValueError(
            "run_id must be 1-64 characters of letters, digits, '_' or '-'"
        )
    return run_id


def _close_span_recorder(recorder: SpanRecorder | None) -> None:
    """Close a run's span recorder, tolerating an already-closed one.

    Mirrors ``AgentService._close_span_recorder``: the planner owns exactly one
    recorder per analysis run and must release it on *every* exit path, including
    a raised ``PlanViolation`` or ``ValueError``. Since step 14 stopped evicting
    an in-flight recorder, an unclosed planner recorder would simply stay in the
    process registry, so closing is not optional here. A failure to close must
    never mask the analysis result, and closing twice must stay harmless.
    """

    try:
        if recorder is not None and not recorder.closed:
            recorder.close()
    except Exception:  # pragma: no cover - closing must never mask a run result
        LOGGER.debug("span recorder close failed", exc_info=True)


class _ThreadLocalDatabaseTools:
    """One governed DatabaseTool per thread (SQLite connections are per thread)."""

    def __init__(self, database_path: str, sql_policy_path: str | None) -> None:
        self.database_path = database_path
        self.sql_policy_path = sql_policy_path
        self._local = threading.local()
        self._created: list[DatabaseTool] = []
        self._lock = threading.Lock()

    def tool(self) -> DatabaseTool:
        existing = getattr(self._local, "tool", None)
        if existing is not None:
            return existing
        connector = SQLiteConnector(self.database_path)
        policy, source = load_sql_policy(self.sql_policy_path)
        tool = DatabaseTool(connector, policy, policy_source_path=source)
        self._local.tool = tool
        self._local.connector = connector
        with self._lock:
            self._created.append(tool)
        return tool

    def close(self) -> None:
        for tool in self._created:
            try:
                tool.connector.close()
            except Exception:
                # sqlite3 refuses a cross-thread close, so a connection opened by
                # a worker thread is released when that thread (and the process)
                # finishes. This is the documented boundary of the thread-local
                # connection strategy, never a silent leak notice.
                pass
        self._created.clear()

    def __enter__(self) -> "_ThreadLocalDatabaseTools":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _ThreadLocalDatabaseToolProxy:
    """Registry-bound facade that resolves the governed tool per calling thread.

    :class:`~queryforge.orchestration.tools.registry.ToolRegistry` binds its
    ``database_tool_factory`` into every tool context, so the planner hands it
    this stable proxy instead of one connection: every attribute access lands on
    the read-only SQLite connection of the *current* thread, which is what makes
    concurrent plan steps safe (sqlite3 connections are not shareable across
    threads).
    """

    def __init__(self, tools: _ThreadLocalDatabaseTools) -> None:
        self._tools = tools

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tools.tool(), name)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<thread-local DatabaseTool proxy>"


#: Ablation switches the step-16 benchmark may turn off (see ``disabled_features``).
#: ``analysis_tools`` / ``data_quality`` drop the corresponding plan steps;
#: ``semantic_compile`` renders SQL through the degraded template instead of the
#: semantic compiler; ``evidence_layer`` skips the step-12 evidence-anchored
#: answer. ``replan`` is expressed with the existing ``max_replans`` knob.
DISABLEABLE_FEATURES: frozenset[str] = frozenset(
    {"analysis_tools", "data_quality", "semantic_compile", "evidence_layer"}
)

#: Which plan actions each ablation removes from the plan.
_ABLATION_ACTIONS: dict[str, tuple[str, ...]] = {
    "analysis_tools": (
        "compare_periods",
        "drill_down",
        "calculate_contribution",
        "detect_anomaly",
        "render_chart",
    ),
    "data_quality": ("check_data_quality",),
}


class AnalysisPlannerService:
    """Plan and execute one analysis question against a governed database."""

    def __init__(
        self,
        *,
        config_loader: Callable[..., Config] = load_config,
        max_replans: int = 2,
        max_workers: int = 4,
        registry_factory: Callable[..., Any] | None = None,
        disabled_features: Iterable[str] = (),
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must be zero or greater")
        self.config_loader = config_loader
        self.max_replans = max_replans
        self.max_workers = max_workers
        self.registry_factory = registry_factory or build_default_registry
        #: Ablation switches used by the step-16 benchmark. Each entry disables a
        #: named capability **through the same code path a real deployment would
        #: use** (dropping the plan steps, or turning the compile/evidence layers
        #: off) so a measured difference is attributable to that capability.
        unknown = sorted(set(disabled_features) - DISABLEABLE_FEATURES)
        if unknown:
            raise ValueError(
                "unknown disabled feature(s): "
                + ", ".join(unknown)
                + f"; known: {', '.join(sorted(DISABLEABLE_FEATURES))}"
            )
        self.disabled_features = frozenset(disabled_features)

    # ------------------------------------------------------------------ public

    def analyze(
        self,
        question: str,
        *,
        run_id: str | None = None,
        resume: bool = False,
        cancel_check: Callable[[], bool] | None = None,
        force_resume: bool = False,
        database: str | None = None,
        domain_id: str | None = None,
        data_version: str | None = None,
        semantic_model_path: str | None = None,
        sql_policy_path: str | None = None,
        mode: str = "execute",
        limits: Mapping[str, Any] | None = None,
        max_replans: int | None = None,
        plan: AnalysisPlan | None = None,
        entrypoint: str | None = None,
    ) -> dict[str, Any]:
        """Run one analysis question and return the structured result.

        ``entrypoint`` names the transport the request arrived on. A network
        entrypoint (``api``, ``api_stream``, ``gateway``, ``mcp``) is held to the
        same transport allowlist as ``AgentService.ask``: caller-supplied
        database, semantic-model and SQL-policy paths are validated *before* any
        file is opened, so ``/analyze`` cannot read a database that ``/ask``
        refuses (H4). A local caller that passes no entrypoint keeps the previous
        behaviour.

        The result carries an additional ``observability`` block (usage,
        end-to-end and per-kind latency, and the run's spans) recorded on the
        same :mod:`queryforge.core.observability` primitives the workflow path
        uses. Without it the planned-analysis entry point, unlike ``/ask``, had no
        cost or latency a caller could reconcile (step 17, section 八 item 3).
        The block is additive: every pre-existing payload key keeps its meaning.
        """

        analysis_run_id = run_id or new_run_id()
        started = time.perf_counter()
        recorder = start_span_recorder(analysis_run_id)
        try:
            # The run context makes a model call performed *inside* a step (a
            # date/LLM fallback) attributable to this run, so its tokens are
            # counted instead of silently lost.
            with run_logging_context(analysis_run_id):
                payload = self._execute_analysis(
                    question,
                    run_id=run_id,
                    resume=resume,
                    cancel_check=cancel_check,
                    force_resume=force_resume,
                    database=database,
                    domain_id=domain_id,
                    data_version=data_version,
                    semantic_model_path=semantic_model_path,
                    sql_policy_path=sql_policy_path,
                    mode=mode,
                    limits=limits,
                    max_replans=max_replans,
                    plan=plan,
                    entrypoint=entrypoint,
                    span_recorder=recorder,
                )
            end_to_end_ms = round((time.perf_counter() - started) * 1000.0, 3)
            payload["observability"] = recorder.to_dict(end_to_end_ms=end_to_end_ms)
            return payload
        finally:
            # Every exit path (including a raised ``PlanViolation``) releases the
            # recorder: an unclosed one would stay in the process registry, which
            # is the same leak the direct-run path had.
            _close_span_recorder(recorder)
            discard_span_recorder(analysis_run_id)

    def _execute_analysis(
        self,
        question: str,
        *,
        run_id: str | None = None,
        resume: bool = False,
        cancel_check: Callable[[], bool] | None = None,
        force_resume: bool = False,
        database: str | None = None,
        domain_id: str | None = None,
        data_version: str | None = None,
        semantic_model_path: str | None = None,
        sql_policy_path: str | None = None,
        mode: str = "execute",
        limits: Mapping[str, Any] | None = None,
        max_replans: int | None = None,
        plan: AnalysisPlan | None = None,
        entrypoint: str | None = None,
        span_recorder: SpanRecorder | None = None,
    ) -> dict[str, Any]:
        """Run the analysis itself; :meth:`analyze` owns the run's observability."""

        text = (question or "").strip()
        if not text:
            raise ValueError("analysis question must not be empty")
        if mode not in {"execute", "plan_only"}:
            raise ValueError("analysis mode must be 'execute' or 'plan_only'")
        if run_id:
            validate_run_id(run_id)

        config = self.config_loader()
        # Bound before the branch: the run's version identity is derived from the
        # resolved domain when there is one, and stays unknown otherwise.
        domain = None
        if domain_id:
            from queryforge.domain.domains import DomainResolver
            domain = DomainResolver.from_config(config).resolve(domain_id)
            for name, supplied, registered in (
                ("database", database, domain.database_path),
                ("semantic_model_path", semantic_model_path, domain.semantic_model_path),
                ("sql_policy_path", sql_policy_path, domain.sql_policy_path),
            ):
                if supplied and (not registered or Path(supplied).resolve() != Path(registered).resolve()):
                    raise ValueError(f"{name} conflicts with registered domain {domain_id!r}")
            if data_version and data_version != domain.data_version:
                raise ValueError("data_version conflicts with published domain")
            database, semantic_model_path, sql_policy_path = domain.database_path, domain.semantic_model_path, domain.sql_policy_path
            data_version = domain.data_version
        if entrypoint in NETWORK_ENTRYPOINTS:
            # Validated after domain resolution, exactly like ``AgentService``:
            # a domain-supplied path is checked too, so a registered domain cannot
            # widen the deployment's allowlist either.
            self._validate_network_paths(
                config,
                entrypoint=entrypoint,
                database=database,
                semantic_model_path=semantic_model_path,
                sql_policy_path=sql_policy_path,
            )
        database_path = self._resolve_database(config, database)
        policy_path = sql_policy_path or config.sql_policy_path
        model_path = semantic_model_path or config.semantic_model_path
        budget_limits = BudgetLimits().merged(limits or {})
        budget = BudgetManager(limits=budget_limits)
        # A resumed run inherits what the earlier attempt already spent. Without
        # this the allowance reset on every resume, so a crashed-and-restarted run
        # could spend its whole budget again while the journal showed the original
        # consumption — the boundary only existed for runs that never restarted.
        inherited: list[str] = []

        # One governed tool stack per analysis run: the connection and the policy
        # engine are opened here (fail fast on a bad database or policy), bound
        # into the registry below, and closed in the ``finally`` block.
        tools = _ThreadLocalDatabaseTools(database_path, policy_path)
        try:
            tools.tool()
            model_context = self._load_semantic_model(model_path, tools, text)
            request = self.build_request(text, model_context)
            date_context = self._date_context(text, request)
            ambiguity = is_high_impact_ambiguity(text, request)
            if ambiguity:
                request.status = "blocked"
                request.unresolved_questions = [
                    high_impact_question(aspect) for aspect in ambiguity
                ]
                return self._clarification_payload(
                    text,
                    request,
                    domain_id=domain_id,
                    reason="high_impact_ambiguity:" + ",".join(ambiguity),
                    budget=budget,
                )
            if model_context is None or not request.metric_ids:
                return self._clarification_payload(
                    text,
                    request,
                    domain_id=domain_id,
                    reason="no_governed_metric_match",
                    budget=budget,
                )
            ungoverned = self._ungoverned_breakdown_terms(text, model_context)
            if ungoverned:
                # The question asks for a breakdown the semantic model does not
                # declare. Answering without it would return a different question's
                # answer (a total) and call it success, so ask instead.
                return self._clarification_payload(
                    text,
                    request,
                    domain_id=domain_id,
                    reason="unsupported_breakdown:" + ",".join(ungoverned),
                    budget=budget,
                )

            analysis_plan = plan or self.build_plan(
                text,
                request,
                model_context,
                domain_id=domain_id,
                semantic_model_path=model_path,
            )
            registry = self._bind_registry(tools, budget, model_context)
            resumer = None
            journal = None
            if run_id:
                state_root = self._state_root(config)
                resumer = RunResumer(state_root, run_id)
                journal = resumer.journal
                if resume:
                    resumer.assert_resumable()
                    recorded = journal.budget() or {}
                    inherited = budget.restore(recorded.get("usage"))
                    if inherited:
                        LOGGER.info(
                            "budget_inherited run_id=%s keys=%s usage=%s",
                            run_id,
                            ",".join(inherited),
                            {key: recorded.get("usage", {}).get(key) for key in inherited},
                        )
                    persisted = resumer.load_plan()
                    if persisted is not None:
                        analysis_plan = persisted
                        self._rebuild_registry_missing_steps(analysis_plan, registry)
                resumer.save_plan(analysis_plan)
            analysis_plan, unavailable = self._drop_unavailable_steps(
                analysis_plan, registry, blocked=self._blocked_actions()
            )
            executor = AnalysisExecutor(
                registry,
                budget_manager=budget,
                max_replans=self.max_replans if max_replans is None else max_replans,
                max_workers=self.max_workers,
                mode=mode,
                journal=journal,
                # Bind the run's identities into every step fingerprint: a resume
                # must not reuse a step computed against a different database
                # snapshot, semantic model or SQL policy.
                run_versions={
                    "data_version": data_version,
                    "semantic_version": (
                        domain.semantic_version if domain is not None else None
                    ),
                    "policy_version": (
                        domain.policy_version if domain is not None else None
                    ),
                },
                worker_id=f"planner-{run_id or uuid4().hex[:6]}",
                cancel_check=cancel_check,
                force_resume=force_resume,
                compile_spec="semantic_compile" not in self.disabled_features,
                evidence_layer="evidence_layer" not in self.disabled_features,
                span_recorder=span_recorder,
                tool_context_factory=lambda: ToolContext(
                    run_id=f"plan_run_{uuid4().hex[:8]}",
                    task_id=analysis_plan.task_id,
                    domain_id=domain_id,
                    data_version=data_version,
                    question=text,
                    database_tool=tools.tool(),
                    semantic_model=model_context,
                    date_context=date_context,
                ),
            )
            result = executor.execute(analysis_plan)
            return self._payload(
                result,
                request,
                mode=mode,
                domain_id=domain_id,
                unavailable_actions=unavailable,
            )
        finally:
            tools.close()

    # ------------------------------------------------- durable run control

    def _state_root(self, config: Any) -> Path:
        """The directory holding every persisted run of this deployment."""
        return Path(
            getattr(config, "orchestration_state_root", ".queryforge/runs")
        ).expanduser()

    def _blocked_actions(self) -> set[str]:
        """Plan actions the configured ablations remove (step 16)."""
        blocked: set[str] = set()
        for feature in self.disabled_features:
            blocked.update(_ABLATION_ACTIONS.get(feature, ()))
        return blocked

    def cancel_run(self, run_id: str, *, reason: str = "cancelled by client") -> bool:
        """Persist cancellation for a durable run.

        Called when the caller stops waiting (for example an SSE client
        disconnecting): the run is marked terminal so the work already committed
        is never silently resumed into a half-finished answer. Returns ``False``
        when the run had already ended.
        """
        if not run_id:
            raise ValueError("run_id is required to cancel a durable run")
        validate_run_id(run_id)
        config = self.config_loader()
        return RunResumer(self._state_root(config), run_id).cancel(reason)

    def run_status(self, run_id: str) -> Any:
        """Operator-facing status of a persisted run (steps, terminal outcome)."""
        if not run_id:
            raise ValueError("run_id is required to read a durable run status")
        validate_run_id(run_id)
        config = self.config_loader()
        return RunResumer(self._state_root(config), run_id).status()

    def build_request(
        self, question: str, model_context: SemanticModelContext | None
    ) -> AnalysisRequest:
        """Build the typed analysis intent using the public step-04 API."""

        metric_matches: list[MetricMatch] = (
            SemanticModelLoader.match_metrics(model_context.model, question)
            if model_context is not None
            else []
        )
        dimensions = [
            match.semantic_name
            for match in (model_context.matches if model_context is not None else [])
            if match.kind == "dimension"
        ]
        return AnalysisRequest(
            intent="analyze",
            metric_ids=[match.metric.name for match in metric_matches],
            dimensions=list(dict.fromkeys(dimensions)),
            time_range=detect_time_range(question),
            time_grain=detect_time_grain(question),
            comparison_baseline=detect_comparison_baseline(question),
            output="structured_analysis",
        )

    def build_plan(
        self,
        question: str,
        request: AnalysisRequest,
        model_context: SemanticModelContext,
        *,
        domain_id: str | None = None,
        semantic_model_path: str | None = None,
    ) -> AnalysisPlan:
        """Construct the default plan: resolve → quality → metric → compose."""

        metric_name = request.metric_ids[0]
        metric = next(
            (item for item in model_context.model.metrics if item.name == metric_name), None
        )
        if metric is None:
            raise ValueError(f"unknown governed metric {metric_name!r}")
        entity = next(
            (item for item in model_context.model.entities if item.name == metric.entity), None
        )
        table = entity.table if entity else None
        if not table:
            raise ValueError(
                f"metric {metric_name!r} references unknown entity {metric.entity!r}"
            )
        dimension_refs = self._dimension_refs(
            model_context, request.dimensions, question
        )
        time_dimension = self._time_dimension_ref(model_context, metric, entity)
        template = self._select_template(
            question, request, dimension_refs, time_dimension
        )
        return AnalysisPlan(
            plan_id=f"plan_{uuid4().hex[:10]}",
            task_id=f"task_{uuid4().hex[:10]}",
            question=question,
            domain_id=domain_id,
            steps=self._template_steps(
                template,
                metric_name,
                table,
                dimension_refs,
                request,
                time_dimension=time_dimension,
            ),
            status="pending",
        )

    # ------------------------------------------------------------- templates

    #: Deterministic task templates (plan 阶段 4 / 11 第 6 条).
    TEMPLATES: tuple[str, ...] = (
        "default",
        "trend",
        "segment",
        "contribution",
        "anomaly",
    )

    _TREND_MARKERS: tuple[str, ...] = (
        "trend",
        "over time",
        "by month",
        "by week",
        "by day",
        "by quarter",
        "by year",
        "monthly",
        "weekly",
        "time series",
        "趋势",
        "按月",
        "按周",
        "随时间",
    )

    _ANOMALY_MARKERS: tuple[str, ...] = (
        "anomal",
        "spike",
        "sudden",
        "unexpected",
        "outlier",
        "异常",
        "突增",
        "突降",
        "异动",
    )

    @classmethod
    def _select_template(
        cls,
        question: str,
        request: AnalysisRequest,
        dimension_refs: list[str],
        time_dimension: str | None = None,
    ) -> str:
        """Pick a deterministic task template from the typed request.

        Ordered-series templates (trend/anomaly) require a resolvable time
        dimension, because the governed compiler does not bucket timestamps.
        """
        lowered = question.casefold()
        if time_dimension and any(marker in lowered for marker in cls._ANOMALY_MARKERS):
            return "anomaly"
        has_time = bool(request.time_range) or bool(request.time_grain)
        if not has_time and time_dimension and any(
            marker in lowered for marker in cls._TREND_MARKERS
        ):
            has_time = True
        if time_dimension and has_time:
            return "trend"
        if request.comparison_baseline and dimension_refs:
            return "contribution"
        if dimension_refs:
            return "segment"
        return "default"

    @staticmethod
    def _time_dimension_ref(model_context: Any, metric: Any, entity: Any) -> str | None:
        """The entity dimension whose column backs the metric's time field."""
        if entity is None:
            return None
        time_field = getattr(metric, "time_field", None)
        if not time_field or "." not in str(time_field):
            return None
        column = str(time_field).partition(".")[2]
        for dimension in getattr(entity, "dimensions", []):
            if dimension.column == column:
                return f"{entity.name}.{dimension.name}"
        return None

    def _template_steps(
        self,
        template: str,
        metric_name: str,
        table: str,
        dimension_refs: list[str],
        request: AnalysisRequest,
        *,
        time_dimension: str | None = None,
    ) -> list[PlanStep]:
        """Build the step list for one template.

        Step inputs are declarative knobs (metric, dimension, window, grain);
        the executor assembles the concrete values from governed SQL at run
        time, so no numbers are invented at planning time.
        """
        resolved = PlanStep(
            id="resolve_metric",
            action="resolve_metric",
            inputs={
                "term": metric_name,
                "question": request.output or metric_name,
                "dimensions": dimension_refs,
            },
            expected_evidence=["metric_resolution"],
            validation={"require_metric": metric_name},
            budget={"max_tool_calls": 0},
        )
        quality = PlanStep(
            id="check_data_quality",
            action="check_data_quality",
            inputs={"table_name": table, "checks": list(DEFAULT_QUALITY_CHECKS)},
            depends_on=["resolve_metric"],
            expected_evidence=["data_quality"],
            validation={"block_on_error": True},
            budget={"max_tool_calls": 1},
        )
        metric_knobs = {"metric": metric_name, "dimensions": dimension_refs}
        if request.time_grain:
            metric_knobs["time_grain"] = request.time_grain
        if request.time_range:
            metric_knobs["time_range"] = request.time_range
        if template == "trend":
            trend_refs = [time_dimension] if time_dimension else []
            return [
                resolved,
                quality,
                PlanStep(
                    id="query_metric",
                    action="query_metric",
                    inputs={**metric_knobs, "dimensions": trend_refs, "limit": 100},
                    depends_on=["check_data_quality"],
                    expected_evidence=["metric_value"],
                    validation={"require_grain": trend_refs},
                    budget={"max_tool_calls": 2},
                ),
                PlanStep(
                    id="compare_periods",
                    action="compare_periods",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["period_comparison"],
                    validation={"require_metric": metric_name, "knobs": metric_knobs},
                    budget={"max_tool_calls": 1},
                ),
                PlanStep(
                    id="render_chart",
                    action="render_chart",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["chart"],
                    validation={"require_metric": metric_name, "knobs": metric_knobs},
                    budget={"max_tool_calls": 1},
                ),
                self._compose_step(
                    ["compare_periods", "render_chart"],
                    ["metric_resolution", "data_quality", "metric_value", "period_comparison"],
                ),
            ]
        if template == "segment":
            return [
                resolved,
                quality,
                PlanStep(
                    id="query_metric",
                    action="query_metric",
                    inputs={**metric_knobs, "limit": 100},
                    depends_on=["check_data_quality"],
                    expected_evidence=["metric_value"],
                    validation={"require_grain": dimension_refs},
                    budget={"max_tool_calls": 2},
                ),
                PlanStep(
                    id="drill_down",
                    action="drill_down",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["drill_down"],
                    validation={
                        "require_metric": metric_name,
                        "knobs": {
                            **metric_knobs,
                            "dimension": dimension_refs[0] if dimension_refs else None,
                            "max_categories": 10,
                        },
                    },
                    budget={"max_tool_calls": 1},
                ),
                PlanStep(
                    id="render_chart",
                    action="render_chart",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["chart"],
                    validation={"require_metric": metric_name, "knobs": metric_knobs},
                    budget={"max_tool_calls": 1},
                ),
                self._compose_step(
                    ["drill_down", "render_chart"],
                    ["metric_resolution", "data_quality", "metric_value", "drill_down"],
                ),
            ]
        if template == "contribution":
            # Per-category two-period values are not queryable yet (the governed
            # compiler groups by one dimension), so the plan compares aggregate
            # periods and records the missing decomposition as a limitation.
            return [
                resolved,
                quality,
                PlanStep(
                    id="query_metric",
                    action="query_metric",
                    inputs={**metric_knobs, "limit": 100},
                    depends_on=["check_data_quality"],
                    expected_evidence=["metric_value"],
                    validation={"require_grain": dimension_refs},
                    budget={"max_tool_calls": 2},
                ),
                PlanStep(
                    id="compare_periods",
                    action="compare_periods",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["period_comparison"],
                    validation={"require_metric": metric_name, "knobs": metric_knobs},
                    budget={"max_tool_calls": 1},
                ),
                self._compose_step(
                    ["query_metric", "compare_periods"],
                    [
                        "metric_resolution",
                        "data_quality",
                        "metric_value",
                        "period_comparison",
                    ],
                ),
            ]
        if template == "anomaly":
            return [
                resolved,
                quality,
                PlanStep(
                    id="query_metric",
                    action="query_metric",
                    inputs={
                        **metric_knobs,
                        "dimensions": [time_dimension] if time_dimension else [],
                        "limit": 100,
                    },
                    depends_on=["check_data_quality"],
                    expected_evidence=["metric_value"],
                    validation={
                        "require_grain": [time_dimension] if time_dimension else []
                    },
                    budget={"max_tool_calls": 2},
                ),
                PlanStep(
                    id="detect_anomaly",
                    action="detect_anomaly",
                    inputs={},
                    depends_on=["query_metric"],
                    expected_evidence=["anomaly"],
                    validation={
                        "require_metric": metric_name,
                        "knobs": {**metric_knobs, "min_points": 4},
                    },
                    budget={"max_tool_calls": 1},
                ),
                self._compose_step(
                    ["detect_anomaly"],
                    ["metric_resolution", "data_quality", "metric_value", "anomaly"],
                ),
            ]
        # default: the original single-metric plan
        return [
            resolved,
            quality,
            PlanStep(
                id="query_metric",
                action="query_metric",
                inputs={**metric_knobs, "limit": 100},
                depends_on=["check_data_quality"],
                expected_evidence=["metric_value"],
                validation={"require_grain": dimension_refs},
                budget={"max_tool_calls": 2},
            ),
            self._compose_step(
                ["query_metric"],
                ["metric_resolution", "data_quality", "metric_value"],
            ),
        ]

    @staticmethod
    def _compose_step(depends_on: list[str], required_evidence: list[str]) -> PlanStep:
        return PlanStep(
            id="compose_answer",
            action="compose_answer",
            inputs={},
            depends_on=list(depends_on),
            expected_evidence=["answer"],
            validation={"require_evidence": list(required_evidence)},
            budget={"max_tool_calls": 0},
        )

    @staticmethod
    def _rebuild_registry_missing_steps(plan: AnalysisPlan, registry: Any) -> None:
        """Re-apply the capability filter to a persisted plan on resume."""
        from queryforge.orchestration.planner.plan import ACTION_TOOL_MAP

        kept = []
        for step in plan.steps:
            tool = ACTION_TOOL_MAP.get(step.action)
            if tool is None or (
                registry.has(tool) and registry.is_available(tool)
            ):
                kept.append(step)
        if len(kept) != len(plan.steps):
            plan.steps = kept

    # --------------------------------------------------------------- internals

    def _bind_registry(
        self,
        tools: _ThreadLocalDatabaseTools,
        budget: BudgetManager,
        model_context: SemanticModelContext,
    ) -> Any:
        """Bind this run's governed tools into a fresh registry.

        The bound object is the thread-local proxy: the registry stores it in
        every ``ToolContext``, and each access then resolves the governed
        ``DatabaseTool`` of the *calling* thread. Binding is verified here so an
        unbound registry fails the request immediately (``ValueError``) instead
        of surfacing later as a per-step "no governed database tool" failure.
        """

        bound = _ThreadLocalDatabaseToolProxy(tools)
        registry = self.registry_factory(
            bound,
            budget,
            semantic_model=model_context,
        )
        if getattr(registry, "database_tool_factory", None) is None:
            raise ValueError(
                "analysis registry was built without a governed database tool; "
                "pass a DatabaseTool or a thread-local proxy as database_tool_factory"
            )
        return registry

    @staticmethod
    def _validate_network_paths(
        config: Config,
        *,
        entrypoint: str,
        database: str | None,
        semantic_model_path: str | None,
        sql_policy_path: str | None,
    ) -> None:
        """Confine a network caller's file paths to the transport allowlist.

        Mirrors ``AgentService._run`` (``validate_transport_options``) so both
        entry points of the deployment enforce one rule instead of two different
        ones. Only the three analysis paths are passed: the planner has no report
        directory or subject-tree option to confine.
        """

        from queryforge.application.options import AgentOptions

        validate_transport_options(
            config,
            AgentOptions(
                database=database,
                semantic_model_path=semantic_model_path,
                sql_policy_path=sql_policy_path,
                entrypoint=entrypoint,
            ),
        )

    @staticmethod
    def _resolve_database(config: Config, database: str | None) -> str:
        path = Path(database or config.database_path).expanduser()
        if not path.is_file():
            raise ValueError(f"SQLite database does not exist: {path}")
        return str(path)

    @staticmethod
    def _load_semantic_model(
        model_path: str | None,
        tools: _ThreadLocalDatabaseTools,
        question: str,
    ) -> SemanticModelContext | None:
        if not model_path:
            return None
        tool = tools.tool()
        schemas = [
            tool.describe_table_for_validation(name) for name in tool.list_tables()
        ]
        return SemanticModelLoader.load_and_validate(model_path, schemas, question)

    #: Words that follow "by" without naming a breakdown dimension.
    _BREAKDOWN_STOPWORDS: frozenset[str] = frozenset(
        {
            "far",
            "now",
            "then",
            "default",
            "order",
            "hour",
            "day",
            "date",
            "week",
            "month",
            "quarter",
            "year",
            "years",
            "time",
            "hand",
            "the",
            "and",
        }
    )

    @staticmethod
    def _date_context(question: str, request: AnalysisRequest) -> Any:
        """Resolve the question's time window with the workflow's rule parser.

        ``detect_time_range`` only understands relative windows ("last 3 months"),
        so "watch hours in 2024" used to compile an unfiltered query and report the
        all-time total as success. The rule parser used on the workflow path already
        resolves years, quarters and months; reusing it here keeps both paths
        consistent, and the parsed span also lands on the request for auditability.
        """
        try:
            from queryforge.workflow.node.date_parser_node import DateParserNode
        except Exception:  # pragma: no cover - planner must not depend on the workflow
            return None
        try:
            ranges = DateParserNode.parse_rules(question, date.today())
        except Exception:  # pragma: no cover - a parser failure never blocks the run
            return None
        if not ranges:
            return None
        if request.time_range is None:
            first = ranges[0]
            request.time_range = f"{first.start_date}..{first.end_date}"
        return DateContext(
            reference_date=date.today().isoformat(), source="rule", ranges=list(ranges)
        )

    @classmethod
    def _ungoverned_breakdown_terms(
        cls, question: str, model_context: SemanticModelContext
    ) -> list[str]:
        """Breakdown terms the semantic model cannot honour ("... by brand_name").

        Only an explicit single-term ``by <term>`` phrase is considered, and only
        when the term is not governed vocabulary (dimension, metric, entity, table,
        synonym, time word) — a false clarification would be as bad as a silent
        wrong answer, so the rule stays narrow.
        """
        model = model_context.model
        vocabulary = {
            str(name).lower()
            for name in (
                *cls._declared_dimension_names(model_context),
                *(metric.name for metric in model.metrics),
                *(metric.entity for metric in model.metrics),
                *(entity.name for entity in model.entities),
                *(entity.table for entity in model.entities),
                *(term for metric in model.metrics for term in metric.synonyms),
                *(term for entity in model.entities for term in entity.synonyms),
            )
        }
        terms: list[str] = []
        for match in re.finditer(r"\bby\s+([A-Za-z][A-Za-z0-9_]{2,})\b", question):
            term = match.group(1)
            lowered = term.lower()
            if lowered in vocabulary or lowered in cls._BREAKDOWN_STOPWORDS:
                continue
            if lowered not in terms:
                terms.append(lowered)
        return terms

    @staticmethod
    def _declared_dimension_names(model_context: SemanticModelContext) -> set[str]:
        """Every dimension name the semantic model declares, across entities."""
        return {
            dimension.name
            for entity in model_context.model.entities
            for dimension in entity.dimensions
        }

    @staticmethod
    def _dimension_refs(
        model_context: SemanticModelContext,
        dimension_names: list[str],
        question: str = "",
    ) -> list[str]:
        """Resolve each requested dimension to exactly ONE entity-qualified ref.

        One business term can be declared on several entities ("format" exists for
        both ``anime`` and ``ad_impression``). Returning every declaration made the
        downstream compiler fail — it receives one dimension name, not a set — so a
        question that should have been answerable ended in a preview. Resolution is
        deterministic: the entity the question names wins, then the metric's own
        entity (no join needed), then the first declaration in model order.
        """
        references: list[str] = []
        model = model_context.model
        lowered = question.lower()
        metric_entities = {metric.entity for metric in model.metrics}
        for name in dimension_names:
            candidates = [
                entity
                for entity in model.entities
                for dimension in entity.dimensions
                if dimension.name == name
            ]
            if not candidates:
                continue
            chosen = next(
                (entity for entity in candidates if entity.name.lower() in lowered),
                None,
            )
            if chosen is None:
                chosen = next(
                    (entity for entity in candidates if entity.name in metric_entities),
                    None,
                )
            if chosen is None:
                chosen = candidates[0]
            reference = f"{chosen.name}.{name}"
            if reference not in references:
                references.append(reference)
        return references

    def _clarification_payload(
        self,
        question: str,
        request: AnalysisRequest,
        *,
        domain_id: str | None,
        reason: str,
        budget: BudgetManager,
    ) -> dict[str, Any]:
        plan = AnalysisPlan(
            plan_id=f"plan_{uuid4().hex[:10]}",
            task_id=f"task_{uuid4().hex[:10]}",
            question=question,
            domain_id=domain_id,
            steps=[],
            status="needs_clarification",
        )
        return {
            "plan": plan.to_payload(),
            "steps": [],
            "evidence": [],
            "answer": None,
            "status": "needs_clarification",
            "replan_reasons": [],
            "budgets": budget.snapshot(),
            "stop_reason": reason,
            "analysis_request": request.model_dump(mode="json"),
            "unresolved_questions": list(request.unresolved_questions),
            "domain_id": domain_id,
        }

    def _payload(
        self,
        result: AnalysisExecutionResult,
        request: AnalysisRequest,
        *,
        mode: str,
        domain_id: str | None,
        unavailable_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = result.to_payload()
        limitations = list(payload.get("limitations") or [])
        if unavailable_actions:
            limitations.append(
                "declared actions without an implementation were skipped: "
                + ", ".join(sorted(set(unavailable_actions)))
            )
        payload.update(
            {
                "steps": [step.to_payload() for step in result.steps],
                "analysis_request": request.model_dump(mode="json"),
                "mode": mode,
                "domain_id": domain_id,
                "unavailable_actions": sorted(set(unavailable_actions or [])),
                "limitations": limitations,
            }
        )
        # Lift the step-12 evidence layer to the top level for callers.
        for step in result.steps:
            outputs = getattr(step, "outputs", None) or {}
            if not isinstance(outputs, dict):
                continue
            if outputs.get("final_answer") is not None:
                payload.setdefault("final_answer", outputs["final_answer"])
            if outputs.get("evidence") is not None:
                payload.setdefault("evidence", outputs["evidence"])
            if outputs.get("validation_problems"):
                payload.setdefault(
                    "validation_problems", list(outputs["validation_problems"])
                )
            if outputs.get("final_answer_error"):
                payload.setdefault(
                    "final_answer_error", outputs["final_answer_error"]
                )
        return payload

    @staticmethod
    def _drop_unavailable_steps(
        plan: AnalysisPlan, registry: Any, *, blocked: Iterable[str] = ()
    ) -> tuple[AnalysisPlan, list[str]]:
        """Drop declared-but-unimplemented steps so capability gaps degrade honestly.

        Step 11 tools are declared in the registry before they are implemented;
        a plan that references one would fail the whole task. Instead the step
        (and its dependents' evidence requirements) is removed and recorded as a
        limitation, so simple paths keep working and the gap stays visible.
        """
        from queryforge.orchestration.planner.plan import ACTION_TOOL_MAP

        def available(action: str) -> bool:
            tool = ACTION_TOOL_MAP.get(action)
            if tool is None:
                return True
            try:
                return bool(registry.has(tool) and registry.is_available(tool))
            except Exception:
                return False

        # ``blocked`` carries the step-16 ablation switches: a disabled feature is
        # removed through exactly this path, so the plan degrades the same way it
        # would if the capability were missing from the deployment.
        blocked_actions = {str(item) for item in blocked}
        dropped: set[str] = set()
        unavailable: list[str] = []
        for step in plan.steps:
            if step.action in {"compose_answer", "resolve_metric"}:
                continue
            if step.action in blocked_actions:
                dropped.add(step.id)
                unavailable.append(step.action)
                continue
            if not available(step.action):
                dropped.add(step.id)
                unavailable.append(step.action)
        if not dropped:
            return plan, unavailable

        producer_of: dict[str, str] = {}
        try:
            from queryforge.orchestration.planner.executor import EVIDENCE_KINDS

            producer_of = {step.id: EVIDENCE_KINDS.get(step.action, step.action) for step in plan.steps}
        except Exception:  # pragma: no cover - defensive import guard
            producer_of = {}

        removed_evidence = {producer_of.get(step_id) for step_id in dropped}
        dependency_map = {step.id: list(step.depends_on) for step in plan.steps}

        def surviving_dependencies(step_id: str, seen: set[str] | None = None) -> list[str]:
            """Rewire a kept step onto the surviving upstream steps."""
            seen = seen or set()
            if step_id in seen:
                return []
            seen.add(step_id)
            resolved: list[str] = []
            for dependency in dependency_map.get(step_id, []):
                if dependency in dropped:
                    for upstream in surviving_dependencies(dependency, seen):
                        if upstream not in resolved:
                            resolved.append(upstream)
                elif dependency not in resolved:
                    resolved.append(dependency)
            return resolved

        kept: list[PlanStep] = []
        for step in plan.steps:
            if step.id in dropped:
                continue
            dependencies = surviving_dependencies(step.id)
            updates: dict[str, Any] = {}
            if dependencies != list(step.depends_on):
                updates["depends_on"] = dependencies
            if step.action == "compose_answer":
                required = [
                    kind
                    for kind in step.validation.get("require_evidence", [])
                    if kind not in removed_evidence
                ]
                updates["validation"] = {**step.validation, "require_evidence": required}
            kept.append(step.model_copy(update=updates) if updates else step)
        return plan.model_copy(update={"steps": kept}), unavailable


__all__ = ["AnalysisPlannerService", "DEFAULT_QUALITY_CHECKS", "validate_run_id"]
