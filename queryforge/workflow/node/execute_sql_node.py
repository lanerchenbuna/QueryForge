"""Execute the generated query through the guarded database tool."""

import logging
import re
import time

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.semantic import SemanticModelLoader
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
        """Reject missing governed paths and unexpected grain-expanding joins."""
        if context.semantic_model is None or not context.metric_matches:
            return None
        physical_tables = {
            schema.table_name for schema in context.relevant_tables
        }
        used_tables = {
            table
            for table in re.findall(
                r'\b(?:from|join)\s+[`"\[]?([A-Za-z_][A-Za-z0-9_]*)',
                context.sql_context.sql,
                flags=re.IGNORECASE,
            )
            if table in physical_tables
        }
        required_tables = {
            table for path in context.metric_join_paths for table in path.tables
        }
        missing = sorted(required_tables - used_tables)
        if missing:
            return (
                "Join Path contract violation: SQL omitted required table(s) "
                + ", ".join(missing)
            )

        model = context.semantic_model.model
        entity_by_name = {entity.name: entity for entity in model.entities}
        entity_by_table = {entity.table: entity for entity in model.entities}
        for match in context.metric_matches:
            base_entity = entity_by_name.get(match.metric.entity)
            if base_entity is None:
                continue
            governed_tables = {base_entity.table} | {
                table
                for path in context.metric_join_paths
                if path.from_entity == match.metric.entity
                for table in path.tables
            }
            for table in sorted(used_tables - governed_tables):
                joined_entity = entity_by_table.get(table)
                if joined_entity is None:
                    continue
                diagnostic = SemanticModelLoader.resolve_join_path(
                    model,
                    match.metric.entity,
                    joined_entity.name,
                    include_undeclared=True,
                )
                if diagnostic is not None and not diagnostic.safe:
                    return (
                        f"Fan-out execution guard blocked metric "
                        f"{match.metric.name!r} from joining {table!r}: "
                        + "; ".join(diagnostic.fanout_steps)
                    )
        return None
