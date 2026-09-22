"""Build and run QueryForge's bounded reflective SQL workflow."""

from __future__ import annotations

from datetime import date
import json
import logging
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Callable

from queryforge.workflow.node.date_parser_node import DateParserNode
from queryforge.workflow.node.execute_sql_node import ExecuteSqlNode
from queryforge.workflow.node.fix_node import FixNode
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.node.metric_search_node import MetricSearchNode
from queryforge.workflow.node.output_node import OutputNode
from queryforge.workflow.node.plan_mode_node import (
    PlanApprover,
    PlanModeNode,
    PlanPresenter,
)
from queryforge.workflow.node.plan_output_node import PlanOutputNode
from queryforge.workflow.node.parallel_candidates_node import ParallelCandidatesNode
from queryforge.workflow.node.reflect_node import ReflectNode
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.node.skill_selection_node import SkillSelectionNode
from queryforge.workflow.node.subject_selection_node import SubjectSelectionNode
from queryforge.workflow.node.tool_loop_node import ToolLoopNode
from queryforge.workflow.node.visualization_node import VisualizationNode
from queryforge.workflow.budgeted_model import BudgetedModelProvider
from queryforge.workflow.event_emitter import EventEmitter
from queryforge.workflow.workflow import (
    ReflectiveWorkflow,
    WorkflowCancelled,
    WorkflowError,
)
from queryforge.core.config import Config
from queryforge.infrastructure.db.adapters import open_database as SQLiteConnector
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.core.observability import (
    ObservedModelProvider,
    Span,
    SpanRecorder,
    ensure_logging_configured,
    get_span_recorder,
    new_run_id,
    run_logging_context,
    stable_digest,
    start_span_recorder,
)
from queryforge.core.schemas.models import (
    Context,
    RunContext,
    SQLContext,
    SqlTask,
    VectorMatch,
)
from queryforge.domain.security import load_sql_policy
from queryforge.domain.skills.manager import SkillManager
from queryforge.infrastructure.storage import (
    LanceDBVectorStore,
    OpenAIEmbeddingProvider,
    SQLHistoryStore,
    SQL_SOURCE_TYPES,
    VectorStore,
)
from queryforge.orchestration.tools import BudgetManager
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.infrastructure.tools.reference_sql_tool import ReferenceSqlTool


LOGGER = logging.getLogger("queryforge.runner")


def sqlite_connection(connector: object) -> object | None:
    """Return the raw sqlite3 connection behind a connector, if reachable."""

    for attribute in ("connection", "_connection"):
        connection = getattr(connector, attribute, None)
        if connection is not None and hasattr(connection, "interrupt"):
            return connection
    return None


def install_cancellation_handler(
    connector: object, cancel_check: Callable[[], bool] | None
) -> Callable[[], None]:
    """Interrupt in-flight SQLite work as soon as cancellation is requested.

    Step 09 established that a SQLite progress handler can abort a running
    statement (see ``orchestration/tools/budget.py::install_sql_deadline_handler``);
    cancellation reuses exactly that mechanism with the cancel flag as the
    trigger instead of a wall-clock deadline, so a client disconnect reaches the
    SQL boundary instead of waiting for the statement to finish.

    A connection has a single progress-handler slot, which other components
    legitimately reuse (the step 09 budget deadline, the step 11 quality tool).
    A lightweight watcher thread therefore also calls ``Connection.interrupt()``,
    so cancellation still lands when another component has replaced the handler.
    Returns a ``stop`` callable. Non-SQLite databases and in-flight model calls
    keep their documented "cannot be interrupted" limitation.
    """

    connection = sqlite_connection(connector)
    if connection is None or cancel_check is None:
        return lambda: None
    setter = getattr(connection, "set_progress_handler", None)
    installed = False
    if setter is not None:

        def handler() -> int:
            return 1 if cancel_check() else 0

        try:  # pragma: no cover - depends on the sqlite3 build
            setter(handler, 1000)
            installed = True
        except Exception:  # pragma: no cover - defensive
            installed = False

    stop_event = threading.Event()

    def watch() -> None:
        while not stop_event.wait(0.02):
            if cancel_check():
                try:
                    connection.interrupt()
                except Exception:  # pragma: no cover - defensive
                    pass
                return

    watcher = threading.Thread(
        target=watch, name="queryforge-cancel-watch", daemon=True
    )
    watcher.start()

    def stop() -> None:
        stop_event.set()
        watcher.join(timeout=1.0)
        if installed:
            try:
                setter(None, 0)
            except Exception:  # pragma: no cover - defensive
                pass

    return stop


