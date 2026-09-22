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
            "reasoning_discarded": context.reasoning_discarded,
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
            # The bound is now visible to callers: without it a capped result was
            # indistinguishable from a complete one.
            "truncated": execution.truncated,
            "fetched_row_count": execution.fetched_row_count or execution.row_count,
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
            "candidate_strategy": context.candidate_strategy,
            "reasoning": (
                context.reasoning_result.model_dump(mode="json")
                if context.reasoning_result
                else None
            ),
            "reasoning_validation": context.reasoning_validation,
            "task_evidence": _task_evidence(context),
            # First-class, not buried in task_evidence: a semantic verdict of
            # ``unsupported`` means "this SQL was never proved to answer the
            # question". It used to reach only a log line, so a run whose business
            # semantics were never checked was delivered exactly like one that
            # passed every check. `verified` states that difference explicitly.
            "semantic_validation": _semantic_validation_summary(context),
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

        # Compose an answer when the pipeline did not supply one (E-10). The
        # conversational path never wrote ``final_answer``, so a run delivered
        # evidence and a completeness marker but no statement of what the result
        # says — ``final_answer`` was always None here. The composer derives every
        # number from the evidence store, so this adds no new source of truth.
        if task_context.get("final_answer") is None and execution is not None:
            composed = _compose_answer(context, store, execution, sql_context)
            if composed is not None:
                task_context["final_answer"] = composed.model_dump(mode="json")
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
        semantic = _semantic_validation_summary(context)
        completeness_notes = [
            f"Display truncation follows the report's report_max_rows={display_limit}. "
            f"{json_note}",
            *issues,
        ]
        if not semantic["verified"]:
            # Stated in the completeness record as well as in its own field, so a
            # consumer reading only one of the two still sees that the business
            # semantics were not proved.
            completeness_notes.append(f"Business semantics: {semantic['reason']}")
        completeness = summarize_completeness(
            total_row_count=row_count,
            displayed_row_count=min(row_count, display_limit),
            evidence=store.all(),
            answer=answer,
            extra_notes=completeness_notes,
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
        if task_context.get("result_truncated") is True or execution.truncated:
            completeness = COMPLETENESS_TRUNCATED
        # The bound is stated in the evidence method so the reader sees that the
        # aggregate was computed over a capped row set.
        method = (
            "executed SQL over the run's data version (aggregates computed over the "
            "complete returned result set)"
        )
        if execution.truncated:
            method = (
                f"executed SQL over the run's data version; the adapter's row bound "
                f"returned {execution.row_count} of {execution.fetched_row_count} "
                f"rows, so aggregates cover only the returned rows"
            )
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
            method=method,
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


def _compose_answer(
    context: Context,
    store: EvidenceStore,
    execution: Any,
    sql_context: Any,
) -> FinalAnswer | None:
    """Build a :class:`FinalAnswer` from the run's own evidence.

    Every number is resolved against the evidence payloads, so a value that cannot
    be found becomes ``None`` and is reported as unknown rather than invented. A
    failure here is recorded as an issue and never fails the run: the query result
    is the primary product.
    """

    from queryforge.domain.analysis.evidence import AnswerComposer, Finding

    try:
        result_evidence_id = next(
            (
                item.id
                for item in reversed(list(store))
                if item.kind == KIND_SQL_RESULT
                and (item.sql or "").strip() == (sql_context.sql or "").strip()
            ),
            None,
        )
        if result_evidence_id is None:
            return None
        columns = list(execution.columns or [])
        row_count = int(execution.row_count or 0)
        numbers: dict[str, Any] = {"row_count": row_count}
        if row_count == 1 and len(columns) == 1:
            # Name the single scalar so the answer states it. The key must be one
            # the evidence payload resolves to a *number*: a bare column name
            # resolves to that column's aggregate dict, which the composer reports
            # as unknown, whereas the evidence also exposes a flat ``total_<column>``
            # scalar. Declared as None so the composer takes the evidence value as
            # the only source of truth.
            numbers[f"total_{columns[0]}"] = None
        finding = Finding(
            kind=KIND_SQL_RESULT,
            statement=(
                f"The query returned {row_count} row(s) over columns "
                f"{', '.join(columns) or '<none>'}."
            ),
            numbers=numbers,
            evidence_ids=[result_evidence_id],
        )
        composer = AnswerComposer(store)
        composed = composer.compose(context.task.question, [finding])
        problems = validate_answer(composed, store)
        return apply_validation(composed, problems) if problems else composed
    except Exception:
        # Reported by the caller through the completeness notes; an answer layer
        # problem must not cost the caller the result.
        return None


def _semantic_validation_summary(context: Context) -> dict[str, Any]:
    """Report whether the business semantics of this SQL were actually proved.

    Three distinct situations must not look alike:

    * ``passed`` / ``violation`` — the governed validator reached a verdict;
    * ``unsupported`` — the validator could not prove anything (an unlisted SQL
      shape, an unparsable statement, or a question that matched no governed
      metric). Execution continues under SQL policy, but "not disproved" is not
      "proved", and the answer has to say so;
    * no verdict at all — no semantic model, or no governed metric matched, so the
      semantic gate never ran. This is the common case for a follow-up that does
      not restate a metric.

    ``verified`` is true only in the first group, so a consumer can gate on one
    boolean instead of re-deriving it from whichever field happens to be present.
    """

    raw = context.task_context.get("semantic_validation")
    if not isinstance(raw, dict):
        return {
            "status": "not_run",
            "verified": False,
            "reason": (
                "No semantic verdict was produced for this run: the question matched "
                "no governed metric, or no semantic model was in scope. Table scope "
                "and the SQL policy still applied; business semantics were not checked."
            ),
            "rules": [],
        }
    status = str(raw.get("status") or "unsupported")
    verified = status == "passed"
    if status == "unsupported":
        reason = str(
            raw.get("unsupported_reason")
            or "the governed validator could not prove this SQL answers the question"
        )
    elif status == "violation":
        reason = "The governed validator rejected this SQL."
    else:
        reason = "The governed validator proved this SQL answers the question."
    return {
        "status": status,
        "verified": verified,
        "reason": reason,
        "rules": list(raw.get("rule_names") or []),
    }


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
