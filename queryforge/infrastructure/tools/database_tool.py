"""Database facade protected by a shared SQL AST security policy."""

from __future__ import annotations

import re

import sqlglot

from queryforge.infrastructure.db.adapters import DatabaseAdapter
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
        connector: DatabaseAdapter,
        policy: SQLSecurityPolicy | None = None,
        *,
        policy_source_path: str | None = None,
    ) -> None:
        self.connector = connector
        self.dialect = getattr(connector, "dialect", "sqlite")
        raw_schemas = [
            connector.describe_table(table) for table in connector.list_tables()
        ]
        self.policy_engine = SQLPolicyEngine(
            policy or SQLSecurityPolicy(),
            raw_schemas,
            source_path=policy_source_path,
            dialect=self.dialect,
        )
        self._last_policy_decision: SqlPolicyDecision | None = None

    @property
    def last_policy_decision(self) -> SqlPolicyDecision | None:
        """The decision from the most recent call made through *this tool*.

        Read-only on purpose. It used to be a plain attribute, which two callers
        exploited: the planner node *assigned* a decision it had computed itself,
        so the tool reported an audit record for a call that never happened, and
        ``PlanOutputNode`` read it as a fallback for a decision it had just failed
        to obtain (E-23). Only :meth:`_record_policy_decision` mutates it now.

        It is still per-instance state, so it is only meaningful immediately after
        a call on this instance. Prefer the value returned by that call wherever a
        decision has to be attributed: shared tools (the planner's and the
        registry's) can be driven concurrently, and a decision read back later can
        belong to another caller's statement.
        """
        return self._last_policy_decision

    def _record_policy_decision(self, decision: SqlPolicyDecision | None) -> None:
        """Record the decision for the call currently in flight."""
        self._last_policy_decision = decision

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
            self._record_policy_decision(exc.decision)
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
            self._record_policy_decision(exc.decision)
            raise UnsafeSQLError(str(exc), exc.decision) from exc
        self._record_policy_decision(decision)
        return self.connector.execute_sql(sql.strip())

    def execute_sql_preview(self, sql: str, limit: int = 20) -> ExecutionResult:
        """Execute a bounded read-only preview through the same policy engine.

        The policy engine is fed the **original** statement, before the preview
        LIMIT is injected. That ordering matters: evaluating the rewritten text
        meant the engine saw a LIMIT that the model never wrote, so a policy with
        ``require_limit`` was satisfied by construction and its audit record
        disagreed with what was actually enforced.

        ``unbounded_result`` is the one refusal that preview tolerates, because an
        unbounded preview is the point — the injected LIMIT bounds it. Every other
        refusal (table scope, column scope, read-only, dangerous functions,
        ``max_limit``) still applies and still raises.
        """

        if not isinstance(limit, int) or limit < 1:
            raise ValueError("preview limit must be a positive integer")
        bounded_limit = min(limit, 100)
        try:
            tree = sqlglot.parse_one(sql, read=self.dialect)
            if tree is None or not tree.find(sqlglot.exp.Select):
                raise UnsafeSQLError("preview requires a SELECT query")
        except UnsafeSQLError:
            raise
        except Exception as exc:
            raise UnsafeSQLError(f"preview SQL could not be parsed: {exc}") from exc

        try:
            self.policy_engine.evaluate(sql)
        except SQLPolicyViolation as exc:
            if getattr(exc.decision, "rule", None) != "unbounded_result":
                self._record_policy_decision(exc.decision)
                raise UnsafeSQLError(str(exc), exc.decision) from exc
            # Tolerated for preview only. Recorded so the audit shows the engine
            # was consulted on the original statement and why it was allowed.
            self._record_policy_decision(exc.decision)

        if tree.args.get("limit") is None:
            tree = tree.limit(bounded_limit)
        else:
            existing = tree.args["limit"]
            literal = existing.expression
            current = int(literal.this) if literal and literal.is_int else bounded_limit
            existing.set(
                "expression", sqlglot.exp.Literal.number(min(current, bounded_limit))
            )
        bounded_sql = tree.sql(dialect=self.dialect)
        # ``execute_sql`` re-evaluates the bounded statement, which is what the
        # engine actually runs; both verdicts are on the record now.
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
    def validate_readonly_shape(sql: str) -> str:
        """Refuse SQL that is not a single read-only statement. **Not** authorization.

        This check runs a :class:`SQLPolicyEngine` with an **empty schema** and a
        default policy, so it enforces only what does not depend on knowing the
        database:

        * exactly one statement,
        * a read-only AST root (no INSERT/UPDATE/DDL/ATTACH/PRAGMA, no recursive CTE),
        * no dangerous function, and
        * ``LIMIT`` literals when the default policy asks for them.

        It cannot enforce ``allowed_tables``, ``allowed_columns`` or table scope,
        because it is given no schema to compare against — and with an empty schema
        the engine deliberately lets an unknown table through (see
        ``SQLPolicyEngine._validate_scopes``). An earlier name, ``validate_readonly_sql``,
        invited callers to treat it as a security gate; ``sql_history_store`` used it
        as the *only* gate on its import path, which is why the name is now explicit
        about what it does and does not do.

        For authorization use :meth:`execute_sql` (or a :class:`DatabaseTool` bound
        to a real schema and the deployment's policy), which runs the full engine.
        """

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

    @staticmethod
    def validate_readonly_sql(sql: str) -> str:
        """Deprecated alias for :meth:`validate_readonly_shape`.

        Kept so existing callers keep working; new code should call the explicit
        name so the difference from an authorization gate is visible at the call
        site.
        """

        return DatabaseTool.validate_readonly_shape(sql)