class ObservedConnector:
    """Duck-typed connector proxy that records ``sql`` and ``tool`` spans.

    The SQL text is never stored: spans keep a character count and a short
    digest so a trace can be correlated with logs without copying query text
    (which may embed literals) into the observability store.
    """

    def __init__(self, connector: object, run_id: str, recorder: SpanRecorder | None) -> None:
        self._connector = connector
        self._run_id = run_id
        self._recorder = recorder

    def __getattr__(self, name):
        return getattr(self._connector, name)

    def _span(self, name: str, kind: str, attributes: dict | None = None) -> Span | None:
        recorder = self._recorder or get_span_recorder(self._run_id)
        if recorder is None:
            return None
        return recorder.begin(name, kind, attributes=attributes)

    def _end(self, span: Span | None, *, status: str, **attributes: object) -> None:
        if span is None:
            return
        recorder = self._recorder or get_span_recorder(self._run_id)
        if recorder is None:
            return
        span.attributes.update(
            {key: value for key, value in attributes.items() if value is not None}
        )
        recorder.end(span, status=status)

    def execute_sql(self, sql: str):
        statement = sql if isinstance(sql, str) else str(sql)
        span = self._span(
            "sql.execute",
            "sql",
            {
                "statement_chars": len(statement),
                "statement_digest": stable_digest(statement),
                "database": getattr(self._connector, "database_path", None)
                and str(getattr(self._connector, "database_path")),
            },
        )
        try:
            result = self._connector.execute_sql(sql)
        except BaseException as exc:
            self._end(span, status="failed", error_type=type(exc).__name__)
            raise
        self._end(
            span,
            status="success",
            row_count=getattr(result, "row_count", None),
            column_count=len(getattr(result, "columns", []) or []),
        )
        return result

    def list_tables(self):
        return self._observe_tool("list_tables", self._connector.list_tables)

    def describe_table(self, table_name: str):
        return self._observe_tool(
            "describe_table", self._connector.describe_table, table_name
        )

    def find_matching_values(self, *args, **kwargs):
        return self._observe_tool(
            "find_matching_values",
            self._connector.find_matching_values,
            *args,
            **kwargs,
        )

    def _observe_tool(self, name: str, operation: Callable, *args, **kwargs):
        span = self._span(f"tool.{name}", "tool", {"tool": name})
        try:
            result = operation(*args, **kwargs)
        except BaseException as exc:
            self._end(span, status="failed", error_type=type(exc).__name__)
            raise
        self._end(
            span,
            status="success",
            item_count=len(result) if hasattr(result, "__len__") else None,
        )
        return result

    def __getattr__(self, name: str):
        return getattr(self._connector, name)

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        close = getattr(self._connector, "close", None)
        if callable(close):
            close()


