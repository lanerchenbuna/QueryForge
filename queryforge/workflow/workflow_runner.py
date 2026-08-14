"""Build and run QueryForge's bounded reflective SQL workflow."""

from __future__ import annotations

from datetime import date
import json
import logging
import time
from typing import Callable

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
from queryforge.workflow.event_emitter import EventEmitter
from queryforge.workflow.workflow import ReflectiveWorkflow, WorkflowError
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.core.observability import (
    ObservedModelProvider,
    ensure_logging_configured,
    new_run_id,
    run_logging_context,
)
from queryforge.core.schemas.models import Context, SQLContext, SqlTask, VectorMatch
from queryforge.domain.security import load_sql_policy
from queryforge.domain.skills.manager import SkillManager
from queryforge.infrastructure.storage import (
    LanceDBVectorStore,
    OpenAIEmbeddingProvider,
    SQLHistoryStore,
    SQL_SOURCE_TYPES,
    VectorStore,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.infrastructure.tools.reference_sql_tool import ReferenceSqlTool


LOGGER = logging.getLogger("queryforge.runner")


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
        with run_logging_context(run_id):
            LOGGER.info(
                "run_start question=%s provider=%s model=%s",
                task.question,
                self.config.llm_provider,
                self.config.llm_model,
            )
            try:
                output, context = self._run_inner(task, run_id)
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
                )
                LOGGER.error("run_summary %s", json.dumps(summary, ensure_ascii=False))
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            summary = self._build_summary(
                context=context,
                question=task.question,
                status=str(output.get("status") or "success"),
                duration_ms=duration_ms,
                run_id=run_id,
            )
            LOGGER.info("run_summary %s", json.dumps(summary, ensure_ascii=False))
            if self.show_run_summary:
                output["run_summary"] = summary
            return output

    def _run_inner(self, task: SqlTask, run_id: str) -> tuple[dict, Context]:
        context = Context(
            task=task,
            run_id=run_id,
            selected_provider=self.config.llm_provider,
            selected_model=self.config.llm_model,
            reference_examples=ReferenceSqlTool(task.database_path).find_similar(
                task.question
            ),
            vector_kb_enabled=self.enable_vector_kb,
            vector_kb_status="active" if self.enable_vector_kb else "disabled",
            vector_write_status="not_attempted" if self.enable_vector_kb else "disabled",
            event_emitter=self.event_emitter,
            report_requested=self.report_requested,
            report_output_dir=self.report_output_dir,
            report_max_rows=self.report_max_rows,
            report_max_charts=self.report_max_charts,
        )
        if self.initial_sql:
            context.sql_context = SQLContext(
                sql=self.initial_sql,
                explanation="User-provided SQL used as the initial troubleshooting candidate.",
                tables_used=[],
            )
        with SQLiteConnector(task.database_path) as connector:
            sql_policy, policy_source_path = load_sql_policy(self.sql_policy_path)
            database_tool = DatabaseTool(
                connector,
                sql_policy,
                policy_source_path=policy_source_path,
            )
            context.sql_policy = database_tool.policy_summary
            raw_llm = self.llm_factory(self.config)
            llm = ObservedModelProvider(
                raw_llm,
                provider_name=self.config.llm_provider,
                model_name=self.config.llm_model,
                debug_prompts=self.debug_prompts,
                trace_dir=self.trace_dir,
            )
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
                    context.vector_sql_matches = [
                        VectorMatch.model_validate(match.to_dict())
                        for match in vector_store.search(
                            task.question,
                            top_k=self.vector_top_k,
                            source_types=SQL_SOURCE_TYPES,
                        )
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
                    context.history_matches = history_store.search(
                        task.question, top_k=self.history_top_k
                    )
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
                    )
                    if self.tool_loop_enabled
                    else None
                ),
                plan_node=plan_node,
                execute_sql_node=ExecuteSqlNode(database_tool),
                reflect_node=ReflectNode(llm, self.skill_manager),
                fix_node=FixNode(llm, self.skill_manager),
                output_node=OutputNode(history_store, vector_store),
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

    def _build_summary(
        self,
        *,
        context: Context | None,
        question: str,
        status: str,
        duration_ms: float,
        run_id: str,
        error: str | None = None,
    ) -> dict:
        node_results = context.node_results if context is not None else []
        return {
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
