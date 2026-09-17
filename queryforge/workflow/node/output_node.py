"""Build QueryForge's final serializable result."""

import logging
from typing import Any

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.analysis.evidence import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_TRUNCATED,
    Evidence,
    EvidenceStore,
    FinalAnswer,
    KIND_SQL_RESULT,
    apply_validation,
    build_execution_evidence,
    load_evidence_store,
    summarize_completeness,
    validate_answer,
)
from queryforge.domain.analysis.request import detect_time_grain
from queryforge.infrastructure.storage import KnowledgeBaseBuilder, SQLHistoryStore, VectorStore


LOGGER = logging.getLogger("queryforge.output")


class OutputNode(Node):
    name = "output"
    description = "Build a serializable response from workflow context"

    def __init__(
        self,
        history_store: SQLHistoryStore | None = None,
        vector_store: VectorStore | None = None,
        *,
        domain_id: str | None = None,
        data_version: str | None = None,
    ) -> None:
        self.history_store = history_store
        self.vector_store = vector_store
        # Data-domain scope of this run; stamped on history and vector documents
        # so unscoped legacy records stay distinguishable from scoped ones.
        self.domain_id = domain_id
        self.data_version = data_version

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
                        # Always present: a None domain_id marks a legacy,
                        # unscoped history row that scoped search must exclude.
                        "domain_id": self.domain_id,
                        "data_version": self.data_version,
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
                            domain_id=self.domain_id,
                            data_version=self.data_version,
                        )
                    ]
                )
                context.vector_write_status = "inserted"
            except Exception as exc:
                context.vector_write_status = "failed"
                context.vector_kb_status = "degraded"
                context.vector_kb_error = str(exc)
                LOGGER.warning("vector_kb_write_failed error=%s", exc)

        evidence_layer = self._evidence_layer(context)
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
            "task_evidence": _task_evidence(context),
            # Step 12 evidence layer: every key number traceable to its source.
            "evidence": evidence_layer["evidence"],
            "evidence_issues": evidence_layer["evidence_issues"],
            "final_answer": evidence_layer["final_answer"],
            "answer_validation": evidence_layer["answer_validation"],
            "completeness": evidence_layer["completeness"],
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

    # -- step 12: evidence layer ------------------------------------------
    def _evidence_layer(self, context: Context) -> dict[str, Any]:
        """Expose evidence, the composed answer and the completeness split.

        Never raises: a run must keep producing its query result even when the
        evidence payload is unusable, but the reason is always reported.
        """
        task_context = context.task_context if isinstance(context.task_context, dict) else {}
        issues: list[str] = []
        store, error = load_evidence_store(task_context.get("evidence"))
        if error:
            issues.append(f"evidence payload was rejected: {error}")
        sql_context = context.sql_context
        execution = context.execution_result
        if (
            execution is not None
            and sql_context is not None
            and not _has_result_evidence(store, sql_context.sql)
        ):
            try:
                store.add(self._result_evidence(context, execution, sql_context))
            except Exception as exc:
                issues.append(
                    f"result evidence could not be recorded: {type(exc).__name__}: {exc}"
                )

        answer = _final_answer(task_context.get("final_answer"), store, issues)
        problems: list[str] = []
        if answer is not None:
            problems = validate_answer(answer, store)
            if problems:
                answer = apply_validation(answer, problems)

        row_count = execution.row_count if execution is not None else 0
        returned = len(execution.rows) if execution is not None else 0
        display_limit = context.report_max_rows if context.report_max_rows > 0 else row_count
        json_note = (
            "The JSON 'rows' list in this response is not truncated."
            if returned >= row_count
            else f"The JSON 'rows' list already holds only {returned} of {row_count} reported rows."
        )
        completeness = summarize_completeness(
            total_row_count=row_count,
            displayed_row_count=min(row_count, display_limit),
            evidence=store.all(),
            answer=answer,
            extra_notes=[
                f"Display truncation follows the report's report_max_rows={display_limit}. "
                f"{json_note}",
                *issues,
            ],
        )
        return {
            "evidence": store.to_list(),
            "evidence_issues": issues,
            "final_answer": answer.model_dump(mode="json") if answer is not None else None,
            "answer_validation": {
                "problems": problems,
                "review_required": bool(answer.review_required) if answer else False,
            },
            "completeness": completeness,
        }

    def _result_evidence(self, context: Context, execution: Any, sql_context: Any) -> Evidence:
        """Seed the run's own result as evidence so numbers are traceable."""
        task_context = context.task_context if isinstance(context.task_context, dict) else {}
        request = task_context.get("analysis_request")
        request = request if isinstance(request, dict) else {}
        grain = (
            request.get("time_grain")
            or detect_time_grain(context.task.question)
        )
        returned = len(execution.rows)
        completeness = (
            COMPLETENESS_COMPLETE
            if execution.row_count == returned
            else COMPLETENESS_TRUNCATED
        )
        if task_context.get("result_truncated") is True:
            completeness = COMPLETENESS_TRUNCATED
        return build_execution_evidence(
            sql=sql_context.sql,
            source=context.task.database_path,
            columns=execution.columns,
            rows=execution.rows,
            row_count=execution.row_count,
            version=self.data_version,
            grain=grain,
            range_=_resolved_range(context, request),
            completeness=completeness,
            method="executed SQL over the run's data version (aggregates computed over the "
            "complete returned result set)",
            kind=KIND_SQL_RESULT,
        )


