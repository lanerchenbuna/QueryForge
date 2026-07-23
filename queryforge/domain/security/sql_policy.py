"""Parse SQLite SQL into an AST and enforce one auditable access policy."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from queryforge.core.observability import current_run_id
from queryforge.core.schemas.models import SqlPolicyDecision, TableSchema


LOGGER = logging.getLogger("queryforge.domain.security")
DEFAULT_DANGEROUS_FUNCTIONS = {
    "eval",
    "fts3_tokenizer",
    "load_extension",
    "readfile",
    "writefile",
}


class SQLPolicyError(ValueError):
    """Raised when SQL policy configuration is invalid."""


class SQLPolicyViolation(ValueError):
    """Raised when a parsed query violates one named security rule."""

    def __init__(self, decision: SqlPolicyDecision) -> None:
        self.decision = decision
        super().__init__(
            f"SQL_SECURITY_ERROR run_id={decision.run_id} "
            f"rule={decision.rule}: {decision.reason}"
        )


class SQLSecurityPolicy(BaseModel):
    """Small deployment policy; None table scope means all physical tables."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    name: str = Field(default="default_sql_security", min_length=1)
    allowed_tables: list[str] | None = None
    allowed_columns: dict[str, list[str]] = Field(default_factory=dict)
    dangerous_functions: list[str] = Field(default_factory=list)
    require_limit: bool = False
    max_limit: int | None = Field(default=None, ge=1)
    max_joins: int | None = Field(default=None, ge=0)
    max_tables: int | None = Field(default=None, ge=1)
    allow_cross_join: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("allowed_tables", "dangerous_functions")
    @classmethod
    def normalize_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(normalized) != len(value):
            raise ValueError("must contain unique, non-blank values")
        return normalized

    @field_validator("allowed_columns")
    @classmethod
    def normalize_column_scope(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for raw_table, raw_columns in value.items():
            table = raw_table.strip()
            columns = list(
                dict.fromkeys(column.strip() for column in raw_columns if column.strip())
            )
            if not table or not columns or len(columns) != len(raw_columns):
                raise ValueError(
                    "table names and column allowlists must be non-blank and unique"
                )
            normalized[table] = columns
        return normalized

    def public_summary(self, source_path: str | None = None) -> dict[str, Any]:
        return {
            "status": "active",
            "name": self.name,
            "version": self.version,
            "source_path": source_path,
            "table_scope": self.allowed_tables,
            "column_scope": self.allowed_columns,
            "dangerous_functions": sorted(
                DEFAULT_DANGEROUS_FUNCTIONS
                | {name.casefold() for name in self.dangerous_functions}
            ),
            "require_limit": self.require_limit,
            "max_limit": self.max_limit,
            "max_joins": self.max_joins,
            "max_tables": self.max_tables,
            "allow_cross_join": self.allow_cross_join,
        }


def load_sql_policy(
    path: str | Path | None,
) -> tuple[SQLSecurityPolicy, str | None]:
    if path is None:
        return SQLSecurityPolicy(), None
    policy_path = Path(path).expanduser().resolve()
    if policy_path.suffix.lower() not in {".yml", ".yaml"}:
        raise SQLPolicyError(
            f"SQL security policy must be a .yml or .yaml file: {policy_path}"
        )
    if not policy_path.is_file():
        raise SQLPolicyError(f"SQL security policy does not exist: {policy_path}")
    try:
        payload = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        policy = SQLSecurityPolicy.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise SQLPolicyError(
            f"Invalid SQL security policy {policy_path}: {exc}"
        ) from exc
    return policy, str(policy_path)


class SQLPolicyEngine:
    """Validate AST structure, data scope, functions, and result bounds."""

    def __init__(
        self,
        policy: SQLSecurityPolicy,
        schemas: list[TableSchema],
        *,
        source_path: str | None = None,
        audit: bool = True,
    ) -> None:
        self.policy = policy
        self.source_path = source_path
        self.audit = audit
        self.schemas = {schema.table_name: schema for schema in schemas}
        self._table_lookup = {name.casefold(): name for name in self.schemas}
        self._validate_configuration()

    @property
    def summary(self) -> dict[str, Any]:
        return self.policy.public_summary(self.source_path)

    def allowed_table_names(self) -> list[str]:
        if self.policy.allowed_tables is None:
            return list(self.schemas)
        allowed = {table.casefold() for table in self.policy.allowed_tables}
        return [table for table in self.schemas if table.casefold() in allowed]

    def filter_schema(self, schema: TableSchema) -> TableSchema:
        table = self._canonical_table(schema.table_name)
        if table is None or table not in self.allowed_table_names():
            raise self._violation(
                "table_scope",
                f"table {schema.table_name!r} is outside the allowed table scope",
                tables=[schema.table_name],
            )
        allowed_columns = self._allowed_columns_for(table)
        if allowed_columns is None:
            return schema
        visible = {
            column.name.casefold(): column.name
            for column in schema.columns
            if column.name.casefold() in allowed_columns
        }
        return TableSchema(
            table_name=schema.table_name,
            columns=[
                column for column in schema.columns if column.name.casefold() in visible
            ],
            foreign_keys=[
                foreign_key
                for foreign_key in schema.foreign_keys
                if foreign_key.column.casefold() in visible
                and self._table_is_allowed(foreign_key.referenced_table)
                and self._column_is_allowed(
                    foreign_key.referenced_table, foreign_key.referenced_column
                )
            ],
        )

    def evaluate(self, sql: str) -> SqlPolicyDecision:
        run_id = current_run_id()
        try:
            statements = [
                statement
                for statement in sqlglot.parse(sql, read="sqlite")
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
        except ParseError as exc:
            raise self._violation(
                "ast_parse", f"SQLite SQL could not be parsed: {exc}"
            ) from exc
        if len(statements) != 1:
            raise self._violation(
                "single_statement",
                f"exactly one SQL statement is required; found {len(statements)}",
            )
        root = statements[0]
        if root is None or not isinstance(root, exp.Query):
            raise self._violation(
                "read_only_ast",
                f"only SELECT/query AST roots are allowed; found {type(root).__name__}",
            )
        forbidden = self._first_forbidden_node(root)
        if forbidden is not None:
            raise self._violation(
                "read_only_ast",
                f"write or administrative AST node {type(forbidden).__name__} is forbidden",
            )
        recursive = next(
            (node for node in root.find_all(exp.With) if node.args.get("recursive")),
            None,
        )
        if recursive is not None:
            raise self._violation(
                "recursive_cte", "WITH RECURSIVE queries are forbidden"
            )

        functions = sorted(self._function_names(root))
        blocked = sorted(
            set(functions)
            & (
                DEFAULT_DANGEROUS_FUNCTIONS
                | {name.casefold() for name in self.policy.dangerous_functions}
            )
        )
        if blocked:
            raise self._violation(
                "dangerous_function",
                "forbidden SQL function(s): " + ", ".join(blocked),
                functions=functions,
            )

        tables, columns = self._validate_scopes(root)
        self._validate_query_shape(root, tables)
        limit = self._validate_limit(root, tables)
        decision = SqlPolicyDecision(
            allowed=True,
            run_id=run_id,
            policy_name=self.policy.name,
            rule="allow",
            reason="AST and configured SQL security rules passed",
            tables=sorted(tables),
            columns=sorted(columns),
            functions=functions,
            limit=limit,
        )
        self._log(decision)
        return decision

    def _validate_scopes(self, root: exp.Expression) -> tuple[set[str], set[str]]:
        referenced_tables: set[str] = set()
        referenced_columns: set[str] = set()
        for scope in traverse_scope(root):
            physical_sources: dict[str, str] = {}
            for alias, source in scope.sources.items():
                if not isinstance(source, exp.Table):
                    continue
                table = self._canonical_table(source.name)
                if table is None:
                    continue
                physical_sources[alias.casefold()] = table
                referenced_tables.add(table)
                if not self._table_is_allowed(table):
                    raise self._violation(
                        "table_scope",
                        f"table {table!r} is outside the allowed table scope",
                        tables=sorted(referenced_tables),
                    )

            for projection in getattr(scope.expression, "selects", []):
                if not projection.is_star:
                    continue
                table_alias = projection.table.casefold() if isinstance(
                    projection, exp.Column
                ) else ""
                star_tables = (
                    [physical_sources[table_alias]]
                    if table_alias in physical_sources
                    else list(physical_sources.values())
                )
                restricted = [
                    table
                    for table in star_tables
                    if self._allowed_columns_for(table) is not None
                ]
                if restricted:
                    raise self._violation(
                        "column_scope_star",
                        "SELECT * cannot be used on column-restricted table(s): "
                        + ", ".join(sorted(restricted)),
                        tables=sorted(referenced_tables),
                    )

            for column in scope.columns:
                if column.is_star:
                    continue
                table = self._resolve_column_table(
                    column.name, column.table, physical_sources
                )
                if table is None:
                    continue
                reference = f"{table}.{column.name}"
                referenced_columns.add(reference)
                if not self._column_is_allowed(table, column.name):
                    raise self._violation(
                        "column_scope",
                        f"column {reference!r} is outside the allowed column scope",
                        tables=sorted(referenced_tables),
                        columns=sorted(referenced_columns),
                    )
        return referenced_tables, referenced_columns

    def _validate_query_shape(
        self,
        root: exp.Expression,
        tables: set[str],
    ) -> None:
        if self.policy.max_tables is not None and len(tables) > self.policy.max_tables:
            raise self._violation(
                "max_tables",
                f"query references {len(tables)} tables; policy maximum is "
                f"{self.policy.max_tables}",
                tables=sorted(tables),
            )
        joins = list(root.find_all(exp.Join))
        if self.policy.max_joins is not None and len(joins) > self.policy.max_joins:
            raise self._violation(
                "max_joins",
                f"query contains {len(joins)} JOINs; policy maximum is "
                f"{self.policy.max_joins}",
                tables=sorted(tables),
            )
        if not self.policy.allow_cross_join:
            cross_join = next(
                (
                    join
                    for join in joins
                    if str(join.args.get("kind") or "").upper() == "CROSS"
                ),
                None,
            )
            if cross_join is not None:
                raise self._violation(
                    "cross_join",
                    "CROSS JOIN is forbidden by the SQL security policy",
                    tables=sorted(tables),
                )

    def _resolve_column_table(
        self,
        column: str,
        qualifier: str,
        physical_sources: dict[str, str],
    ) -> str | None:
        if qualifier:
            return physical_sources.get(qualifier.casefold())
        candidates = [
            table
            for table in physical_sources.values()
            if column.casefold()
            in {
                item.name.casefold() for item in self.schemas[table].columns
            }
        ]
        if len(candidates) > 1:
            raise self._violation(
                "ambiguous_column_scope",
                f"unqualified column {column!r} is ambiguous across {candidates!r}",
                tables=sorted(set(candidates)),
                columns=[column],
            )
        return candidates[0] if candidates else None

    def _validate_limit(self, root: exp.Expression, tables: set[str]) -> int | None:
        limit_node = root.args.get("limit")
        if limit_node is None:
            if self.policy.require_limit and not self._is_intrinsically_bounded(root, tables):
                raise self._violation(
                    "unbounded_result",
                    "query must include a literal LIMIT",
                    tables=sorted(tables),
                )
            return None
        expression = limit_node.args.get("expression")
        if not isinstance(expression, exp.Literal) or expression.is_string:
            raise self._violation(
                "limit_literal", "LIMIT must be a positive integer literal"
            )
        try:
            limit = int(expression.this)
        except (TypeError, ValueError) as exc:
            raise self._violation(
                "limit_literal", "LIMIT must be a positive integer literal"
            ) from exc
        if limit < 1:
            raise self._violation("limit_literal", "LIMIT must be at least 1")
        if self.policy.max_limit is not None and limit > self.policy.max_limit:
            raise self._violation(
                "max_limit",
                f"LIMIT {limit} exceeds configured maximum {self.policy.max_limit}",
                limit=limit,
            )
        return limit

    @staticmethod
    def _is_intrinsically_bounded(root: exp.Expression, tables: set[str]) -> bool:
        if not tables:
            return True
        if not isinstance(root, exp.Select):
            return False
        if root.args.get("group") or root.args.get("distinct"):
            return False
        if next(root.find_all(exp.Window), None) is not None:
            return False
        return next(root.find_all(exp.AggFunc), None) is not None

    @staticmethod
    def _function_names(root: exp.Expression) -> set[str]:
        names: set[str] = set()
        for function in root.find_all(exp.Func):
            if isinstance(function, exp.Anonymous):
                name = function.name
            else:
                name = function.sql_name()
            if name:
                names.add(str(name).casefold())
        return names

    @staticmethod
    def _first_forbidden_node(root: exp.Expression) -> exp.Expression | None:
        names = (
            "Alter",
            "Analyze",
            "Attach",
            "Command",
            "Create",
            "Delete",
            "Detach",
            "Drop",
            "Insert",
            "Merge",
            "Pragma",
            "TruncateTable",
            "Update",
            "Use",
        )
        forbidden_types = tuple(
            expression_type
            for name in names
            if (expression_type := getattr(exp, name, None)) is not None
        )
        return next(root.find_all(forbidden_types), None)

    def _validate_configuration(self) -> None:
        errors: list[str] = []
        physical_tables = set(self.schemas)
        for table in self.policy.allowed_tables or []:
            if table not in physical_tables:
                errors.append(f"allowed_tables references unknown table {table!r}")
        for table, columns in self.policy.allowed_columns.items():
            if table not in physical_tables:
                errors.append(f"allowed_columns references unknown table {table!r}")
                continue
            if self.policy.allowed_tables is not None and table not in self.policy.allowed_tables:
                errors.append(
                    f"allowed_columns table {table!r} is outside allowed_tables"
                )
            physical_columns = {column.name for column in self.schemas[table].columns}
            for column in columns:
                if column not in physical_columns:
                    errors.append(
                        f"allowed_columns references unknown column {table}.{column}"
                    )
        if self.policy.require_limit and self.policy.max_limit is None:
            errors.append("require_limit=true requires max_limit")
        if errors:
            raise SQLPolicyError("Invalid SQL security policy scope: " + "; ".join(errors))

    def _canonical_table(self, table: str) -> str | None:
        return self._table_lookup.get(table.casefold())

    def _table_is_allowed(self, table: str) -> bool:
        canonical = self._canonical_table(table)
        if canonical is None:
            return False
        if self.policy.allowed_tables is None:
            return True
        return canonical in self.policy.allowed_tables

    def _allowed_columns_for(self, table: str) -> set[str] | None:
        canonical = self._canonical_table(table)
        if canonical is None:
            return set()
        columns = self.policy.allowed_columns.get(canonical)
        return {column.casefold() for column in columns} if columns else None

    def _column_is_allowed(self, table: str, column: str) -> bool:
        allowed = self._allowed_columns_for(table)
        return allowed is None or column.casefold() in allowed

    def _violation(
        self,
        rule: str,
        reason: str,
        *,
        tables: list[str] | None = None,
        columns: list[str] | None = None,
        functions: list[str] | None = None,
        limit: int | None = None,
    ) -> SQLPolicyViolation:
        decision = SqlPolicyDecision(
            allowed=False,
            run_id=current_run_id(),
            policy_name=self.policy.name,
            rule=rule,
            reason=reason,
            tables=tables or [],
            columns=columns or [],
            functions=functions or [],
            limit=limit,
        )
        self._log(decision)
        return SQLPolicyViolation(decision)

    def _log(self, decision: SqlPolicyDecision) -> None:
        if not self.audit:
            return
        log = LOGGER.info if decision.allowed else LOGGER.error
        log(
            "sql_policy_decision allowed=%s policy=%s rule=%s reason=%s "
            "tables=%s columns=%s functions=%s limit=%s",
            decision.allowed,
            decision.policy_name,
            decision.rule,
            decision.reason,
            decision.tables,
            decision.columns,
            decision.functions,
            decision.limit,
        )
