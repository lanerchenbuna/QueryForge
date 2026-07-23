"""Build QueryForge's final serializable result."""

import logging

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.infrastructure.storage import KnowledgeBaseBuilder, SQLHistoryStore, VectorStore


LOGGER = logging.getLogger("queryforge.output")


class OutputNode(Node):
    name = "output"
    description = "Build a serializable response from workflow context"

    def __init__(
        self,
        history_store: SQLHistoryStore | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.history_store = history_store
        self.vector_store = vector_store

    def execute(self, context: Context) -> NodeResult:
        sql_context = context.sql_context
        execution = context.execution_result
        if sql_context is None or execution is None:
            return self.failure("SQL generation or execution result is missing")
        if self.history_store is not None:
            try:
                entry_id, inserted = self.history_store.add(
                    question=context.task.question,
                    sql=sql_context.sql,
                    explanation=sql_context.explanation,
                    tables_used=sql_context.tables_used,
                    success=True,
                    row_count=execution.row_count,
                    provider=context.selected_provider,
                    model=context.selected_model,
                    metadata={
                        "retry_count": context.retry_count,
                        "reflection_reason": (
                            context.reflection_result.reason
                            if context.reflection_result
                            else None
                        ),
                        "metrics": [
                            match.metric.name for match in context.metric_matches
                        ],
                    },
                    source="query",
                )
                context.history_entry_id = entry_id
                context.history_write_status = (
                    "inserted" if inserted else "duplicate"
                )
            except Exception as exc:
                context.history_write_status = "failed"
                context.history_error = str(exc)
                LOGGER.warning("history_write_failed error=%s", exc)

        if self.vector_store is not None:
            try:
                self.vector_store.add_documents(
                    [
                        KnowledgeBaseBuilder.successful_query_document(
                            question=context.task.question,
                            sql_context=sql_context,
                            history_id=context.history_entry_id,
                        )
                    ]
                )
                context.vector_write_status = "inserted"
            except Exception as exc:
                context.vector_write_status = "failed"
                context.vector_kb_status = "degraded"
                context.vector_kb_error = str(exc)
                LOGGER.warning("vector_kb_write_failed error=%s", exc)

        context.final_output = {
            "status": "success",
            "run_id": context.run_id,
            "question": context.task.question,
            "model_provider": context.selected_provider,
            "model": context.selected_model,
            "skills_used": context.loaded_skill_names,
            "skill_selection": {
                "mode": context.skill_selection_mode,
                "reason": context.skill_selection_reason,
            },
            "date_context": (
                context.date_context.model_dump() if context.date_context else None
            ),
            "plan": (
                {
                    **context.execution_plan.model_dump(mode="json"),
                    "approved": context.plan_approved,
                }
                if context.execution_plan
                else None
            ),
            "reflection": (
                context.reflection_result.model_dump()
                if context.reflection_result
                else None
            ),
            "retry_count": context.retry_count,
            "execution_errors": context.execution_errors,
            "fix_attempts": [
                attempt.model_dump() for attempt in context.fix_attempts
            ],
            "sql_attempt_history": [
                attempt.model_dump() for attempt in context.sql_attempt_history
            ],
            "history_matches": [
                match.model_dump() for match in context.history_matches
            ],
            "history_write": {
                "status": context.history_write_status,
                "entry_id": context.history_entry_id,
                "error": context.history_error,
            },
            "vector_kb": {
                "enabled": context.vector_kb_enabled,
                "status": context.vector_kb_status,
                "error": context.vector_kb_error,
                "write_status": context.vector_write_status,
                "sql_matches": [
                    match.model_dump() for match in context.vector_sql_matches
                ],
                "schema_matches": [
                    match.model_dump() for match in context.vector_schema_matches
                ],
            },
            "relevant_tables": [
                schema.table_name for schema in context.relevant_tables
            ],
            "subject": (
                context.subject_selection.model_dump(mode="json")
                if context.subject_selection
                else None
            ),
            "sql": sql_context.sql,
            "explanation": sql_context.explanation,
            "tables_used": sql_context.tables_used,
            "columns": execution.columns,
            "rows": execution.rows,
            "row_count": execution.row_count,
            "sql_execution_duration_ms": context.sql_execution_duration_ms,
            "sql_security": {
                **context.sql_policy,
                "decisions": [
                    decision.model_dump()
                    for decision in context.sql_policy_decisions
                ],
            },
            "tool_loop": {
                "status": context.tool_loop_status,
                "exit_reason": context.tool_loop_exit_reason,
                "rounds": len(context.tool_loop_history),
                "history": context.tool_loop_history,
            },
            "candidate_selection": context.candidate_selection,
            "reasoning": (
                context.reasoning_result.model_dump(mode="json")
                if context.reasoning_result
                else None
            ),
            "reasoning_validation": context.reasoning_validation,
        }
        if context.semantic_model:
            context.final_output["semantic_model"] = {
                "status": "active",
                "name": context.semantic_model.model.name,
                "version": context.semantic_model.model.version,
                "source_path": context.semantic_model.source_path,
                "matches": [
                    match.model_dump() for match in context.semantic_model.matches
                ],
            }
            context.final_output["metric_search"] = {
                "status": "matched" if context.metric_matches else "no_match",
                "matches": [
                    match.model_dump() for match in context.metric_matches
                ],
                "requested_dimensions": context.metric_requested_dimensions,
                "join_paths": [
                    path.model_dump() for path in context.metric_join_paths
                ],
            }
        return self.success("Final output assembled")
