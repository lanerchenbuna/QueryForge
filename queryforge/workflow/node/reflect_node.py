"""Evaluate whether generated SQL and its result answer the user question."""

from __future__ import annotations

import json

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import Context, NodeResult, ReflectionResult
from queryforge.domain.skills import SkillManager


class ReflectNode(Node):
    name = "reflect"
    description = "Assess SQL correctness and choose the next workflow strategy"

    def __init__(self, llm: BaseModelProvider, skill_manager: SkillManager) -> None:
        self.llm = llm
        self.skill_manager = skill_manager

    def execute(self, context: Context) -> NodeResult:
        if context.sql_context is None or context.execution_result is None:
            return self.failure("SQL and an execution result are required for reflection")
        prompt = self._build_prompt(context)
        try:
            payload = self.llm.generate_json(prompt)
            if isinstance(payload.get("strategy"), str):
                payload["strategy"] = payload["strategy"].strip().upper()
            reflection = ReflectionResult.model_validate(payload)
            if reflection.success != (reflection.strategy == "SUCCESS"):
                return self.failure(
                    "Reflection field 'success' must be true only for strategy SUCCESS"
                )
            context.reflection_result = reflection
        except ModelResponseError as exc:
            return self.failure(
                f"Reflection response is not valid JSON: {exc}; "
                f"raw_output={exc.raw_output[:1000]!r}"
            )
        except Exception as exc:
            return self.failure(f"Could not reflect on SQL result: {exc}")
        return self.success(
            f"Reflection strategy={context.reflection_result.strategy}"
        )

    def _build_prompt(self, context: Context) -> str:
        assert context.sql_context is not None
        assert context.execution_result is not None
        schemas = [
            {
                "table_name": schema.table_name,
                "columns": [column.model_dump() for column in schema.columns[:80]],
                "foreign_keys": [
                    foreign_key.model_dump() for foreign_key in schema.foreign_keys
                ],
            }
            for schema in context.relevant_tables[:40]
        ]
        execution = context.execution_result
        result_summary = {
            "columns": execution.columns,
            "row_count": execution.row_count,
            "sample_rows": execution.rows[:20],
            "sample_truncated": execution.row_count > 20,
        }
        skills = self.skill_manager.context_for_node(
            "reflect", context.loaded_skill_names
        ).loaded_skills
        date_context = context.date_context.model_dump() if context.date_context else {}
        metric_matches = [match.model_dump() for match in context.metric_matches]
        metric_join_paths = [
            path.model_dump() for path in context.metric_join_paths
        ]
        return f"""Evaluate whether the SQL and result plausibly answer the user question.

Choose exactly one strategy:
- SUCCESS: SQL semantics and result are acceptable.
- FIX_SQL: a localized SQL correction can address the issue.
- REGENERATE: the SQL approach substantially misses the question and should be regenerated.
- NEED_USER_REVIEW: ambiguity or missing business meaning prevents a reliable decision.

Rules:
- Successful execution alone does not prove semantic correctness.
- An empty result can be valid; do not reject it without schema, filter, or join evidence.
- Check requested metric, grain, joins, filters, dates, ordering, limits, and columns.
- If structured metrics are matched, verify that SQL preserves their aggregation
  expressions, every default filter, allowed grouping dimensions, and time_field.
- Verify that every cross-entity metric dimension follows the supplied Join Path exactly.
  Reject extra joins or reversed one-to-many steps that multiply the metric base grain.
- Do not invent facts not visible in the supplied context or sample.
- Return only one JSON object with this shape:
  {{"success": true, "strategy": "SUCCESS", "reason": "...", "suggested_fix": null}}
- success must be true only when strategy is SUCCESS.

User question:
{context.task.question}

Generated SQL:
{context.sql_context.sql}

SQL explanation:
{context.sql_context.explanation}

Execution result summary:
{json.dumps(result_summary, ensure_ascii=False, indent=2)}

Recent execution errors from earlier attempts (may be empty):
{json.dumps(context.execution_errors[-3:], ensure_ascii=False, indent=2)}

Available schema:
{json.dumps(schemas, ensure_ascii=False, indent=2)}

Resolved date context:
{json.dumps(date_context, ensure_ascii=False, indent=2)}

Matched structured metrics (authoritative; may be empty):
{json.dumps(metric_matches, ensure_ascii=False, indent=2)}

Validated requested metric group dimensions:
{json.dumps(context.metric_requested_dimensions, ensure_ascii=False, indent=2)}

Resolved safe Join Paths and Cardinality Contracts:
{json.dumps(metric_join_paths, ensure_ascii=False, indent=2)}

Structured reasoning summary (audit data, not hidden chain-of-thought):
{json.dumps(
    context.reasoning_result.model_dump(mode="json")
    if context.reasoning_result else None,
    ensure_ascii=False,
    indent=2,
)}

Reasoning versus SQL validation:
{json.dumps(context.reasoning_validation, ensure_ascii=False, indent=2)}

Loaded reflection skills:
{skills}
"""
