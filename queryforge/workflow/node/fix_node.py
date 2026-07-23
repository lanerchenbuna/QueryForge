"""Repair a failed or locally incorrect SQLite query with the configured LLM."""

from __future__ import annotations

import json
import logging

from queryforge.workflow.node.base import Node
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import Context, FixAttempt, NodeResult, SQLContext
from queryforge.domain.skills import SkillManager
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


LOGGER = logging.getLogger("queryforge.sql")


class FixNode(Node):
    name = "fix"
    description = "Generate one guarded read-only replacement SQL query"

    def __init__(self, llm: BaseModelProvider, skill_manager: SkillManager) -> None:
        self.llm = llm
        self.skill_manager = skill_manager

    def execute(self, context: Context) -> NodeResult:
        if context.sql_context is None:
            return self.failure("No SQL is available to fix")
        original = context.sql_context
        trigger = self._trigger(context)
        try:
            payload = self.llm.generate_json(self._build_prompt(context, trigger))
            fixed_sql = payload.get("fixed_sql")
            explanation = payload.get("explanation")
            if not isinstance(fixed_sql, str) or not fixed_sql.strip():
                return self.failure("Fix response must contain non-empty 'fixed_sql'")
            if not isinstance(explanation, str) or not explanation.strip():
                return self.failure("Fix response must contain non-empty 'explanation'")
            try:
                clean_sql = DatabaseTool.validate_readonly_sql(fixed_sql)
            except UnsafeSQLError as exc:
                return self.failure(f"Fixed SQL violates read-only policy: {exc}")
            if clean_sql.strip() == original.sql.strip():
                return self.failure("Fix response repeated the previous SQL unchanged")
            raw_tables = payload.get("tables_used")
            tables_used = (
                raw_tables
                if isinstance(raw_tables, list)
                and all(isinstance(table, str) for table in raw_tables)
                else original.tables_used
            )
            context.sql_context = SQLContext(
                sql=clean_sql,
                explanation=explanation.strip(),
                tables_used=tables_used,
                reasoning_result=context.reasoning_result,
            )
            if context.reasoning_result:
                context.reasoning_validation = GenSqlNode._validate_reasoning(
                    clean_sql,
                    context.reasoning_result,
                )
                context.sql_context.reasoning_validation = context.reasoning_validation
            context.fix_attempts.append(
                FixAttempt(
                    retry_number=context.retry_count,
                    trigger=trigger,
                    original_sql=original.sql,
                    fixed_sql=clean_sql,
                    explanation=explanation.strip(),
                )
            )
            LOGGER.info(
                "sql_fix retry=%s trigger=%s original_sql=%s fixed_sql=%s",
                context.retry_count,
                trigger,
                original.sql,
                clean_sql,
            )
            context.execution_result = None
            context.reflection_result = None
        except ModelResponseError as exc:
            return self.failure(
                f"Fix response is not valid JSON: {exc}; "
                f"raw_output={exc.raw_output[:1000]!r}"
            )
        except Exception as exc:
            return self.failure(f"Could not fix SQL: {exc}")
        return self.success(f"Generated fixed SQL for retry {context.retry_count}")

    @staticmethod
    def _trigger(context: Context) -> str:
        if context.last_execution_error:
            return context.last_execution_error
        if context.reflection_result:
            parts = [context.reflection_result.reason]
            if context.reflection_result.suggested_fix:
                parts.append(context.reflection_result.suggested_fix)
            return " | ".join(parts)
        return "The previous SQL requires a localized correction."

    def _build_prompt(self, context: Context, trigger: str) -> str:
        assert context.sql_context is not None
        schemas = [
            {
                "table_name": schema.table_name,
                "columns": [column.model_dump() for column in schema.columns[:100]],
                "foreign_keys": [
                    foreign_key.model_dump() for foreign_key in schema.foreign_keys
                ],
            }
            for schema in context.relevant_tables[:50]
        ]
        skills = self.skill_manager.context_for_node(
            "fix", context.loaded_skill_names
        ).loaded_skills
        history = [attempt.model_dump() for attempt in context.sql_attempt_history[-3:]]
        date_context = context.date_context.model_dump() if context.date_context else {}
        metric_matches = [match.model_dump() for match in context.metric_matches]
        metric_join_paths = [
            path.model_dump() for path in context.metric_join_paths
        ]
        return f"""Repair the SQLite query using the error or reflection feedback.

Rules:
- Return exactly one JSON object with fields fixed_sql, explanation, and tables_used.
- fixed_sql must be exactly one read-only SQLite SELECT or WITH ... SELECT query.
- Never generate DDL, DML, PRAGMA, multiple statements, or invented identifiers.
- Preserve correct parts of the original query and make the smallest reliable correction.
- Use the resolved date context and loaded Skill constraints when applicable.
- Preserve matched structured metric expressions, default filters, allowed dimensions,
  and time_field semantics.
- Preserve the exact resolved Join Path steps for cross-entity dimensions. Never add an
  undeclared fact join or a cardinality step that fans out the metric base grain.
- Do not repeat a SQL attempt already shown in the history unless the feedback requires it.

User question:
{context.task.question}

Original SQL:
{context.sql_context.sql}

Original explanation:
{context.sql_context.explanation}

Execution error or reflection feedback:
{trigger}

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

Structured reasoning summary to preserve when repairing:
{json.dumps(
    context.reasoning_result.model_dump(mode="json")
    if context.reasoning_result else None,
    ensure_ascii=False,
    indent=2,
)}

Loaded fix skills:
{skills}

Recent SQL attempt history:
{json.dumps(history, ensure_ascii=False, indent=2)}
"""