def _has_result_evidence(store: EvidenceStore, sql: str) -> bool:
    wanted = (sql or "").strip()
    for item in store:
        if item.kind == KIND_SQL_RESULT and (item.sql or "").strip() == wanted:
            return True
    return False


def _final_answer(
    payload: Any,
    store: EvidenceStore,
    issues: list[str],
) -> FinalAnswer | None:
    """Validate a composed answer supplied by the pipeline (never invent one)."""
    if payload is None:
        return None
    try:
        if isinstance(payload, FinalAnswer):
            return payload.model_copy(deep=True)
        return FinalAnswer.model_validate(payload)
    except Exception as exc:
        issues.append(
            f"final answer payload was rejected: {type(exc).__name__}: {exc}"
        )
        return None


def _resolved_range(context: Context, request: dict[str, Any]) -> dict[str, Any] | None:
    ranges = context.date_context.ranges if context.date_context else []
    if ranges:
        first = ranges[0]
        return {
            "start": first.start_date,
            "end": ranges[-1].end_date,
            "expression": first.expression,
        }
    time_range = request.get("time_range")
    return {"expression": str(time_range)} if time_range else None


def _task_evidence(context: Context) -> dict[str, Any]:
    """Summarize step 04-09 task evidence for audit without dumping raw dumps.

    The full structures remain on ``context.task_context`` / run artifacts;
    the response carries the compact, decision-relevant slice.
    """
    task_context = context.task_context or {}
    evidence: dict[str, Any] = {"keys": sorted(task_context)}
    for key in (
        "schema_retrieval",
        # How many history rows few-shot retrieval considered, under which scope,
        # and with which governance state: without it a run cannot show whether
        # the domain/trust filter actually ran (step 13).
        "history_retrieval",
        "semantic_validation",
        "date_window",
        "data_quality",
    ):
        value = task_context.get(key)
        if value is not None:
            evidence[key] = value
    categories = task_context.get("error_categories")
    if categories:
        evidence["error_categories"] = list(categories)
    calls = task_context.get("tool_calls") or []
    if calls:
        evidence["tool_calls"] = {
            "count": len(calls),
            "last": calls[-5:],
        }
    signatures = task_context.get("attempt_signatures")
    if signatures:
        evidence["attempt_count"] = len(signatures)
    return evidence
