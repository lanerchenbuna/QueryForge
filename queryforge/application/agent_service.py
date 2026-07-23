"""Application service that maps every transport to the Agent Team workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Callable

from queryforge.workflow.event_emitter import EventEmitter, emit_event
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.orchestration.agents.entry_router import EntryRouterAgent
from queryforge.orchestration.agents.product_analyst import ProductAnalystAgent
from queryforge.orchestration.agents.sql_review import SQLReviewAgent
from queryforge.orchestration.orchestrator.orchestrator import OrchestratorAgent
from queryforge.orchestration.runtime.session_store import SessionStore
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.core.config import Config, load_config
from queryforge.infrastructure.models.base import BaseModelProvider
from queryforge.infrastructure.models.factory import ModelFactory
from queryforge.core.observability import new_run_id
from queryforge.core.schemas.models import SqlTask
from queryforge.application.event_stream import WorkflowEventStream
from queryforge.domain.skills import SkillRegistry
from queryforge.application.direct_tasks import DirectTaskExecutor
from queryforge.application.options import AgentOptions
from queryforge.application.resources import ResourceService
from queryforge.domain.semantic import discover_semantic_model


class AgentService(ResourceService):
    """Transport-neutral facade; it contains no SQL generation logic itself."""

    def __init__(
        self,
        *,
        config_loader: Callable[..., Config] = load_config,
        runner_factory: Callable[..., WorkflowRunner] = WorkflowRunner,
        llm_factory: Callable[[Config], BaseModelProvider] = ModelFactory.create,
        skill_registry: SkillRegistry | None = None,
        entry_router: EntryRouterAgent | None = None,
    ) -> None:
        self.config_loader = config_loader
        self.runner_factory = runner_factory
        self.llm_factory = llm_factory
        self.skill_registry = skill_registry or SkillRegistry()
        self.entry_router = entry_router or EntryRouterAgent()
        self.direct_tasks = DirectTaskExecutor()
        super().__init__(config_loader, self.skill_registry)

    def ask(
        self, question: str, options: AgentOptions | None = None
    ) -> dict:
        return self._run(question, options or AgentOptions(), plan_only=False)

    def plan(
        self, question: str, options: AgentOptions | None = None
    ) -> dict:
        return self._run(question, options or AgentOptions(), plan_only=True)

    def stream(
        self,
        question: str,
        options: AgentOptions | None = None,
    ) -> "WorkflowEventStream":
        """Run one ask request in a worker and expose progress-only events."""
        resolved_options = options or AgentOptions()
        config = self.config_loader(
            provider_override=resolved_options.model_provider,
            model_override=resolved_options.model,
        )
        if not config.streaming_enabled:
            raise ValueError("Streaming is disabled by configuration")
        run_id = resolved_options.run_id or new_run_id()
        resolved_options = replace(resolved_options, run_id=run_id)
        emitter = EventEmitter(config.streaming_event_buffer_size)
        stream = WorkflowEventStream(emitter)
        emitter.on_event(stream._publish)

        def worker() -> None:
            emit_event(
                emitter,
                "run_started",
                run_id,
                status="running",
                message="Started QueryForge workflow.",
            )
            try:
                stream.result = self._run(
                    question,
                    resolved_options,
                    plan_only=False,
                    event_emitter=emitter,
                )
                emit_event(
                    emitter,
                    "final_result",
                    run_id,
                    status=str(stream.result.get("status") or "success"),
                    message="QueryForge workflow completed.",
                )
            except Exception as exc:
                stream.error = exc
                emit_event(
                    emitter,
                    "final_result",
                    run_id,
                    status="failed",
                    message="QueryForge workflow failed.",
                )
            finally:
                stream._close()

        Thread(target=worker, name=f"queryforge-{run_id}", daemon=True).start()
        return stream

    def new_session(
        self,
        *,
        session_id: str | None = None,
        orchestration_state_root: str | None = None,
    ) -> dict:
        config = self.config_loader()
        options = AgentOptions(orchestration_state_root=orchestration_state_root)
        store = self._session_store(options, config)
        memory = store.create(session_id)
        path = store.save(memory)
        return {
            "session_id": memory.session_id,
            "path": str(path),
            "turn_count": memory.turn_count,
        }

    def reset_session(
        self,
        session_id: str,
        *,
        orchestration_state_root: str | None = None,
    ) -> dict:
        config = self.config_loader()
        options = AgentOptions(orchestration_state_root=orchestration_state_root)
        memory = self._session_store(options, config).reset(session_id)
        return {
            "session_id": memory.session_id,
            "turn_count": memory.turn_count,
            "status": "reset",
        }

    def _run(
        self,
        question: str,
        options: AgentOptions,
        *,
        plan_only: bool,
        event_emitter: EventEmitter | None = None,
    ) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("question must be non-empty")
        options.validate()
        config = self.config_loader(
            provider_override=options.model_provider,
            model_override=options.model,
        )
        options.validate_for_config(config)
        database = options.database or config.database_path
        path = Path(database).expanduser()
        if not path.is_file():
            raise ValueError(f"SQLite database does not exist: {database}")
        semantic_model_path = discover_semantic_model(
            path,
            options.semantic_model_path or config.semantic_model_path,
        )
        if (
            config.require_semantic_model
            and not options.allow_schema_only
            and semantic_model_path is None
        ):
            suggested_output = path.resolve().with_suffix(".semantic.yml")
            raise ValueError(
                "A validated semantic layer is required before this database can "
                "be queried. Build one with: "
                f"python scripts/build_semantic_model.py --database {path.resolve()} "
                f"--output {suggested_output}; review the generated draft/report, "
                "then retry. Use allow_schema_only only for explicit diagnostics."
            )
        options = replace(options, semantic_model_path=semantic_model_path)

        original_question = question
        session_store = None
        session_memory = None
        followup_reason = None
        rewritten_question = None
        if options.session_id or options.new_session:
            session_store = self._session_store(options, config)
            if options.new_session:
                session_memory = session_store.create()
            elif options.reset_session:
                session_memory = session_store.reset(options.session_id or "")
            else:
                session_memory = session_store.load_or_create(options.session_id or "")
            rewrite = ProductAnalystAgent.rewrite_followup(question, session_memory)
            if bool(rewrite["is_followup"]):
                question = str(rewrite["question"])
                rewritten_question = question
                followup_reason = str(rewrite["reason"])

        decision = self.entry_router.route(question, options.entrypoint)
        run_id = options.run_id or new_run_id()
        effective_complex = (
            options.complexity_mode == "complex"
            or (
                options.complexity_mode == "auto"
                and decision.complexity_profile == "complex"
            )
        )
        runner_kwargs = {
            "llm_factory": self.llm_factory,
            "selected_skills": options.skills,
            "date_llm_fallback": options.date_llm_fallback,
            "plan_mode": options.plan_mode,
            "auto_approve_plan": options.auto_approve_plan,
            "plan_approver": options.plan_approver,
            "plan_presenter": options.plan_presenter,
            "max_retries": options.max_retries,
            "history_top_k": options.history_top_k,
            "enable_vector_kb": options.enable_vector_kb,
            "vector_top_k": options.vector_top_k,
            "visualize": options.visualize,
            "chart_output_dir": options.chart_output_dir,
            "debug_prompts": options.debug_prompts,
            "show_run_summary": options.show_run_summary,
            "plan_only": plan_only,
            "semantic_model_path": options.semantic_model_path,
            "subject_tree_enabled": options.subject_tree_enabled,
            "subject_tree_path": options.subject_tree_path,
            "subject": options.subject,
            "default_subject": options.default_subject,
            "sql_policy_path": options.sql_policy_path,
            "tool_loop_enabled": options.tool_loop_enabled or effective_complex,
            "tool_loop_max_rounds": options.tool_loop_max_rounds,
            "tool_loop_timeout_seconds": options.tool_loop_timeout_seconds,
            "tool_loop_preview_limit": options.tool_loop_preview_limit,
            "parallel_candidates": max(
                options.parallel_candidates,
                2 if effective_complex else 1,
            ),
            "parallel_max_preview": options.parallel_max_preview,
            "parallel_preview_limit": options.parallel_preview_limit,
            "parallel_preview_timeout_seconds": options.parallel_preview_timeout_seconds,
            "selector_weights": options.selector_weights,
            "event_emitter": event_emitter,
            "report_requested": config.report_enabled and (
                options.report or decision.task_type == "build_report"
            ),
            "report_output_dir": options.report_output_dir,
            "report_max_rows": options.report_max_rows,
            "report_max_charts": options.report_max_charts,
        }
        runner_kwargs["run_id_factory"] = lambda: run_id
        task = SqlTask(question=question, database_path=str(path))
        if decision.task_type == "troubleshoot_sql":
            runner_kwargs["initial_sql"] = (
                options.provided_sql or SQLReviewAgent.extract_sql(question) or None
            )
        orchestrator = OrchestratorAgent(
            AgentTeamStateStore(
                options.orchestration_state_root or config.orchestration_state_root,
                event_emitter,
            )
        )

        def run_workflow(analysis_hook, candidate_hook, completion_hook):
            runner = self.runner_factory(
                config,
                **runner_kwargs,
                analysis_hook=analysis_hook,
                candidate_hook=candidate_hook,
                completion_hook=completion_hook,
            )
            return runner.run(task)

        direct_run = None
        if decision.task_type == "metadata_query":
            direct_run = lambda state, orch: self.direct_tasks.metadata(
                state, orch, task, config, options, run_id
            )
        elif decision.task_type == "sql_review":
            direct_run = lambda state, orch: self.direct_tasks.sql_review(
                state, orch, task, config, options, run_id
            )

        return orchestrator.run(
            run_id=run_id,
            decision=decision,
            workflow=run_workflow,
            direct_run=direct_run,
            plan_only=plan_only,
            session_memory=session_memory,
            session_store=session_store,
            original_question=original_question if session_memory else None,
            rewritten_question=rewritten_question,
            followup_reason=followup_reason,
        )

    @staticmethod
    def _session_store(options: AgentOptions, config: Config) -> SessionStore:
        runs_root = Path(
            options.orchestration_state_root or config.orchestration_state_root
        ).expanduser()
        return SessionStore(runs_root.parent / "sessions")
