"""Database facade protected by a shared SQL AST security policy."""

from __future__ import annotations

import re

import sqlglot

from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import ExecutionResult, SqlPolicyDecision, TableSchema
from queryforge.domain.security import (
    SQLPolicyEngine,
    SQLPolicyViolation,
    SQLSecurityPolicy,
)


class UnsafeSQLError(ValueError):
    """Raised before execution when SQL violates a named policy rule."""

    def __init__(
        self, message: str, decision: SqlPolicyDecision | None = None
    ) -> None:
        self.decision = decision
        super().__init__(message)


class DatabaseTool:
    """Expose policy-filtered schema and AST-guarded query execution."""

    def __init__(
        self,
        connector: SQLiteConnector,
        policy: SQLSecurityPolicy | None = None,
        *,
        policy_source_path: str | None = None,
    ) -> None:
        self.connector = connector
        raw_schemas = [
            connector.describe_table(table) for table in connector.list_tables()
        ]
        self.policy_engine = SQLPolicyEngine(
            policy or SQLSecurityPolicy(),
            raw_schemas,
            source_path=policy_source_path,
        )
        self.last_policy_decision: SqlPolicyDecision | None = None

    @property
    def policy_summary(self) -> dict:
        return self.policy_engine.summary

    def list_tables(self) -> list[str]:
        return self.policy_engine.allowed_table_names()

    def describe_table(self, table_name: str) -> TableSchema:
        try:
            return self.policy_engine.filter_schema(
                self.connector.describe_table(table_name)
            )
        except SQLPolicyViolation as exc:
            self.last_policy_decision = exc.decision
            raise UnsafeSQLError(str(exc), exc.decision) from exc

    def describe_table_for_validation(self, table_name: str) -> TableSchema:
        """Return full metadata internally, but only for an authorized table."""
        if table_name not in self.list_tables():
            raise UnsafeSQLError(
                f"SQL security policy denied schema validation for {table_name!r}"
            )
        return self.connector.describe_table(table_name)

    def find_matching_values(
        self,
        table_name: str,
        column_name: str,
        keywords: list[str],
        limit: int = 3,
    ) -> list[str]:
        schema = self.describe_table(table_name)
        if column_name not in {column.name for column in schema.columns}:
            raise UnsafeSQLError(
                f"SQL security policy denied value sampling for "
                f"{table_name}.{column_name}"
            )
        return self.connector.find_matching_values(
            table_name, column_name, keywords, limit
        )

    def execute_sql(self, sql: str) -> ExecutionResult:
        try:
            decision = self.policy_engine.evaluate(sql)
        except SQLPolicyViolation as exc:
            self.last_policy_decision = exc.decision
            raise UnsafeSQLError(str(exc), exc.decision) from exc
        self.last_policy_decision = decision
        return self.connector.execute_sql(sql.strip())

    def execute_sql_preview(self, sql: str, limit: int = 20) -> ExecutionResult:
        """Execute a bounded read-only preview through the same policy engine."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("preview limit must be a positive integer")
        bounded_limit = min(limit, 100)
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
            if tree is None or not tree.find(sqlglot.exp.Select):
                raise UnsafeSQLError("preview requires a SELECT query")
            if tree.find(sqlglot.exp.Limit) is None:
                tree = tree.limit(bounded_limit)
            else:
                existing = tree.find(sqlglot.exp.Limit)
                literal = existing.expression
                current = int(literal.this) if literal and literal.is_int else bounded_limit
                existing.set("expression", sqlglot.exp.Literal.number(min(current, bounded_limit)))
            bounded_sql = tree.sql(dialect="sqlite")
        except UnsafeSQLError:
            raise
        except Exception as exc:
            raise UnsafeSQLError(f"preview SQL could not be parsed: {exc}") from exc
        return self.execute_sql(bounded_sql)

    def preview_distinct_values(
        self,
        table_name: str,
        column_name: str,
        limit: int = 20,
    ) -> list[object]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("preview limit must be a positive integer")
        schema = self.describe_table(table_name)
        if column_name not in {column.name for column in schema.columns}:
            raise UnsafeSQLError(
                f"SQL security policy denied value sampling for "
                f"{table_name}.{column_name}"
            )
        safe_table = self._quote_identifier(table_name)
        safe_column = self._quote_identifier(column_name)
        result = self.execute_sql_preview(
            f"SELECT DISTINCT {safe_column} FROM {safe_table}",
            min(limit, 100),
        )
        return [row[0] for row in result.rows]

    @staticmethod
    def _quote_identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise UnsafeSQLError("invalid SQL identifier")
        return f'"{value}"'

    @staticmethod
    def validate_readonly_sql(sql: str) -> str:
        """Compatibility API now backed by the same SQLGlot AST checks."""
        if not isinstance(sql, str) or not sql.strip():
            raise UnsafeSQLError(
                "SQL_SECURITY_ERROR run_id=- rule=ast_parse: empty SQL query"
            )
        engine = SQLPolicyEngine(SQLSecurityPolicy(), [], audit=False)
        try:
            engine.evaluate(sql)
        except SQLPolicyViolation as exc:
            raise UnsafeSQLError(str(exc), exc.decision) from exc
        return sql.strip()
