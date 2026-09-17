"""Execute the generated query through the guarded database tool."""

import logging
import time

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.semantic import SemanticSQLValidator
from queryforge.workflow.errors import record_error_category
from queryforge.infrastructure.tools.database_tool import DatabaseTool


LOGGER = logging.getLogger("queryforge.sql")


class ExecuteSqlNode(Node):
    name = "execute_sql"
    description = "Execute generated SQL through the read-only database tool"

    def __init__(self, database_tool: DatabaseTool) -> None:
        self.database_tool = database_tool

    def execute(self, context: Context) -> NodeResult:
        if context.sql_context is None:
            return self.failure("No generated SQL is available")
        context.execution_result = None
        join_contract_error = self._validate_join_contract(context)
        if join_contract_error:
            record_error_category(context, join_contract_error)
            return self.failure(join_contract_error)
        started = time.perf_counter()
        try:
            context.execution_result = self.database_tool.execute_sql(
                context.sql_context.sql
            )
        except Exception as exc:
            self._capture_policy_decision(context)
            context.sql_execution_duration_ms = round(
                (time.perf_counter() - started) * 1000, 3
            )
            record_error_category(context, exc)
            LOGGER.error(
                "sql_execution duration_ms=%s success=false error=%s sql=%s",
                context.sql_execution_duration_ms,
                exc,
                context.sql_context.sql,
            )
            return self.failure(f"Could not execute generated SQL: {exc}")
        self._capture_policy_decision(context)
        context.sql_execution_duration_ms = round(
            (time.perf_counter() - started) * 1000, 3
        )
        LOGGER.info(
            "sql_execution duration_ms=%s success=true row_count=%s sql=%s",
            context.sql_execution_duration_ms,
            context.execution_result.row_count,
            context.sql_context.sql,
        )
        return self.success(
            f"Query returned {context.execution_result.row_count} row(s)"
        )

    def _capture_policy_decision(self, context: Context) -> None:
        decision = self.database_tool.last_policy_decision
        if decision is not None and (
            not context.sql_policy_decisions
            or context.sql_policy_decisions[-1] != decision
        ):
            context.sql_policy_decisions.append(decision)

    @staticmethod
    def _validate_join_contract(context: Context) -> str | None:
        """Reject business-semantic contract violations before running SQL.

        Returns ``None`` when the request is not governed (no semantic model or no
        matched metric). AST-level join/key/filter/grain/fan-out checks are owned
        by :class:`~queryforge.domain.semantic.SemanticSQLValidator`; shapes the
        validator cannot prove are recorded as ``unsupported`` and execution
        continues under governance.
        """
        if context.sql_context is None:
            return None
        validator = SemanticSQLValidator.for_context(context)
        if validator is None:
            return None
        result = validator.validate(context.sql_context.sql)
        context.task_context["semantic_validation"] = result.model_dump()
        if result.status == "violation":
            LOGGER.warning(
                "semantic_validation status=violation rules=%s sql=%s",
                ",".join(result.rule_names),
                context.sql_context.sql,
            )
            return result.error_message()
        if result.status == "unsupported":
            LOGGER.info(
                "semantic_validation status=unsupported reason=%s",
                result.unsupported_reason,
            )
        return None