class WorkflowRunner:
    NODE_NAMES = (
        "date_parser",
        "schema_linking",
        "skill_selection",
        "metric_search",
        "tool_loop (optional)",
        "gen_sql",
        "plan_mode_check (optional)",
        "execute_sql",
        "reflect",
        "fix/regenerate (conditional, max_retries)",
        "output",
        "visualization (optional)",
    )
    NODE_DESCRIPTIONS = (
        ("date_parser", "resolve relative dates before SQL generation"),
        ("schema_linking", "read SQLite table and column metadata"),
        ("skill_selection", "select relevant prompt skills automatically or manually"),
        ("metric_search", "match structured metrics and validate group dimensions"),
        ("tool_loop", "optionally collect bounded read-only observations before SQL generation"),
        ("gen_sql", "ask the configured LLM with local skills for one read-only query"),
        ("plan_mode_check", "optionally require approval before SQL execution"),
        ("execute_sql", "validate and execute the generated query"),
        ("reflect", "evaluate SQL semantics and result quality"),
        ("fix/regenerate", "conditionally repair or regenerate SQL within retry limit"),
        ("output", "format SQL, explanation, and rows as JSON"),
        ("visualization", "optionally build a local Vega-Lite chart configuration"),
    )

    def __init__(
        self,
        config: Config,
        llm_factory: Callable[[Config], BaseModelProvider] = ModelFactory.create,
        skill_manager: SkillManager | None = None,
        selected_skills: list[str] | None = None,
        date_llm_fallback: bool = False,
        today_provider: Callable[[], date] = date.today,
        plan_mode: bool = False,
        auto_approve_plan: bool = False,
        plan_approver: PlanApprover | None = None,
        plan_presenter: PlanPresenter | None = None,
        max_retries: int = 2,
        history_store: SQLHistoryStore | None = None,
        history_top_k: int = 3,
        enable_vector_kb: bool = False,
        vector_store: VectorStore | None = None,
        vector_top_k: int = 3,
        visualize: bool = False,
        chart_output_dir: str | None = None,
        debug_prompts: bool = False,
        trace_dir: str | None = None,
        show_run_summary: bool = False,
        run_id_factory: Callable[[], str] = new_run_id,
        plan_only: bool = False,
        semantic_model_path: str | None = None,
        subject_tree_enabled: bool = False,
        subject_tree_path: str | None = None,
        subject: str | None = None,
        default_subject: str | None = None,
        sql_policy_path: str | None = None,
        initial_sql: str | None = None,
        analysis_hook: Callable[[Context], None] | None = None,
        candidate_hook: Callable[[Context, DatabaseTool], None] | None = None,
        completion_hook: Callable[[Context], None] | None = None,
        tool_loop_enabled: bool = False,
        tool_loop_max_rounds: int = 5,
        tool_loop_timeout_seconds: float = 30,
        tool_loop_preview_limit: int = 20,
        tool_budget_manager: BudgetManager | None = None,
        tool_budget_limits: dict[str, float] | None = None,
        model_budget_manager: BudgetManager | None = None,
        model_budget_limits: dict[str, float] | None = None,
        parallel_candidates: int = 1,
        parallel_max_preview: int = 2,
        parallel_preview_limit: int = 20,
        parallel_preview_timeout_seconds: float = 10,
        selector_weights: dict[str, float] | None = None,
        event_emitter: EventEmitter | None = None,
        report_requested: bool = False,
        report_output_dir: str | None = None,
        report_max_rows: int | None = None,
        report_max_charts: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
        history_domain_id: str | None = None,
        history_data_version: str | None = None,
        retrieval_scope: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.llm_factory = llm_factory
        self.skill_manager = skill_manager or SkillManager()
        self.selected_skills = selected_skills
        self.date_llm_fallback = date_llm_fallback
        self.today_provider = today_provider
        self.plan_mode = plan_mode
        self.auto_approve_plan = auto_approve_plan
        self.plan_approver = plan_approver
        self.plan_presenter = plan_presenter
        if max_retries < 0:
            raise ValueError("max_retries must be zero or greater")
        self.max_retries = max_retries
        if history_top_k < 0:
            raise ValueError("history_top_k must be zero or greater")
        self.history_store = history_store
        self.history_top_k = history_top_k
        if vector_top_k < 0:
            raise ValueError("vector_top_k must be zero or greater")
        self.enable_vector_kb = enable_vector_kb
        self.vector_store = vector_store
        self.vector_top_k = vector_top_k
        self.visualize = visualize
        self.chart_output_dir = chart_output_dir
        self.debug_prompts = debug_prompts
        self.trace_dir = trace_dir
        self.show_run_summary = show_run_summary
        self.run_id_factory = run_id_factory
        self.plan_only = plan_only
        self.semantic_model_path = semantic_model_path or config.semantic_model_path
        self.subject_tree_enabled = subject_tree_enabled or config.subject_tree_enabled
        self.subject_tree_path = subject_tree_path or config.subject_tree_path
        self.subject = subject
        self.default_subject = default_subject or config.default_subject
        self.sql_policy_path = sql_policy_path or config.sql_policy_path
        self.initial_sql = initial_sql.strip() if initial_sql and initial_sql.strip() else None
        self.analysis_hook = analysis_hook
        self.candidate_hook = candidate_hook
        self.completion_hook = completion_hook
        self.tool_loop_enabled = tool_loop_enabled
        self.tool_loop_max_rounds = tool_loop_max_rounds
        self.tool_loop_timeout_seconds = tool_loop_timeout_seconds
        self.tool_loop_preview_limit = tool_loop_preview_limit
        # Step 09: the tool loop shares one atomic budget per run.
        self.tool_budget_manager = tool_budget_manager
        self.tool_budget_limits = dict(tool_budget_limits or {})
        # Separate from the tool budget on purpose: tool calls and model calls are
        # different resources with different failure modes, and sharing one
        # allowance would let a long tool loop silently starve generation.
        self._model_budget_manager = model_budget_manager
        self.model_budget_limits = dict(model_budget_limits or {})
        if parallel_candidates < 1 or parallel_candidates > 3:
            raise ValueError("parallel_candidates must be between 1 and 3")
        self.parallel_candidates = parallel_candidates
        self.parallel_max_preview = parallel_max_preview
        self.parallel_preview_limit = parallel_preview_limit
        self.parallel_preview_timeout_seconds = parallel_preview_timeout_seconds
        self.selector_weights = selector_weights
        self.event_emitter = event_emitter
        self.report_requested = report_requested
        self.report_output_dir = report_output_dir or config.report_output_dir
        self.report_max_rows = report_max_rows or config.report_max_rows
        self.report_max_charts = report_max_charts or config.report_max_charts
        self.cancel_check = cancel_check
        # Data-domain identity of this run, stamped onto persisted history and
        # vector documents so scoped storage can be filtered by domain.
        self.history_domain_id = history_domain_id
        self.history_data_version = history_data_version
        self.retrieval_scope = dict(retrieval_scope or {})

    @classmethod
    def describe_workflow(cls) -> str:
        lines = [
            "QueryForge governed analytics workflow:",
            "  transports -> AgentService -> Router -> Orchestrator",
            "  analysis -> candidate -> execution -> completion -> delivery",
            "  analysis: scope, semantic context, schema plan",
            "  candidate: SQL generation, AST governance, bounded preview",
            "  execution: DatabaseTool read-only query",
            "  completion: data QA, optional visualization/report, operational artifact",
            "  extensions: Tool Loop, parallel candidates, plan approval, sessions, streaming",
            "SQL execution kernel:",
        ]
        lines.extend(f"  {name}: {description}" for name, description in cls.NODE_DESCRIPTIONS)
        return "\n".join(lines)

    def run(self, task: SqlTask) -> dict:
        ensure_logging_configured()
        run_id = self.run_id_factory()
        started = time.perf_counter()
        # One span recorder per run: model, tool, sql, retrieval and step spans
        # all aggregate into the same run-level usage/latency summary.
        recorder = start_span_recorder(run_id)
        with run_logging_context(run_id):
            LOGGER.info(
                "run_start question=%s provider=%s model=%s",
                task.question,
                self.config.llm_provider,
                self.config.llm_model,
            )
            try:
                output, context = self._run_inner(task, run_id)
            except WorkflowCancelled:
                # A cancelled run is not a failed run: no failure summary, no
                # ``failed`` status anywhere, and the run is never retried.
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                LOGGER.warning(
                    "run_cancelled run_id=%s duration_ms=%s", run_id, duration_ms
                )
                recorder.close()
                raise
            except Exception as exc:
                context = getattr(exc, "context", None)
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                summary = self._build_summary(
                    context=context,
                    question=task.question,
                    status="failed",
                    duration_ms=duration_ms,
                    error=str(exc),
                    run_id=run_id,
                    recorder=recorder,
                )
                LOGGER.error("run_summary %s", json.dumps(summary, ensure_ascii=False))
                recorder.close()
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            summary = self._build_summary(
                context=context,
                question=task.question,
                status=str(output.get("status") or "success"),
                duration_ms=duration_ms,
                run_id=run_id,
                recorder=recorder,
            )
            LOGGER.info("run_summary %s", json.dumps(summary, ensure_ascii=False))
            recorder.close()
            if self.show_run_summary:
                output["run_summary"] = summary
            return output

    def _run_inner(self, task: SqlTask, run_id: str) -> tuple[dict, Context]:
        recorder = get_span_recorder(run_id)
        with self._retrieval_span(recorder, "reference_examples", question=task.question):
            reference_examples = ReferenceSqlTool(task.database_path).find_similar(
                task.question
            )
        context = Context(
            task=task,
            run_id=run_id,
            selected_provider=self.config.llm_provider,
            selected_model=self.config.llm_model,
            reference_examples=reference_examples,
            vector_kb_enabled=self.enable_vector_kb,
            vector_kb_status="active" if self.enable_vector_kb else "disabled",
            vector_write_status="not_attempted" if self.enable_vector_kb else "disabled",
            event_emitter=self.event_emitter,
            report_requested=self.report_requested,
            report_output_dir=self.report_output_dir,
            report_max_rows=self.report_max_rows,
            report_max_charts=self.report_max_charts,
        )
        # Publish the retrieval scope so governed retrieval is filtered in the
        # *production* path and not only when a caller sets a node kwarg: an
        # unset scope means "no domain filter", which would let another domain's
        # definitions compete for the context window (step 13, 13-I1).
        retrieval_scope = self._retrieval_scope()
        if retrieval_scope:
            context.task_context["retrieval_scope"] = retrieval_scope
        # Unified run identity + versions, populated once here so downstream
        # consumers (artifacts, evidence, recovery) read one object instead of
        # reconstructing identity and versions from whichever layer is asking.
        context.run_context = RunContext.from_context(
            context,
            domain_id=self.history_domain_id,
            data_version=self.history_data_version,
        )
        if self.initial_sql:
            context.sql_context = SQLContext(
                sql=self.initial_sql,
                explanation="User-provided SQL used as the initial troubleshooting candidate.",
                tables_used=[],
            )
        connector = SQLiteConnector(task.database_path)
        with ExitStack() as connector_scope, connector:
            # Cancellation reaches the SQL boundary: an in-flight statement is
            # interrupted as soon as the streaming client disconnects, and the
            # watcher/progress handler is stopped when the run leaves this scope.
            connector_scope.callback(
                install_cancellation_handler(connector, self.cancel_check)
            )
            observed_connector = ObservedConnector(connector, run_id, recorder)
            sql_policy, policy_source_path = load_sql_policy(self.sql_policy_path)
            database_tool = DatabaseTool(
                observed_connector,
                sql_policy,
                policy_source_path=policy_source_path,
            )
            context.sql_policy = database_tool.policy_summary
            context.task_context["sql_dialect"] = database_tool.dialect
            raw_llm = self.llm_factory(self.config)
            observed_llm = ObservedModelProvider(
                raw_llm,
                provider_name=self.config.llm_provider,
                model_name=self.config.llm_model,
                debug_prompts=self.debug_prompts,
                trace_dir=self.trace_dir,
            )
            # Every model call on this path is charged to one shared budget, and
            # the run's remaining deadline reaches the adapter (see
            # workflow/budgeted_model.py). Previously only the tool loop and the
            # planner had a budget, so /ask model spend was unbounded.
            model_budget = self.model_budget_manager()
            budget_refusal: dict[str, Any] = {}
            llm = BudgetedModelProvider(
                observed_llm, model_budget, refusal_sink=budget_refusal
            )
            context.model_budget = model_budget
            context.budget_refusal = budget_refusal
            vector_store = self.vector_store
            if self.enable_vector_kb and vector_store is None:
                try:
                    vector_store = LanceDBVectorStore(
                        self.config.vector_kb_path,
                        embedding_provider=OpenAIEmbeddingProvider(
                            self.config.embedding_api_key,
                            self.config.embedding_model,
                            self.config.embedding_base_url,
                        ),
                    )
                except Exception as exc:
                    context.vector_kb_status = "degraded"
                    context.vector_kb_error = str(exc)
                    context.vector_write_status = "failed"
                    LOGGER.warning("vector_kb_initialization_failed error=%s", exc)
            if vector_store is not None and self.vector_top_k > 0:
                try:
                    with self._retrieval_span(
                        recorder, "vector_sql", top_k=self.vector_top_k
                    ) as span:
                        matches = list(
                            vector_store.search(
                                task.question,
                                top_k=self.vector_top_k,
                                source_types=SQL_SOURCE_TYPES,
                            )
                        )
                        if span is not None:
                            span.attributes["match_count"] = len(matches)
                        context.vector_sql_matches = [
                            VectorMatch.model_validate(match.to_dict())
                            for match in matches
                        ]
                except Exception as exc:
                    context.vector_kb_status = "degraded"
                    context.vector_kb_error = str(exc)
                    LOGGER.warning("vector_kb_search_failed error=%s", exc)
            history_store = self.history_store
            if history_store is None:
                try:
                    history_store = SQLHistoryStore(self.config.history_db_path)
                except Exception as exc:
                    context.history_error = str(exc)
                    context.history_write_status = "failed"
                    LOGGER.warning("history_initialization_failed error=%s", exc)
            if history_store is not None and self.history_top_k > 0:
                try:
                    with self._retrieval_span(
                        recorder, "sql_history", top_k=self.history_top_k
                    ) as span:
                        # History rows are written with the run's domain/data
                        # version, so the reader must query with the *same*
                        # resolved scope: an unscoped search injected another
                        # domain's SQL verbatim into the generation prompt.
                        # ``trusted_only`` keeps prompt-injected few-shot
                        # examples to human-reviewed rows, because a successful
                        # execution does not prove business correctness.
                        search = history_store.search_with_evidence(
                            task.question,
                            top_k=self.history_top_k,
                            domain_id=retrieval_scope.get("domain_id"),
                            data_version=retrieval_scope.get("data_version"),
                            trusted_only=True,
                        )
                        if span is not None:
                            span.attributes["match_count"] = len(search.matches)
                        context.history_matches = list(search.matches)
                        context.task_context["history_retrieval"] = search.evidence
                except Exception as exc:
                    context.history_error = str(exc)
                    context.history_write_status = "failed"
                    LOGGER.warning("history_search_failed error=%s", exc)
            setup_nodes = [
                SubjectSelectionNode(
                    enabled=self.subject_tree_enabled,
                    subject_tree_path=self.subject_tree_path,
                    requested_subject=self.subject,
                    default_subject=self.default_subject,
                ),
                DateParserNode(
                    llm=llm,
                    enable_llm_fallback=self.date_llm_fallback,
                    today_provider=self.today_provider,
                ),
                SchemaLinkingNode(
                    database_tool,
                    vector_store,
                    self.vector_top_k,
                    self.semantic_model_path,
                ),
                SkillSelectionNode(llm, self.skill_manager, self.selected_skills),
                MetricSearchNode(),
            ]
            plan_node = None
            if self.plan_mode or self.plan_only:
                plan_node = PlanModeNode(
                    auto_approve=self.auto_approve_plan,
                    approver=self.plan_approver,
                    presenter=self.plan_presenter,
                    approval_required=not self.plan_only,
                )
            output = ReflectiveWorkflow(
                context=context,
                setup_nodes=setup_nodes,
                gen_sql_node=GenSqlNode(llm),
                parallel_candidates_node=(
                    ParallelCandidatesNode(
                        llm,
                        database_tool,
                        candidate_count=self.parallel_candidates,
                        max_preview=self.parallel_max_preview,
                        preview_limit=self.parallel_preview_limit,
                        preview_timeout_seconds=self.parallel_preview_timeout_seconds,
                        selector_weights=self.selector_weights,
                    )
                    if self.parallel_candidates > 1
                    else None
                ),
                tool_loop_node=(
                    ToolLoopNode(
                        llm,
                        database_tool,
                        max_rounds=self.tool_loop_max_rounds,
                        timeout_seconds=self.tool_loop_timeout_seconds,
                        preview_limit=self.tool_loop_preview_limit,
                        budget_manager=self._tool_budget_manager(),
                        # plan_only must not run generated SQL or previews.
                        mode="plan_only" if self.plan_only else "execute",
                    )
                    if self.tool_loop_enabled
                    else None
                ),
                plan_node=plan_node,
                execute_sql_node=ExecuteSqlNode(database_tool),
                reflect_node=ReflectNode(llm, self.skill_manager),
                fix_node=FixNode(llm, self.skill_manager),
                output_node=OutputNode(
                    history_store,
                    vector_store,
                    domain_id=self.history_domain_id,
                    data_version=self.history_data_version,
                ),
                visualization_node=(
                    VisualizationNode(self.chart_output_dir)
                    if self.visualize and self.chart_output_dir
                    else VisualizationNode()
                    if self.visualize
                    else None
                ),
                plan_output_node=(
                    PlanOutputNode(database_tool) if self.plan_only else None
                ),
                max_retries=self.max_retries,
                analysis_hook=self.analysis_hook,
                candidate_hook=(
                    (lambda active_context: self.candidate_hook(active_context, database_tool))
                    if self.candidate_hook is not None
                    else None
                ),
                cancel_check=self.cancel_check,
            ).run()
            if self.completion_hook is not None:
                try:
                    self.completion_hook(context)
                except Exception as exc:
                    raise WorkflowError(
                        "agent_completion", str(exc), context
                    ) from exc
            return output, context

    def model_budget_manager(self) -> BudgetManager:
        """The per-run budget that bounds every model call."""

        if self._model_budget_manager is None:
            self._model_budget_manager = BudgetManager(
                limits=self.model_budget_limits or None
            )
        return self._model_budget_manager

    def _tool_budget_manager(self) -> BudgetManager:
        """Return the per-run budget manager used by the bounded tool loop."""

        if self.tool_budget_manager is None:
            self.tool_budget_manager = BudgetManager(limits=self.tool_budget_limits)
        return self.tool_budget_manager

    def _retrieval_scope(self) -> dict[str, Any]:
        """The governed retrieval scope this run must filter by (step 13).

        An explicit ``retrieval_scope`` wins; otherwise the scope is derived from
        the data domain the run was bound to. Every value stays optional: a run
        with no domain (a local, single-database deployment) publishes nothing and
        therefore keeps the previous, unfiltered behaviour instead of filtering on
        an empty string.
        """

        scope: dict[str, Any] = {}
        for key, value in self.retrieval_scope.items():
            if isinstance(value, str):
                if value.strip():
                    scope[key] = value.strip()
            elif isinstance(value, (list, tuple)):
                cleaned = [str(item).strip() for item in value if str(item).strip()]
                if cleaned:
                    scope[key] = cleaned
        scope.setdefault("domain_id", self.history_domain_id)
        scope.setdefault("data_version", self.history_data_version)
        return {
            key: value
            for key, value in scope.items()
            if value not in (None, "", [], ())
        }

    @staticmethod
    @contextmanager
    def _retrieval_span(
        recorder: SpanRecorder | None, name: str, **attributes: object
    ):
        """Record a ``retrieval`` span, or a no-op when tracing is unavailable."""

        if recorder is None:
            yield None
            return
        with recorder.span(
            f"retrieval.{name}", "retrieval", attributes=dict(attributes)
        ) as span:
            yield span

    def _build_summary(
        self,
        *,
        context: Context | None,
        question: str,
        status: str,
        duration_ms: float,
        run_id: str,
        error: str | None = None,
        recorder: SpanRecorder | None = None,
    ) -> dict:
        node_results = context.node_results if context is not None else []
        summary = {
            "run_id": run_id,
            "question": question,
            "workflow_nodes": [
                {
                    "name": result.node_name,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                }
                for result in node_results
            ],
            "selected_model": {
                "provider": self.config.llm_provider,
                "model": self.config.llm_model,
            },
            "duration_ms": duration_ms,
            "retries": context.retry_count if context is not None else 0,
            "history_matches": len(context.history_matches) if context is not None else 0,
            "vector_sql_matches": (
                len(context.vector_sql_matches) if context is not None else 0
            ),
            "vector_schema_matches": (
                len(context.vector_schema_matches) if context is not None else 0
            ),
            "output_status": status,
            "error": error,
        }
        if recorder is not None:
            # Step 14: token usage (measured or explicitly estimated) plus the
            # end-to-end and per-kind latency breakdown of this run.
            summary["usage"] = recorder.usage_summary()
            latency = recorder.latency_summary(end_to_end_ms=duration_ms)
            summary["latency"] = latency
            summary["spans"] = {
                "total": latency["span_count"],
                "by_kind": {
                    kind: entry["count"]
                    for kind, entry in latency["by_kind"].items()
                    if entry["count"]
                },
            }
        return summary
