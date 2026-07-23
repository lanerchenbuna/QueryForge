"""Top-level coordinator for QueryForge's integrated Agent Team workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from queryforge.orchestration.orchestrator.pipeline_registry import pipeline_for
from queryforge.workflow.event_emitter import emit_event
from queryforge.orchestration.agents.data_qa import DataQAAgent
from queryforge.orchestration.agents.governance import GovernanceAgent
from queryforge.orchestration.agents.knowledge import KnowledgeAgent
from queryforge.orchestration.agents.ops import OpsAgent
from queryforge.orchestration.agents.product_analyst import ProductAnalystAgent
from queryforge.orchestration.agents.report import ReportAgent
from queryforge.orchestration.agents.schema_architect import SchemaArchitectAgent
from queryforge.orchestration.agents.sql_review import SQLReviewAgent
from queryforge.orchestration.agents.sql_developer import SQLDeveloperAgent
from queryforge.orchestration.agents.visualization import VisualizationAgent
from queryforge.orchestration.quality import append_warning
from queryforge.orchestration.gates import QualityGateEvaluator
from queryforge.orchestration.runtime.session_store import SessionStore
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import DeliveryReport, RoutingDecision, TaskState, utc_now
from queryforge.orchestration.schemas.session import SessionMemory, SessionTurn
from queryforge.core.schemas.models import Context
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


AnalysisHook = Callable[[Context], None]
CandidateHook = Callable[[Context, DatabaseTool], None]
CompletionHook = Callable[[Context], None]
WorkflowRun = Callable[[AnalysisHook, CandidateHook, CompletionHook], dict[str, Any]]
DirectRun = Callable[[TaskState, "OrchestratorAgent"], dict[str, Any]]


class OrchestratorAgent:
    """Coordinate role agents at explicit lifecycle points in WorkflowRunner."""

    def __init__(self, state_store: AgentTeamStateStore | None = None) -> None:
        self.state_store = state_store or AgentTeamStateStore()
        self.product_analyst = ProductAnalystAgent(self.state_store)
        self.report = ReportAgent(self.state_store)
        self.knowledge = KnowledgeAgent(self.state_store)
        self.schema_architect = SchemaArchitectAgent(self.state_store)
        self.sql_developer = SQLDeveloperAgent(self.state_store)
        self.sql_review = SQLReviewAgent(self.state_store)
        self.governance = GovernanceAgent(self.state_store)
        self.data_qa = DataQAAgent(self.state_store)
        self.visualization = VisualizationAgent(self.state_store)
        self.ops = OpsAgent(self.state_store)
        self.gates = QualityGateEvaluator(self.state_store)

    def run(
        self,
        *,
        run_id: str,
        decision: RoutingDecision,
        workflow: WorkflowRun,
        direct_run: DirectRun | None = None,
        plan_only: bool = False,
        session_memory: SessionMemory | None = None,
        session_store: SessionStore | None = None,
        original_question: str | None = None,
        rewritten_question: str | None = None,
        followup_reason: str | None = None,
    ) -> dict[str, Any]:
        pipeline = pipeline_for(decision.task_type)
        if plan_only:
            pipeline = tuple(
                phase
                for phase in pipeline
                if phase != "execution"
            )
        state = TaskState(
            run_id=run_id,
            entrypoint=decision.entrypoint,
            classification=decision,
            status="running",
            current_phase="routing",
            pending_phases=list(pipeline),
            session_id=session_memory.session_id if session_memory else None,
            original_question=original_question,
            rewritten_question=rewritten_question,
            is_followup=bool(followup_reason),
            followup_reason=followup_reason,
        )
        run_dir = self.state_store.initialize(state)
        emit_event(
            self.state_store.event_emitter,
            "phase_started",
            run_id,
            phase_name="routing",
            status="running",
            message="Started routing.",
        )

        self.state_store.write_artifact(
            state,
            artifact_type="routing_decision",
            producer="EntryRouterAgent",
            status="valid",
            payload=decision.model_dump(mode="json"),
        )
        if session_memory is not None:
            self.state_store.write_artifact(
                state,
                artifact_type="conversation_context",
                producer="OrchestratorAgent",
                status="valid",
                payload={
                    "session_id": session_memory.session_id,
                    "turn_count": session_memory.turn_count,
                    "is_followup": state.is_followup,
                    "original_question": original_question,
                    "rewritten_question": state.rewritten_question,
                    "followup_reason": followup_reason,
                },
            )
        self._complete_phase(state, "routing")
        state.current_phase = "workflow"
        self.state_store.save_state(state)
        try:
            if direct_run is not None:
                result = direct_run(state, self)
            else:
                result = workflow(
                    self._analysis_hook(state),
                    self._candidate_hook(state),
                    self._completion_hook(state),
                )
        except Exception as exc:
            context = getattr(exc, "context", None)
            node_name = getattr(exc, "node_name", "")
            emit_event(
                self.state_store.event_emitter,
                "node_failed",
                run_id,
                node_name=node_name or "orchestrator",
                status="failed",
                message="Workflow stopped before completion.",
            )
            if node_name == "execute_sql" and context is not None:
                self.gates.block(
                    state,
                    phase="execution",
                    reason=str(exc),
                    context=context,
                )
                result = context.final_output or {
                    "status": "blocked",
                    "run_id": run_id,
                    "reason": str(exc),
                }
            elif state.status == "blocked":
                state.last_error = str(exc)
                state.finished_at = utc_now()
                report = DeliveryReport(
                    run_id=run_id,
                    task_id=state.task_id,
                    task_type=decision.task_type,
                    status="degraded",
                    pipeline=list(pipeline),
                    artifact_refs=list(state.artifacts),
                    summary="The workflow was blocked before SQL execution.",
                )
                self._write_delivery(state, report)
                self._record_session_turn(
                    state,
                    session_memory,
                    session_store,
                    context=context,
                    result={"status": "blocked"},
                )
                self.state_store.save_state(state)
                raise
            else:
                state.status = "failed"
                state.current_phase = "failed"
                state.last_error = str(exc)
                state.finished_at = utc_now()
                report = DeliveryReport(
                    run_id=run_id,
                    task_id=state.task_id,
                    task_type=decision.task_type,
                    status="failed",
                    pipeline=list(pipeline),
                    artifact_refs=list(state.artifacts),
                    summary="The integrated Agent Team workflow failed.",
                )
                self._write_delivery(state, report)
                self._record_session_turn(
                    state,
                    session_memory,
                    session_store,
                    context=context,
                    result={"status": "failed"},
                )
                self.state_store.save_state(state)
                raise

        result_status = str(result.get("status") or "success")
        if result_status == "blocked":
            state.status = "blocked"
            state.current_phase = state.blocked_phase or "blocked"
        else:
            state.status = "completed"
            state.current_phase = "delivery"
        self._start_phase(state, "delivery")
        self._complete_phase(state, "delivery")
        state.pending_phases = []
        state.workflow_run_id = str(result.get("run_id") or run_id)
        state.finished_at = utc_now()
        report = DeliveryReport(
            run_id=run_id,
            task_id=state.task_id,
            task_type=decision.task_type,
            status=(
                "degraded"
                if result_status == "blocked"
                else
                result_status
                if result_status in {"planned", "success", "degraded", "failed"}
                else "success"
            ),
            pipeline=list(pipeline),
            artifact_refs=list(state.artifacts),
            summary="The request completed through the integrated Agent Team workflow.",
        )
        self._write_delivery(state, report)
        session = self._record_session_turn(
            state,
            session_memory,
            session_store,
            result=result,
        )
        self.state_store.save_state(state)

        output = dict(result)
        output["agent_team"] = self._team_metadata(state, report, run_dir)
        output["delivery_report"] = report.model_dump(mode="json", exclude={"result"})
        if session is not None:
            output["session"] = session
        return output

    def _analysis_hook(self, state: TaskState) -> AnalysisHook:
        def hook(context: Context) -> None:
            state.current_phase = "analysis"
            self._start_phase(state, "analysis")
            if (
                context.subject_selection is not None
                and not self._has_artifact(state, "subject_selection")
            ):
                self.state_store.write_artifact(
                    state,
                    artifact_type="subject_selection",
                    producer="SubjectSelectionNode",
                    status=(
                        "warning"
                        if context.subject_selection.status == "fallback_all"
                        else "valid"
                    ),
                    payload=context.subject_selection.model_dump(mode="json"),
                )
            if self._phase_expected(state, "analysis") and not self._has_artifact(
                state, "analysis_request"
            ):
                self.product_analyst.run(state, context)
            if self._phase_expected(state, "analysis") and not self._has_artifact(
                state, "knowledge_context"
            ):
                self.knowledge.run(state, context)
            if self._phase_expected(state, "analysis") and not self._has_artifact(
                state, "schema_plan"
            ):
                self.schema_architect.run(state, context)
            self._check_gate(state, "analysis", context)
            self._complete_phase(state, "analysis")
            self.state_store.save_state(state)

        return hook

    def _candidate_hook(self, state: TaskState) -> CandidateHook:
        def hook(context: Context, database_tool: DatabaseTool) -> None:
            if not self._phase_configured(state, "candidate"):
                return
            self._start_phase(state, "candidate")
            if context.tool_loop_history and not self._has_artifact(state, "tool_loop_trace"):
                self.state_store.write_artifact(
                    state,
                    artifact_type="tool_loop_trace",
                    producer="ToolLoopNode",
                    status="valid" if context.tool_loop_status == "completed" else "degraded",
                    payload={
                        "status": context.tool_loop_status,
                        "exit_reason": context.tool_loop_exit_reason,
                        "rounds": len(context.tool_loop_history),
                        "history": context.tool_loop_history,
                    },
                )
            if context.candidate_selection and not self._has_artifact(
                state, "candidate_selection"
            ):
                self.state_store.write_artifact(
                    state,
                    artifact_type="candidate_selection",
                    producer="SQLSelector",
                    status=(
                        "valid"
                        if context.candidate_selection.get("selected_index") is not None
                        else "blocked"
                    ),
                    payload=context.candidate_selection,
                )
            state.current_phase = "candidate"
            self.sql_developer.run(state, context)
            try:
                self.governance.run(state, context, database_tool)
            except UnsafeSQLError as exc:
                state.last_error = str(exc)
                self._check_gate(state, "sql_candidate", context)
                self._complete_phase(state, "candidate")
                self.state_store.save_state(state)
                raise
            except Exception as exc:
                state.last_error = str(exc)
                self._complete_phase(state, "candidate")
                if self._check_gate(state, "sql_candidate", context):
                    self.state_store.save_state(state)
                    return
                raise
            self._complete_phase(state, "candidate")
            if self._check_gate(state, "sql_candidate", context):
                self.state_store.save_state(state)
                return
            self.state_store.save_state(state)

        return hook

    def _completion_hook(self, state: TaskState) -> CompletionHook:
        def hook(context: Context) -> None:
            if self._phase_expected(state, "execution"):
                self._start_phase(state, "execution")
            if context.execution_result is not None:
                self._check_gate(state, "execution", context)
            if self._phase_expected(state, "execution"):
                self._complete_phase(state, "execution")
            if not self._phase_expected(state, "completion"):
                self.state_store.save_state(state)
                return
            self._start_phase(state, "completion")
            if context.execution_result is not None:
                if self._phase_expected(state, "completion"):
                    self.data_qa.run(state, context)
                    self._check_gate(state, "qa", context)
                if state.classification.task_type in {"ask_sql", "build_report"}:
                    self.visualization.run(state, context)
                    self._check_gate(state, "visualization", context)
                if state.classification.task_type == "explain_result":
                    self._write_explain_artifact(state, context)
                if context.report_requested:
                    self.report.run(state, context)
            self.ops.run(state)
            self._check_gate(state, "ops", context)
            self._complete_phase(state, "completion")
            self.state_store.save_state(state)

        return hook

    @staticmethod
    def _has_artifact(state: TaskState, artifact_type: str) -> bool:
        return any(
            artifact.artifact_type == artifact_type for artifact in state.artifacts
        )

    @staticmethod
    def _phase_expected(state: TaskState, phase: str) -> bool:
        return phase in state.pending_phases

    @staticmethod
    def _phase_configured(state: TaskState, phase: str) -> bool:
        return phase in state.pending_phases or phase in state.completed_phases

    def _start_phase(self, state: TaskState, phase: str) -> None:
        emit_event(
            self.state_store.event_emitter,
            "phase_started",
            state.run_id,
            phase_name=phase,
            status="running",
            message=f"Started {phase}.",
        )

    def start_phase(self, state: TaskState, phase: str) -> None:
        """Start an explicit stage for direct application use cases."""
        self._start_phase(state, phase)

    def _complete_phase(self, state: TaskState, phase: str) -> None:
        if phase not in state.completed_phases:
            state.completed_phases.append(phase)
        if phase in state.pending_phases:
            state.pending_phases.remove(phase)
        emit_event(
            self.state_store.event_emitter,
            "phase_completed",
            state.run_id,
            phase_name=phase,
            status="success",
            message=f"Completed {phase}.",
        )

    def complete_phase(self, state: TaskState, phase: str) -> None:
        """Complete an explicit stage and update persisted pending state."""
        self._complete_phase(state, phase)

    def _write_delivery(self, state: TaskState, report: DeliveryReport) -> None:
        reference = self.state_store.write_artifact(
            state,
            artifact_type="delivery_report",
            producer="OrchestratorAgent",
            status="valid" if report.status in {"planned", "success"} else "degraded",
            payload=report.model_dump(mode="json", exclude={"artifact_refs", "result"}),
        )
        report.artifact_refs.append(reference)

    def _record_session_turn(
        self,
        state: TaskState,
        memory: SessionMemory | None,
        store: SessionStore | None,
        *,
        result: dict[str, Any],
        context: Context | None = None,
    ) -> dict[str, Any] | None:
        if memory is None or store is None:
            return None
        raw_status = str(result.get("status") or "failed")
        status = (
            raw_status
            if raw_status in {"success", "planned", "blocked", "failed"}
            else "failed"
        )
        analysis = next(
            (
                artifact.get("payload") or {}
                for artifact in self._artifact_documents(state)
                if artifact.get("artifact_type") == "analysis_request"
            ),
            {},
        )
        sql = result.get("sql")
        if not sql and context and context.sql_context:
            sql = context.sql_context.sql
        filters = [
            item if isinstance(item, dict) else {"expression": str(item)}
            for item in analysis.get("filters", [])
        ]
        turn = SessionTurn(
            turn_number=memory.turn_count + 1,
            question=state.original_question or str(result.get("question") or ""),
            rewritten_question=state.rewritten_question,
            sql=str(sql) if sql else None,
            metrics=list(analysis.get("metrics") or []),
            dimensions=list(analysis.get("dimensions") or []),
            filters=filters,
            time_range=analysis.get("time_range"),
            result_schema=list(result.get("columns") or []),
            status=status,
        )
        memory.turn_count = turn.turn_number
        memory.history.append(turn)
        memory.history = memory.history[-10:]
        if status in {"success", "planned"} and turn.sql:
            memory.last_question = state.rewritten_question or turn.question
            memory.last_sql = turn.sql
            memory.last_result_schema = turn.result_schema
            memory.last_metrics = turn.metrics
            memory.last_dimensions = turn.dimensions
            memory.last_filters = turn.filters
            memory.last_time_range = turn.time_range
        try:
            path = store.save(memory)
        except Exception as exc:
            append_warning(
                state,
                phase="session",
                artifact_type="conversation_context",
                producer="OrchestratorAgent",
                reason=f"Session persistence failed: {exc}",
            )
            return {
                "session_id": memory.session_id,
                "turn_count": memory.turn_count,
                "status": "degraded",
                "error": str(exc),
            }
        return {
            "session_id": memory.session_id,
            "turn_count": memory.turn_count,
            "is_followup": state.is_followup,
            "original_question": state.original_question,
            "rewritten_question": state.rewritten_question,
            "path": str(path),
        }

    def _apply_analysis_decision(self, state: TaskState, context: Context) -> None:
        self._check_gate(state, "analysis", context)

    def _check_gate(
        self,
        state: TaskState,
        phase: str,
        context: Context | None = None,
    ) -> bool:
        return self.gates.evaluate(state, phase, context)

    def check_gate(
        self,
        state: TaskState,
        phase: str,
        context: Context | None = None,
    ) -> bool:
        """Evaluate one named quality gate for direct application use cases."""
        return self._check_gate(state, phase, context)

    def _artifact_documents(self, state: TaskState) -> list[dict[str, Any]]:
        return self.gates.documents(state)

    def _write_explain_artifact(self, state: TaskState, context: Context) -> None:
        reflection = context.reflection_result
        self.state_store.write_artifact(
            state,
            artifact_type="explanation_report",
            producer="OrchestratorAgent",
            status="valid",
            payload={
                "question": context.task.question,
                "sql": context.sql_context.sql if context.sql_context else None,
                "explanation": context.sql_context.explanation if context.sql_context else None,
                "columns": context.execution_result.columns if context.execution_result else [],
                "row_count": context.execution_result.row_count if context.execution_result else 0,
                "reflection": reflection.model_dump(mode="json") if reflection else None,
                "summary": reflection.reason if reflection else "No reflection result was available.",
            },
        )

    @staticmethod
    def _team_metadata(state: TaskState, report: DeliveryReport, run_dir: Any) -> dict:
        return {
            "entrypoint": state.entrypoint,
            "task_type": state.classification.task_type,
            "routing_confidence": state.classification.confidence,
            "routing_reason": state.classification.reason,
            "complexity_profile": state.classification.complexity_profile,
            "complexity_score": state.classification.complexity_score,
            "complexity_reasons": state.classification.complexity_reasons,
            "pipeline": report.pipeline,
            "state_path": str(run_dir / "state.json"),
            "artifacts_dir": str(run_dir / "artifacts"),
            "warnings": list(state.warnings),
            "blocked_phase": state.blocked_phase,
            "blocked_reason": state.blocked_reason,
        }
