"""AST-level business-semantic validation and deterministic metric compilation.

Step 06 of the optimization plan replaces string/regex "join guard" checks with
a real SQLGlot AST + scope analysis:

* :class:`SemanticSQLValidator` proves that a generated SQL statement honours the
  governed metric contract (aggregation shape, default filters and their compared
  values, time window, join keys, grain, visible schema). Anything that cannot be
  proven is reported as ``unsupported`` — never as ``passed``.
* :class:`QuerySpecCompiler` deterministically compiles a matched metric request
  into SQLite SQL and is used as an oracle/extra candidate by step 07.

The module deliberately depends only on ``queryforge.domain.semantic`` plus
SQLGlot so that the domain layer stays free of workflow imports. Callers in the
workflow layer pass a duck-typed context through
:meth:`SemanticSQLValidator.for_context`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from pydantic import BaseModel, ConfigDict, Field

from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.semantic.schemas import (
    MetricMatch,
    ResolvedJoinPath,
    SemanticEntity,
    SemanticMetric,
    SemanticModelContext,
)


RULE_METRIC_EXPRESSION = "metric_expression"
RULE_DEFAULT_FILTER = "default_filter"
RULE_TIME_FILTER = "time_filter"
RULE_JOIN_KEY = "join_key"
RULE_FANOUT = "fanout"
RULE_GRAIN = "grain"
RULE_UNKNOWN_TABLE_OR_COLUMN = "unknown_table_or_column"

PASSED = "passed"
VIOLATION = "violation"
UNSUPPORTED = "unsupported"

_AGGREGATE_FUNCTIONS = {"SUM", "COUNT", "AVG", "MIN", "MAX", "TOTAL"}


def normalize_sql_signature(sql: str | None) -> str:
    """Canonical, dialect-normalized signature used for dedup and cycle detection."""
    text = (sql or "").strip()
    if not text:
        return ""
    try:
        parsed = sqlglot.parse_one(text, read="sqlite")
    except Exception:
        return " ".join(text.rstrip(";").casefold().split())
    if parsed is None:
        return ""
    try:
        return parsed.sql(dialect="sqlite").casefold()
    except Exception:
        return " ".join(text.rstrip(";").casefold().split())


def _parse_expression(fragment: str) -> exp.Expression | None:
    """Parse a declarative expression fragment (never the user SQL) into an AST."""
    text = (fragment or "").strip()
    if not text:
        return None
    try:
        parsed = sqlglot.parse_one(f"SELECT {text}", read="sqlite")
        if isinstance(parsed, exp.Select) and parsed.expressions:
            return parsed.expressions[0]
    except Exception:
        pass
    try:
        return sqlglot.parse_one(text, read="sqlite")
    except Exception:
        return None


def _literal_value(node: exp.Expression | None) -> tuple[str, Any] | None:
    """Return ``(kind, value)`` for integer/boolean/string literals."""
    if node is None:
        return None
    if isinstance(node, exp.Boolean):
        return ("bool", bool(node.this))
    if isinstance(node, exp.Literal):
        if node.is_int:
            try:
                return ("int", int(node.this))
            except (TypeError, ValueError):
                return None
        return ("str", str(node.this))
    return None


def _literal_matches(expected: tuple[str, Any], actual: tuple[str, Any]) -> bool:
    if expected[0] in {"int", "bool"} and actual[0] in {"int", "bool"}:
        return int(expected[1]) == int(actual[1])
    if expected[0] == actual[0]:
        return expected[1] == actual[1]
    return False


@dataclass(frozen=True)
class _Aggregate:
    """One aggregate observed in SQL, with resolved physical columns."""

    function: str
    columns: frozenset[tuple[str, str]] = frozenset()
    distinct: bool = False
    star: bool = False


@dataclass
class _MetricAggregateRequirement:
    function: str
    column: tuple[str, str] | None
    distinct: bool
    star: bool

    def describe(self) -> str:
        if self.star:
            return f"{self.function}(*)"
        column = f"{self.column[0]}.{self.column[1]}" if self.column else "?"
        if self.distinct:
            return f"{self.function}(DISTINCT {column})"
        return f"{self.function}({column})"


class SemanticValidationResult(BaseModel):
    """Outcome of one business-semantic validation run."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "violation", "unsupported"] = PASSED
    violations: list[dict[str, str]] = Field(default_factory=list)
    unsupported_reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def rule_names(self) -> list[str]:
        names: list[str] = []
        for violation in self.violations:
            rule = violation.get("rule")
            if rule and rule not in names:
                names.append(rule)
        return names

    @property
    def ok(self) -> bool:
        return self.status != VIOLATION

    def error_message(self) -> str:
        details = "; ".join(
            violation.get("detail", "") for violation in self.violations
        )
        return (
            "Semantic SQL validation failed ("
            + ", ".join(self.rule_names)
            + f"): {details}"
        )

    def summary(self) -> str:
        if self.status == VIOLATION:
            return self.error_message()
        if self.status == UNSUPPORTED:
            return f"Semantic SQL validation unsupported: {self.unsupported_reason}"
        return "Semantic SQL validation passed."


class _AstIndex:
    """Scope-aware column/table index over one parsed SQLite statement.

    All lookups go through SQLGlot scopes/nodes; there is no regex search over the
    SQL text, so aliases, CTEs, and nested subqueries resolve to real tables.
    """

    def __init__(self, root: exp.Expression, table_columns: dict[str, set[str]]) -> None:
        self.root = root
        self.table_columns = table_columns
        self.scopes: list[Scope] = list(traverse_scope(root))
        self.scope_by_expression = {id(scope.expression): scope for scope in self.scopes}
        self.cte_names = {
            cte.alias_or_name.casefold()
            for cte in root.find_all(exp.CTE)
            if cte.alias_or_name
        }
        self.referenced_tables: list[str] = sorted(
            {
                table.name
                for table in root.find_all(exp.Table)
                if table.name and table.name.casefold() not in self.cte_names
            }
        )
        self.column_facts: list[tuple[Scope, exp.Column, set[tuple[str, str]]]] = []
        self.unresolved_columns: list[tuple[Scope, exp.Column]] = []
        self.unknown_qualifiers: set[str] = set()
        self.predicates: list[tuple[Scope, str, exp.Expression]] = []
        self.joins_without_on: list[tuple[Scope, exp.Join]] = []
        self.group_by_columns: list[tuple[Scope, exp.Column]] = []
        self._collect()

    # ------------------------------------------------------------------ setup
    def _collect(self) -> None:
        seen_columns: set[int] = set()
        for scope in self.scopes:
            expression = scope.expression
            if isinstance(expression, exp.Select):
                where = expression.args.get("where")
                if where is not None:
                    self.predicates.append((scope, "where", where.this))
                having = expression.args.get("having")
                if having is not None:
                    self.predicates.append((scope, "having", having.this))
                for join in expression.args.get("joins") or []:
                    on = join.args.get("on")
                    if on is None or (isinstance(on, exp.Boolean) and bool(on.this)):
                        self.joins_without_on.append((scope, join))
                    else:
                        self.predicates.append((scope, "join_on", on))
                group = expression.args.get("group")
                if group is not None:
                    for column in group.find_all(exp.Column):
                        self.group_by_columns.append((scope, column))
            for column in scope.columns:
                if id(column) in seen_columns:
                    continue
                seen_columns.add(id(column))
                if isinstance(column.this, exp.Star):
                    continue
                tables = self.resolve_column(scope, column)
                if tables:
                    self.column_facts.append((scope, column, tables))
                else:
                    self.unresolved_columns.append((scope, column))

    # ------------------------------------------------------------- resolution
    @staticmethod
    def _source_for(scope: Scope, name: str) -> Any:
        source = scope.sources.get(name)
        if source is not None:
            return source
        lowered = name.casefold()
        for key, value in scope.sources.items():
            if key.casefold() == lowered:
                return value
        return None

    def resolve_column(
        self, scope: Scope, column: exp.Column, _depth: int = 0
    ) -> set[tuple[str, str]]:
        name = column.name
        if not name:
            return set()
        if _depth > 8:
            return set()
        table_ref = column.table
        if table_ref:
            source = self._source_for(scope, table_ref)
            if source is None:
                self.unknown_qualifiers.add(table_ref)
                return set()
            return self._resolve_source(source, name, _depth)
        return self._resolve_unqualified(scope, name, _depth)

    def _resolve_source(
        self, source: Any, column_name: str, depth: int
    ) -> set[tuple[str, str]]:
        if isinstance(source, Scope):
            return self._resolve_unqualified(source, column_name, depth + 1)
        if isinstance(source, exp.Table):
            return {(source.name, column_name)}
        return set()

    def _resolve_unqualified(
        self, scope: Scope, column_name: str, depth: int
    ) -> set[tuple[str, str]]:
        if depth > 8:
            return set()
        resolved: set[tuple[str, str]] = set()
        for source in scope.sources.values():
            resolved |= self._resolve_source(source, column_name, depth + 1)
        return resolved

    # ---------------------------------------------------------------- helpers
    def scope_of(self, node: exp.Expression | None) -> Scope | None:
        if node is None:
            return None
        current = node
        while current is not None:
            scope = self.scope_by_expression.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return None

    def scope_key(self, scope: Scope | None) -> str:
        if scope is None:
            return "unknown"
        if scope.is_cte:
            for cte in self.root.find_all(exp.CTE):
                if cte.alias_or_name and scope.expression is cte.this:
                    return f"cte:{cte.alias_or_name}"
        if scope.is_subquery:
            return f"subquery:{id(scope.expression)}"
        return "root"

    @staticmethod
    def _referenced_source_names(scope: Scope) -> set[str]:
        """Names of sources this scope actually reads (traverse_scope also lists all CTEs)."""
        names: set[str] = set()
        expression = scope.expression
        if expression is None:
            return names
        for table in expression.find_all(exp.Table):
            if table.name:
                names.add(table.name.casefold())
            if table.alias:
                names.add(table.alias.casefold())
        for subquery in expression.find_all(exp.Subquery):
            if subquery.alias:
                names.add(subquery.alias.casefold())
        return names

    def scope_lineage(self, scope: Scope | None) -> set[int]:
        """The scope plus every scope whose rows actually feed it.

        A CTE that the aggregate scope never references is deliberately excluded:
        a filter living only there does not govern the metric.
        """
        if scope is None:
            return set()
        lineage: set[int] = set()
        stack = [scope]
        while stack:
            current = stack.pop()
            if id(current) in lineage:
                continue
            lineage.add(id(current))
            referenced = self._referenced_source_names(current)
            for name, source in current.sources.items():
                if not isinstance(source, Scope):
                    continue
                if name.casefold() not in referenced:
                    continue
                stack.append(source)
        return lineage

    def output_aliases(self) -> set[str]:
        aliases: set[str] = set()
        for scope in self.scopes:
            expression = scope.expression
            if not isinstance(expression, exp.Select):
                continue
            for select in expression.selects:
                name = select.alias_or_name
                if name:
                    aliases.add(name.casefold())
        return aliases

    def aggregates(self) -> list[tuple[exp.Expression, _Aggregate]]:
        observed: list[tuple[exp.Expression, _Aggregate]] = []
        seen: set[int] = set()
        for scope in self.scopes:
            expression = scope.expression
            if not isinstance(expression, (exp.Select, exp.Union)):
                continue
            for aggregate in expression.find_all(exp.AggFunc):
                if id(aggregate) in seen:
                    continue
                seen.add(id(aggregate))
                own_scope = self.scope_of(aggregate) or scope
                argument = aggregate.this
                columns = {
                    resolved
                    for column in (
                        argument.find_all(exp.Column)
                        if isinstance(argument, exp.Expression)
                        else []
                    )
                    for resolved in self.resolve_column(own_scope, column)
                }
                observed.append(
                    (
                        aggregate,
                        _Aggregate(
                            function=(aggregate.sql_name() or "").upper(),
                            columns=frozenset(columns),
                            distinct=isinstance(argument, exp.Distinct),
                            star=isinstance(argument, exp.Star)
                            or (
                                isinstance(argument, exp.Distinct)
                                and isinstance(argument.this, exp.Star)
                            ),
                        ),
                    )
                )
        return observed

def _operator_symbol(comparison: Any) -> str:
    """``gte``/``gt``/``lte``/``lt``/``eq`` for one comparison node."""

    for name, symbol in (
        ("GTE", "gte"),
        ("GT", "gt"),
        ("LTE", "lte"),
        ("LT", "lt"),
        ("EQ", "eq"),
    ):
        node_type = getattr(exp, name, None)
        if node_type is not None and isinstance(comparison, node_type):
            return symbol
    return "unknown"


def _digits(value: Any) -> str:
    """Comparable digit form: ``2024-01-01`` and ``20240101`` both become digits.

    The resolved range is an ISO date while a date-key column is an integer, so the
    two must be compared in a shape-independent way.
    """

    return "".join(char for char in str(value) if char.isdigit())


class SemanticSQLValidator:
    """Prove that generated SQL honours the governed semantic contract."""

    def __init__(
        self,
        semantic_model: SemanticModelContext,
        metric_matches: list[MetricMatch],
        *,
        metric_join_paths: list[ResolvedJoinPath] | None = None,
        requested_dimensions: list[str] | None = None,
        date_context: Any | None = None,
    ) -> None:
        self.semantic_model = semantic_model
        self.metric_matches = list(metric_matches)
        self.metric_join_paths = list(metric_join_paths or [])
        self.requested_dimensions = list(requested_dimensions or [])
        self.date_context = date_context
        self.model = semantic_model.model
        self.entities_by_name: dict[str, SemanticEntity] = {
            entity.name: entity for entity in self.model.entities
        }
        self.entities_by_table: dict[str, SemanticEntity] = {
            entity.table: entity for entity in self.model.entities
        }
        self.table_columns: dict[str, set[str]] = {
            entity.table: self._entity_columns(entity) for entity in self.model.entities
        }

    # -------------------------------------------------------------- factories
    @classmethod
    def for_context(cls, context: Any) -> "SemanticSQLValidator | None":
        """Build a validator from a workflow context, or ``None`` when ungoverned."""
        semantic_model = getattr(context, "semantic_model", None)
        metric_matches = list(getattr(context, "metric_matches", None) or [])
        if semantic_model is None or not metric_matches:
            return None
        return cls(
            semantic_model,
            metric_matches,
            metric_join_paths=list(getattr(context, "metric_join_paths", None) or []),
            requested_dimensions=list(
                getattr(context, "metric_requested_dimensions", None) or []
            ),
            date_context=getattr(context, "date_context", None),
        )

    @staticmethod
    def _entity_columns(entity: SemanticEntity) -> set[str]:
        return {
            *entity.expected_columns,
            *entity.primary_key,
            *entity.grain,
            *entity.hidden_columns,
            *(dimension.column for dimension in entity.dimensions),
        }

    # --------------------------------------------------------------- validate
    def validate(self, sql: str) -> SemanticValidationResult:
        evidence: dict[str, Any] = {
            "metrics_checked": [match.metric.name for match in self.metric_matches],
            "tables_found": [],
            "columns_found": [],
            "join_keys_verified": [],
            "default_filters": [],
            "metric_aggregates": [],
            "group_by": [],
            "time_filter": None,
        }
        if not self.metric_matches:
            return SemanticValidationResult(
                status=UNSUPPORTED,
                unsupported_reason="no governed metric is matched for this request",
                evidence=evidence,
            )
        text = (sql or "").strip()
        if not text:
            return SemanticValidationResult(
                status=UNSUPPORTED,
                unsupported_reason="SQL text is empty",
                evidence=evidence,
            )
        try:
            statements = [
                statement
                for statement in sqlglot.parse(text, read="sqlite")
                if statement is not None
            ]
        except Exception as exc:  # noqa: BLE001 - any parse/shape failure
            return SemanticValidationResult(
                status=UNSUPPORTED,
                unsupported_reason=f"SQLite SQL could not be parsed: {exc}",
                evidence=evidence,
            )
        if len(statements) != 1:
            # Never report "passed" for a shape this validator did not inspect.
            return SemanticValidationResult(
                status=UNSUPPORTED,
                unsupported_reason=(
                    "exactly one SQLite statement is required for semantic "
                    f"validation, found {len(statements)}"
                ),
                evidence=evidence,
            )
        root = statements[0]

        unsupported = self._unsupported_shape(root)
        if unsupported is not None:
            return SemanticValidationResult(
                status=UNSUPPORTED,
                unsupported_reason=unsupported,
                evidence=evidence,
            )

        index = _AstIndex(root, self.table_columns)
        evidence["tables_found"] = list(index.referenced_tables)
        evidence["columns_found"] = sorted(
            {
                f"{table}.{column}"
                for _, _, resolved in index.column_facts
                for table, column in resolved
            }
        )
        violations: list[dict[str, str]] = []
        self._check_unknown_schema(index, violations, evidence)
        measure_nodes = self._check_metric_expressions(index, violations, evidence)
        self._check_default_filters(index, violations, evidence, measure_nodes)
        self._check_time_filter(index, violations, evidence)
        self._check_join_keys(index, violations, evidence)
        self._check_fanout(index, violations, evidence)
        self._check_grain(index, violations, evidence)

        if violations:
            return SemanticValidationResult(
                status=VIOLATION, violations=violations, evidence=evidence
            )
        return SemanticValidationResult(status=PASSED, evidence=evidence)

    # ------------------------------------------------------------ unsupported
    def _unsupported_shape(self, root: exp.Expression) -> str | None:
        for _ in root.find_all(exp.Lateral):
            return "LATERAL joins are outside supported validation coverage"
        for with_clause in root.find_all(exp.With):
            if with_clause.args.get("recursive"):
                return "recursive CTEs are outside supported validation coverage"
        if next(root.find_all(exp.Union), None) is not None:
            return "set operations (UNION/INTERSECT/EXCEPT) are outside supported coverage"
        for window in root.find_all(exp.Window):
            if next(window.find_all(exp.AggFunc), None) is not None:
                return (
                    "window functions over aggregates are outside supported "
                    "metric validation coverage"
                )
        for aggregate in root.find_all(exp.AggFunc):
            if any(
                nested is not aggregate
                for nested in aggregate.find_all(exp.AggFunc)
            ):
                return "nested aggregate expressions are outside supported coverage"
            argument = aggregate.this
            if isinstance(argument, exp.Expression) and (
                next(argument.find_all(exp.Subquery), None) is not None
            ):
                return "aggregates over subqueries are outside supported coverage"
        return None

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _add_violation(
        violations: list[dict[str, str]], rule: str, detail: str
    ) -> None:
        candidate = {"rule": rule, "detail": detail}
        if candidate not in violations:
            violations.append(candidate)

    def _check_unknown_schema(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> None:
        model_tables = set(self.table_columns)
        unknown_tables = [
            table for table in index.referenced_tables if table not in model_tables
        ]
        for table in unknown_tables:
            self._add_violation(
                violations,
                RULE_UNKNOWN_TABLE_OR_COLUMN,
                f"table {table!r} is not part of the semantic model's visible "
                "physical schema",
            )
        if unknown_tables:
            return
        aliases = index.output_aliases()
        for _scope, column, resolved in index.column_facts:
            for table, name in sorted(resolved):
                if table in model_tables and name not in self.table_columns[table]:
                    self._add_violation(
                        violations,
                        RULE_UNKNOWN_TABLE_OR_COLUMN,
                        f"column {table}.{name} is not visible in the semantic "
                        "model's physical schema",
                    )
        for _scope, column in index.unresolved_columns:
            if column.name.casefold() in aliases:
                continue
            if index.unknown_qualifiers:
                continue
            self._add_violation(
                violations,
                RULE_UNKNOWN_TABLE_OR_COLUMN,
                f"column reference {column.sql()} cannot be resolved to a visible "
                "physical table",
            )

    def _metric_requirements(
        self, metric: SemanticMetric, base_table: str
    ) -> tuple[list[_MetricAggregateRequirement], set[tuple[str, str]], bool]:
        expression = _parse_expression(metric.expression)
        requirements: list[_MetricAggregateRequirement] = []
        columns: set[tuple[str, str]] = set()
        has_division = False
        if expression is None:
            return requirements, columns, has_division
        for column in expression.find_all(exp.Column):
            columns.add((column.table or base_table, column.name))
        for aggregate in expression.find_all(exp.AggFunc):
            argument = aggregate.this
            if isinstance(argument, exp.Distinct):
                argument = argument.this
            if isinstance(argument, exp.Star):
                requirements.append(
                    _MetricAggregateRequirement(
                        function=(aggregate.sql_name() or "").upper(),
                        column=None,
                        distinct=isinstance(aggregate.this, exp.Distinct),
                        star=True,
                    )
                )
                continue
            column_refs = [
                (column.table or base_table, column.name)
                for column in (
                    argument.find_all(exp.Column)
                    if isinstance(argument, exp.Expression)
                    else []
                )
            ]
            primary = column_refs[0] if column_refs else None
            requirements.append(
                _MetricAggregateRequirement(
                    function=(aggregate.sql_name() or "").upper(),
                    column=primary,
                    distinct=isinstance(aggregate.this, exp.Distinct),
                    star=False,
                )
            )
        has_division = next(expression.find_all(exp.Div), None) is not None
        return requirements, columns, has_division

    @staticmethod
    def _aggregate_satisfies(
        requirement: _MetricAggregateRequirement, observed: _Aggregate
    ) -> bool:
        if requirement.function == "COUNT":
            if observed.function != "COUNT":
                return False
            if requirement.distinct and not observed.distinct:
                return False
            if not requirement.distinct and observed.distinct:
                return False
            if requirement.star:
                return observed.star or bool(observed.columns)
            if requirement.column is None:
                return True
            return requirement.column in observed.columns
        if observed.function != requirement.function:
            return False
        if requirement.column is None:
            return not observed.columns
        return requirement.column in observed.columns

    @staticmethod
    def _average_equivalence(
        requirement: _MetricAggregateRequirement, observed: _Aggregate
    ) -> bool:
        """SUM(x)/COUNT(*) ratio metrics accept the equivalent AVG(x) shape."""
        if requirement.function != "SUM" or requirement.column is None:
            return False
        return (
            observed.function == "AVG"
            and not observed.distinct
            and requirement.column in observed.columns
        )

    def _check_metric_expressions(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> list[exp.Expression]:
        observed = index.aggregates()
        measure_nodes: list[exp.Expression] = []
        sql_has_division = next(index.root.find_all(exp.Div), None) is not None

        for match in self.metric_matches:
            metric = match.metric
            base_entity = self.entities_by_name.get(metric.entity)
            if base_entity is None:
                continue
            base_table = base_entity.table
            requirements, columns, has_division = self._metric_requirements(
                metric, base_table
            )
            missing_columns = sorted(
                f"{table}.{column}"
                for table, column in columns
                if not any(
                    (table, column) in resolved for _, _, resolved in index.column_facts
                )
            )
            if missing_columns:
                self._add_violation(
                    violations,
                    RULE_METRIC_EXPRESSION,
                    f"metric {metric.name!r} requires physical column(s) "
                    + ", ".join(missing_columns)
                    + " which the SQL never references",
                )
            ratio_avg_equivalence = (
                metric.aggregation == "ratio"
                and len(
                    [
                        item
                        for item in requirements
                        if item.function == "SUM" and item.column is not None
                    ]
                )
                == 1
                and any(item.function == "COUNT" and item.star for item in requirements)
            )
            avg_equivalence = ratio_avg_equivalence and any(
                self._average_equivalence(requirement, aggregate)
                for requirement in requirements
                if requirement.function == "SUM" and requirement.column is not None
                for _node, aggregate in observed
            )
            matched_any = True
            for requirement in requirements:
                matched = [
                    (node, aggregate)
                    for node, aggregate in observed
                    if self._aggregate_satisfies(requirement, aggregate)
                ]
                if not matched and avg_equivalence:
                    # SUM(column)/COUNT(*) ratio metrics accept the equivalent AVG(column).
                    continue
                if not matched:
                    matched_any = False
                    self._add_violation(
                        violations,
                        RULE_METRIC_EXPRESSION,
                        f"metric {metric.name!r} requires aggregation "
                        f"{requirement.describe()} which the SQL does not compute",
                    )
                    continue
                for node, _aggregate in matched:
                    if id(node) not in {id(item) for item in measure_nodes}:
                        measure_nodes.append(node)
            if has_division and not sql_has_division and not avg_equivalence:
                matched_any = False
                self._add_violation(
                    violations,
                    RULE_METRIC_EXPRESSION,
                    f"metric {metric.name!r} is a ratio but the SQL computes no "
                    "division between its aggregates",
                )
            evidence["metric_aggregates"].append(
                {
                    "metric": metric.name,
                    "aggregation": metric.aggregation,
                    "required": [item.describe() for item in requirements],
                    "status": "matched" if matched_any else "unmet",
                }
            )
        return measure_nodes

    def _default_filter_expectations(
        self, raw_filter: str, base_table: str
    ) -> tuple[list[tuple[str, str]] | None, tuple[str, Any] | None] | None:
        """The columns and literal a declared default filter is expected to use.

        Returns ``None`` when the filter is not a declarative predicate at all
        (unparsable — the caller reports that as a violation), or a
        ``(columns, literal)`` pair where ``columns`` may itself be ``None`` for a
        filter that parses but references **no column** (for example
        ``EXISTS(SELECT 1 FROM t.x)``). The annotation says so because the previous
        one claimed ``list`` and the caller trusted it: passing ``None`` on to
        ``set(columns)`` raised ``TypeError`` out of ``validate()``, which escapes
        the node's try block and crashes the semantic gate instead of producing a
        verdict.
        """
        node = _parse_expression(raw_filter)
        if node is None:
            return None
        columns = [
            (column.table or base_table, column.name)
            for column in node.find_all(exp.Column)
        ]
        if isinstance(node, exp.EQ):
            left, right = node.this, node.expression
            left_columns = list(left.find_all(exp.Column))
            right_columns = list(right.find_all(exp.Column))
            if len(left_columns) == 1 and not right_columns:
                literal = _literal_value(right)
                if literal is not None:
                    column = left_columns[0]
                    return (
                        [(column.table or base_table, column.name)],
                        literal,
                    )
            if len(right_columns) == 1 and not left_columns:
                literal = _literal_value(left)
                if literal is not None:
                    column = right_columns[0]
                    return (
                        [(column.table or base_table, column.name)],
                        literal,
                    )
        return (columns or None, None)

    def _check_default_filters(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
        measure_nodes: list[exp.Expression],
    ) -> None:
        measure_node_ids = {id(item) for item in measure_nodes}
        for match in self.metric_matches:
            metric = match.metric
            base_entity = self.entities_by_name.get(metric.entity)
            if base_entity is None or not metric.default_filters:
                continue
            base_table = base_entity.table
            lineage = index.scope_lineage(
                self._metric_scope(index, metric, base_table)
            )
            for raw_filter in metric.default_filters:
                expected = self._default_filter_expectations(raw_filter, base_table)
                if expected is None:
                    self._add_violation(
                        violations,
                        RULE_DEFAULT_FILTER,
                        f"metric {metric.name!r} default filter {raw_filter!r} "
                        "could not be interpreted as a declarative predicate",
                    )
                    continue
                columns, literal = expected
                if not columns:
                    # Parses, but names no column: there is nothing to look for in
                    # the SQL, so the filter cannot be proved either way. Reported
                    # rather than crashing, and not silently treated as satisfied.
                    self._add_violation(
                        violations,
                        RULE_DEFAULT_FILTER,
                        f"metric {metric.name!r} default filter {raw_filter!r} "
                        "references no column, so its presence in the SQL cannot "
                        "be verified",
                    )
                    record: dict[str, Any] = {
                        "metric": metric.name,
                        "filter": raw_filter,
                        "status": "unverifiable_no_column",
                    }
                    evidence["default_filters"].append(record)
                    continue
                record = {
                    "metric": metric.name,
                    "filter": raw_filter,
                    "status": "missing",
                }
                comparisons = self._filter_comparisons(index, columns, lineage, measure_node_ids)
                if literal is not None:
                    satisfied = [
                        observed
                        for observed, _ in comparisons
                        if _literal_matches(literal, observed)
                    ]
                    conflicting = [
                        observed
                        for observed, _ in comparisons
                        if not _literal_matches(literal, observed)
                    ]
                    if satisfied:
                        record["status"] = "value_checked"
                    elif conflicting:
                        self._add_violation(
                            violations,
                            RULE_DEFAULT_FILTER,
                            f"metric {metric.name!r} default filter {raw_filter!r} is "
                            f"violated: the SQL compares "
                            f"{columns[0][0]}.{columns[0][1]} to a different literal",
                        )
                        record["status"] = "value_conflict"
                    else:
                        self._check_filter_presence(
                            index,
                            violations,
                            record,
                            metric,
                            raw_filter,
                            columns,
                            lineage,
                            measure_nodes,
                        )
                else:
                    self._check_filter_presence(
                        index,
                        violations,
                        record,
                        metric,
                        raw_filter,
                        columns,
                        lineage,
                        measure_nodes,
                    )
                evidence["default_filters"].append(record)

    def _metric_scope(
        self, index: _AstIndex, metric: SemanticMetric, base_table: str
    ) -> Scope | None:
        requirements, _, _ = self._metric_requirements(metric, base_table)
        wanted = {
            item.column for item in requirements if item.column is not None
        }
        for node, aggregate in index.aggregates():
            if wanted and (wanted & set(aggregate.columns)):
                return index.scope_of(node)
        return index.scopes[-1] if index.scopes else None

    def _filter_comparisons(
        self,
        index: _AstIndex,
        columns: list[tuple[str, str]],
        lineage: set[int],
        measure_node_ids: set[int],
    ) -> list[tuple[tuple[str, Any], bool]]:
        """Every effective ``col = literal`` comparison for the governed columns.

        Each entry is ``(literal, matches_expected_columns)``; entries are ordered
        so that callers can detect both satisfied and conflicting comparisons.
        """
        wanted = set(columns)
        results: list[tuple[tuple[str, Any], bool]] = []
        for comparison in index.root.find_all(exp.EQ):
            left, right = comparison.this, comparison.expression
            left_columns = list(left.find_all(exp.Column))
            right_columns = list(right.find_all(exp.Column))
            if bool(left_columns) == bool(right_columns):
                continue
            column = left_columns[0] if left_columns else right_columns[0]
            literal = _literal_value(right if left_columns else left)
            if literal is None:
                continue
            scope = index.scope_of(comparison)
            if scope is None:
                continue
            resolved = index.resolve_column(scope, column)
            if not (resolved & wanted):
                continue
            if not self._filter_is_effective(
                index, comparison, scope, lineage, measure_node_ids
            ):
                continue
            results.append((literal, True))
        return results

    @staticmethod
    def _filter_is_effective(
        index: _AstIndex,
        comparison: exp.Expression,
        scope: Scope,
        lineage: set[int],
        measure_node_ids: set[int],
    ) -> bool:
        """A filter governs the metric only inside the aggregate's lineage.

        Predicates in the aggregate's own lineage (WHERE/HAVING/JOIN ON) and
        comparisons that feed the metric expression itself (CASE-style filters)
        count; a predicate in an unrelated/unused CTE does not.
        """
        current: exp.Expression | None = comparison.parent
        while current is not None:
            if isinstance(current, (exp.Where, exp.Having, exp.Join)):
                return not lineage or id(scope) in lineage
            if id(current) in measure_node_ids:
                return True
            current = current.parent
        return False

    def _check_filter_presence(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        record: dict[str, Any],
        metric: SemanticMetric,
        raw_filter: str,
        columns: list[tuple[str, str]],
        lineage: set[int],
        measure_nodes: list[exp.Expression],
    ) -> None:
        wanted = set(columns)
        present = False
        for scope, _kind, predicate in index.predicates:
            if lineage and id(scope) not in lineage:
                continue
            for column in predicate.find_all(exp.Column):
                if index.resolve_column(scope, column) & wanted:
                    present = True
                    break
            if present:
                break
        if not present:
            for node in measure_nodes:
                for column in node.find_all(exp.Column):
                    parent_scope = index.scope_of(node)
                    if parent_scope is None:
                        break
                    if index.resolve_column(parent_scope, column) & wanted:
                        present = True
                        break
                if present:
                    break
        if present:
            record["status"] = "presence_checked"
            return
        record["status"] = "missing"
        rendered = ", ".join(f"{table}.{column}" for table, column in wanted)
        self._add_violation(
            violations,
            RULE_DEFAULT_FILTER,
            f"metric {metric.name!r} default filter {raw_filter!r} is missing: no "
            f"effective predicate references {rendered}",
        )

    @staticmethod
    def _time_boundaries(
        index: "_AstIndex", wanted: set[tuple[str, str]]
    ) -> list[tuple[str, Any]]:
        """Literal comparisons applied to the governed time column.

        Returns ``(operator, literal)`` for every predicate that compares the time
        field to a value, so the boundaries can be checked against the resolved
        date range instead of only checking that *some* predicate mentions the
        column.
        """

        boundaries: list[tuple[str, Any]] = []
        for scope, _kind, predicate in index.predicates:
            for node in predicate.find_all(exp.Column):
                if not (index.resolve_column(scope, node) & wanted):
                    continue
                # BETWEEN is its own node type, not a pair of comparisons, and the
                # governed compiler emits it for a resolved date range. Missing it
                # made every compiler-produced time filter look unbounded.
                between = node.find_ancestor(exp.Between)
                if between is not None:
                    low, high = between.args.get("low"), between.args.get("high")
                    for operator, bound in (("gte", low), ("lte", high)):
                        if isinstance(bound, exp.Literal):
                            boundaries.append((operator, _literal_value(bound)))
                    continue
                comparison = node.find_ancestor(
                    exp.EQ, exp.GTE, exp.GT, exp.LTE, exp.LT
                )
                if comparison is None:
                    continue
                literal = comparison.expression
                if not isinstance(literal, exp.Literal):
                    continue
                boundaries.append((_operator_symbol(comparison), _literal_value(literal)))
        return boundaries

    def _check_time_filter(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> None:
        ranges = [
            (getattr(item, "start_date", None), getattr(item, "end_date", None))
            for item in (getattr(self.date_context, "ranges", None) or [])
        ]
        ranges = [
            (start, end)
            for start, end in ranges
            if isinstance(start, str) and isinstance(end, str)
        ]
        if not ranges:
            return
        for match in self.metric_matches:
            metric = match.metric
            time_field = metric.time_field
            if not time_field:
                self._add_violation(
                    violations,
                    RULE_TIME_FILTER,
                    f"metric {metric.name!r} has no time_field but the request "
                    "carries a date range",
                )
                continue
            table_ref, separator, column_ref = time_field.partition(".")
            if not separator or not table_ref or not column_ref:
                continue
            wanted = {(table_ref, column_ref)}
            present = False
            for scope, _kind, predicate in index.predicates:
                for column in predicate.find_all(exp.Column):
                    if index.resolve_column(scope, column) & wanted:
                        present = True
                        break
                if present:
                    break
            boundaries = (
                self._time_boundaries(index, wanted) if present else []
            )
            verdict = "presence_checked"
            if present and not boundaries:
                # The column is mentioned but never compared to anything, so the
                # predicate cannot be bounding the window.
                verdict = "presence_only"
            elif present:
                verdict = "boundary_checked"
            evidence["time_filter"] = {
                "metric": metric.name,
                "time_field": time_field,
                "status": verdict,
                "resolved_ranges": [
                    {"start": start, "end": end} for start, end in ranges
                ],
                "observed_boundaries": [
                    {"operator": operator, "value": value}
                    for operator, value in boundaries
                ],
            }
            if not present:
                self._add_violation(
                    violations,
                    RULE_TIME_FILTER,
                    f"metric {metric.name!r} time_field {time_field} is not filtered "
                    "although the request resolves a date range",
                )
            elif not boundaries:
                # Asymmetric with default filters, which do compare literals. A
                # predicate that mentions the column without comparing it does not
                # constrain the window, so the check would otherwise pass on a
                # statement that ignores the requested range.
                self._add_violation(
                    violations,
                    RULE_TIME_FILTER,
                    f"metric {metric.name!r} time_field {time_field} is referenced but "
                    "compared to no value, so the requested date range is not applied",
                )
            elif not self._boundaries_cover_ranges(boundaries, ranges):
                self._add_violation(
                    violations,
                    RULE_TIME_FILTER,
                    f"metric {metric.name!r} time_field {time_field} is compared to "
                    f"{[value for _operator, value in boundaries]} which does not cover "
                    f"the requested range(s) "
                    f"{[f'{start}..{end}' for start, end in ranges]}",
                )

    @staticmethod
    def _boundaries_cover_ranges(
        boundaries: list[tuple[str, Any]],
        ranges: list[tuple[str, str]],
    ) -> bool:
        """Whether the observed comparisons can express the requested window.

        Only *shape* is checked, not calendar arithmetic: at least one lower bound
        and one upper bound must be present, in a form comparable to the range
        strings. A comparison that pins a different value is not "wrong" here — the
        resolved range is derived from the question, so a mismatch means the SQL
        filtered something else. Values are normalised to digits so a date-key form
        (``20240101``) and an ISO form (``2024-01-01``) both compare.
        """

        lower = {"gte", "gt", "eq"}
        upper = {"lte", "lt", "eq"}
        observed = {
            (_digits(value), operator) for operator, value in boundaries
        }
        for start, end in ranges:
            start_digits = _digits(start)
            end_digits = _digits(end)
            has_lower = any(
                operator in lower and value >= start_digits
                for value, operator in observed
            )
            has_upper = any(
                operator in upper and value <= end_digits
                for value, operator in observed
            )
            if not (has_lower and has_upper):
                return False
        return True

    def _check_join_keys(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> None:
        used_tables = set(index.referenced_tables)
        for path in self.metric_join_paths:
            required = {table for table in path.tables}
            missing = sorted(required - used_tables)
            if missing:
                self._add_violation(
                    violations,
                    RULE_JOIN_KEY,
                    "Join Path contract violation: SQL omitted required table(s) "
                    + ", ".join(missing),
                )
                continue
            for step in path.steps:
                if step.from_table == step.to_table:
                    continue
                if self._join_equality_present(index, step.from_table, step.from_column,
                                               step.to_table, step.to_column):
                    evidence["join_keys_verified"].append(
                        f"{step.from_table}.{step.from_column} = "
                        f"{step.to_table}.{step.to_column}"
                    )
                    continue
                self._add_violation(
                    violations,
                    RULE_JOIN_KEY,
                    f"join key mismatch for relationship {step.relationship!r}: the SQL "
                    f"must equate {step.from_table}.{step.from_column} with "
                    f"{step.to_table}.{step.to_column}",
                )
        for _scope, join in index.joins_without_on:
            joined = join.this
            table_name = joined.name if isinstance(joined, exp.Table) else None
            if table_name is None:
                continue
            governed = {
                table
                for path in self.metric_join_paths
                for table in path.tables
                if table != table_name
            }
            if governed & used_tables:
                self._add_violation(
                    violations,
                    RULE_JOIN_KEY,
                    f"cross join detected: {table_name!r} is joined without an ON "
                    "equality to the governed tables "
                    + ", ".join(sorted(governed & used_tables)),
                )

    def _join_equality_present(
        self,
        index: _AstIndex,
        left_table: str,
        left_column: str,
        right_table: str,
        right_column: str,
    ) -> bool:
        for scope, _kind, predicate in index.predicates:
            for comparison in predicate.find_all(exp.EQ):
                sides = [
                    side
                    for side in (comparison.this, comparison.expression)
                    if side is not None
                ]
                resolutions: list[set[tuple[str, str]]] = []
                for side in sides:
                    columns = list(side.find_all(exp.Column))
                    if len(columns) != 1:
                        resolutions.append(set())
                        continue
                    resolutions.append(index.resolve_column(scope, columns[0]))
                if len(resolutions) != 2:
                    continue
                if (
                    {(left_table, left_column)} & resolutions[0]
                    and {(right_table, right_column)} & resolutions[1]
                ) or (
                    {(right_table, right_column)} & resolutions[0]
                    and {(left_table, left_column)} & resolutions[1]
                ):
                    return True
        return False

    def _check_fanout(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> None:
        used_tables = set(index.referenced_tables)
        for match in self.metric_matches:
            base_entity = self.entities_by_name.get(match.metric.entity)
            if base_entity is None:
                continue
            governed_tables = {base_entity.table} | {
                table
                for path in self.metric_join_paths
                if path.from_entity == match.metric.entity
                for table in path.tables
            }
            for table in sorted(used_tables - governed_tables):
                joined_entity = self.entities_by_table.get(table)
                if joined_entity is None:
                    continue
                diagnostic = SemanticModelLoader.resolve_join_path(
                    self.model,
                    match.metric.entity,
                    joined_entity.name,
                    include_undeclared=True,
                )
                if diagnostic is not None and not diagnostic.safe:
                    self._add_violation(
                        violations,
                        RULE_FANOUT,
                        f"Fan-out execution guard blocked metric "
                        f"{match.metric.name!r} from joining {table!r}: "
                        + "; ".join(diagnostic.fanout_steps),
                    )
                    evidence.setdefault("fanout", []).append(table)

    def _check_grain(
        self,
        index: _AstIndex,
        violations: list[dict[str, str]],
        evidence: dict[str, Any],
    ) -> None:
        grouped = {
            resolved
            for scope, column in index.group_by_columns
            for resolved in index.resolve_column(scope, column)
        }
        evidence["group_by"] = sorted(f"{table}.{column}" for table, column in grouped)
        for reference in self.requested_dimensions:
            entity_name, separator, dimension_name = reference.partition(".")
            entity = self.entities_by_name.get(entity_name)
            if not separator or entity is None:
                continue
            dimension = next(
                (
                    item
                    for item in entity.dimensions
                    if item.name == dimension_name
                ),
                None,
            )
            if dimension is None:
                continue
            expected = (entity.table, dimension.column)
            if expected not in grouped:
                self._add_violation(
                    violations,
                    RULE_GRAIN,
                    f"GROUP BY is missing requested dimension {reference!r} "
                    f"({entity.table}.{dimension.column})",
                )


# --------------------------------------------------------------------- compiler


class QuerySpec(BaseModel):
    """Deterministic compiled query for one matched governed metric."""

    model_config = ConfigDict(extra="forbid")

    metric_name: str
    aggregation: Literal["count", "sum", "ratio"]
    base_table: str
    measure_expression: str
    default_filters: list[str] = Field(default_factory=list)
    time_field: str | None = None
    time_filter: str | None = None
    joins: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list)
    limit: int = 100
    sql: str
    explanation: str
    tables_used: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    def to_candidate(self, index: int = 0) -> dict[str, Any]:
        return {
            "candidate_index": index,
            "sql": self.sql,
            "explanation": self.explanation,
            "tables_used": list(self.tables_used),
            "generated_by": "query_spec",
            "query_spec": {
                "metric": self.metric_name,
                "aggregation": self.aggregation,
                "base_table": self.base_table,
                "default_filters": list(self.default_filters),
                "time_filter": self.time_filter,
                "group_by": list(self.group_by),
            },
        }


class QuerySpecCompiler:
    """Compile sum/count/ratio metrics into a deterministic SQLite query."""

    @classmethod
    def for_context(cls, context: Any, *, limit: int = 100) -> QuerySpec | None:
        semantic_model = getattr(context, "semantic_model", None)
        metric_matches = list(getattr(context, "metric_matches", None) or [])
        if semantic_model is None or not metric_matches:
            return None
        return cls.compile(
            semantic_model=semantic_model,
            metric_matches=metric_matches,
            metric_join_paths=list(getattr(context, "metric_join_paths", None) or []),
            requested_dimensions=list(
                getattr(context, "metric_requested_dimensions", None) or []
            ),
            date_context=getattr(context, "date_context", None),
            limit=limit,
        )

    @classmethod
    def compile(
        cls,
        *,
        semantic_model: SemanticModelContext,
        metric_matches: list[MetricMatch],
        metric_join_paths: list[ResolvedJoinPath] | None = None,
        requested_dimensions: list[str] | None = None,
        date_context: Any | None = None,
        limit: int = 100,
    ) -> QuerySpec | None:
        if not metric_matches:
            return None
        model = semantic_model.model
        entities_by_name = {entity.name: entity for entity in model.entities}
        first = metric_matches[0]
        metric = first.metric
        entity = entities_by_name.get(metric.entity)
        if entity is None:
            return None
        base_table = entity.table
        used_tables = [base_table]
        joins: list[str] = []
        seen_join_tables: set[str] = set()
        group_by: list[str] = []
        #: Chronological ordering for period-name dimensions (month_name, ...), so a
        #: time series is never ordered alphabetically.
        order_by: list[str] = []
        for reference in requested_dimensions or []:
            entity_name, separator, dimension_name = reference.partition(".")
            dimension_entity = entities_by_name.get(entity_name)
            if not separator or dimension_entity is None:
                return None
            dimension = next(
                (
                    item
                    for item in dimension_entity.dimensions
                    if item.name == dimension_name
                ),
                None,
            )
            if dimension is None:
                return None
            group_by.append(f"{dimension_entity.table}.{dimension.column}")
            ordering = cls._natural_order_column(dimension_entity, dimension)
            if ordering is not None:
                order_by.append(f"{dimension_entity.table}.{ordering}")
            if dimension_entity.table == base_table:
                continue
            direct = cls._direct_time_join(
                semantic_model,
                base_table=base_table,
                metric_time_field=metric.time_field,
                dimension_entity=dimension_entity,
                join_paths=metric_join_paths or [],
            )
            if direct is not None:
                joins.append(direct)
                if dimension_entity.table not in used_tables:
                    used_tables.append(dimension_entity.table)
                continue
            path = cls._resolve_time_aware_path(
                semantic_model,
                metric_entity=metric.entity,
                dimension_entity=entity_name,
                metric_time_field=metric.time_field,
                join_paths=metric_join_paths or [],
            )
            if path is None:
                return None
            for step in path.steps:
                if step.to_table in {base_table, *seen_join_tables}:
                    continue
                seen_join_tables.add(step.to_table)
                joins.append(
                    f"JOIN {step.to_table} ON {step.from_table}.{step.from_column} = "
                    f"{step.to_table}.{step.to_column}"
                )
                if step.to_table not in used_tables:
                    used_tables.append(step.to_table)

        time_field = metric.time_field
        time_filter = cls._time_filter(time_field, date_context)
        where_clauses = [*metric.default_filters]
        if time_filter:
            where_clauses.append(time_filter)
        alias = cls._alias(metric.name)
        statement = f"SELECT "
        if group_by:
            statement += ", ".join(group_by) + ", "
        statement += f"{metric.expression} AS {alias} FROM {base_table}"
        if joins:
            statement += " " + " ".join(joins)
        if where_clauses:
            statement += " WHERE " + " AND ".join(f"({item})" for item in where_clauses)
        if group_by:
            statement += " GROUP BY " + ", ".join(group_by)
            # Period labels are ordered by their numeric companion when the model
            # exposes one (month_name -> month_number); otherwise the label order is
            # kept, which is stable and predictable if not chronological.
            statement += " ORDER BY " + ", ".join(order_by or group_by)
        if limit and limit > 0:
            statement += f" LIMIT {int(limit)}"
        return QuerySpec(
            metric_name=metric.name,
            aggregation=metric.aggregation,
            base_table=base_table,
            measure_expression=metric.expression,
            default_filters=list(metric.default_filters),
            time_field=time_field,
            time_filter=time_filter,
            joins=joins,
            group_by=group_by,
            order_by=list(group_by),
            limit=int(limit),
            sql=statement,
            explanation=(
                f"Deterministic QuerySpec compilation of governed metric "
                f"{metric.name!r} ({metric.aggregation}) over {base_table}."
            ),
            tables_used=used_tables,
            evidence={
                "metric": metric.name,
                "aggregation": metric.aggregation,
                "default_filters": list(metric.default_filters),
                "time_field": time_field,
                "time_filter": time_filter,
                "group_by": group_by,
                "order_by": order_by or list(group_by),
                "joins": joins,
                "limit": int(limit),
            },
        )

    @staticmethod
    def _natural_order_column(dimension_entity: Any, dimension: Any) -> str | None:
        """The column that orders a period-name dimension chronologically.

        ``ORDER BY month_name`` sorts April, August, December, ... which makes a
        "previous month" comparison pick the alphabetically-last months. When the
        entity also exposes ``month_number`` (or a ``full_date``), that column is the
        truthful ordering key.
        """
        columns = {
            str(getattr(item, "column", "") or ""): str(getattr(item, "name", "") or "")
            for item in getattr(dimension_entity, "dimensions", ())
        }
        name = str(getattr(dimension, "name", "") or "")
        if f"{name}_number" in columns.values():
            return next(
                column for column, label in columns.items() if label == f"{name}_number"
            )
        if name in {"month", "quarter", "day_of_week"} and "full_date" in columns:
            return "full_date"
        return None

    @classmethod
    def _direct_time_join(
        cls,
        semantic_model: SemanticModelContext,
        *,
        base_table: str,
        metric_time_field: str | None,
        dimension_entity: Any,
        join_paths: list[ResolvedJoinPath],
    ) -> str | None:
        """JOIN clause for a date dimension joined on the metric's OWN time column.

        The governed metric declares which column carries its grain's time
        (``watch_hours.time_field = fact_watch_session.watch_date_key``). A date
        dimension must therefore be reached through that column: the only declared
        path to the calendar entity here travels through the *episode release* date,
        which would answer "watch hours by month" with the month each episode was
        released. The direct join is used only when it is unambiguous: the base table
        really has the column and the target date table exposes the same key the
        declared path uses as its destination column.
        """
        if not metric_time_field:
            return None
        # Only a genuine calendar/period dimension may be joined on the metric's own
        # time column. A plain dimension (store.region) must keep its declared join
        # path: joining it on the date column would silently pair unrelated rows.
        period_names = {
            "date",
            "full_date",
            "month",
            "month_number",
            "quarter",
            "year",
            "week",
            "day_of_week",
            "weekend",
        }
        if not {
            str(getattr(item, "name", "") or "") for item in getattr(dimension_entity, "dimensions", ())
        } & period_names:
            return None
        table, separator, column = str(metric_time_field).partition(".")
        if not separator or table != base_table or not column:
            return None
        key: str | None = None
        for path in join_paths:
            if getattr(path, "to_entity", None) != getattr(dimension_entity, "name", None):
                continue
            steps = list(getattr(path, "steps", ()) or ())
            if steps and getattr(steps[-1], "to_table", None) == dimension_entity.table:
                key = str(getattr(steps[-1], "to_column", "") or "")
        if not key:
            return None
        # The join key may be the dimension's primary key rather than one of its
        # exposed dimensions (a calendar is keyed by ``date_key`` here).
        known_columns = {
            getattr(item, "column", None) for item in getattr(dimension_entity, "dimensions", ())
        } | {str(item) for item in getattr(dimension_entity, "primary_key", ()) or ()}
        if key not in known_columns:
            return None
        # ``metric.time_field`` is validated against the physical schema when the
        # model loads, so the base table really does have this column; the entity's
        # own dimension list does not have to expose it.
        return (
            f"JOIN {dimension_entity.table} ON {base_table}.{column} = "
            f"{dimension_entity.table}.{key}"
        )

    @classmethod
    def _resolve_time_aware_path(
        cls,
        semantic_model: SemanticModelContext,
        *,
        metric_entity: str,
        dimension_entity: str,
        metric_time_field: str | None,
        join_paths: list[ResolvedJoinPath],
    ) -> ResolvedJoinPath | None:
        """Pick the join path for a dimension, preferring the metric's own time column.

        A governed metric declares the column that carries its grain's time
        (``watch_hours.time_field = fact_watch_session.watch_date_key``). When the
        requested dimension lives on the calendar entity the caller may hand over a
        declared-but-different path (for this model ``watch_session -> calendar``
        travels through the *episode release* date), which would answer "watch hours
        by month" with the month each episode was released rather than the month it
        was watched. Whenever a declared path starts from the metric's own time
        column, that path is the correct one for time bucketing.
        """
        column = (
            str(metric_time_field).partition(".")[2] if metric_time_field else ""
        )
        if column:
            for candidate in getattr(semantic_model.model, "join_paths", ()) or ():
                if (
                    getattr(candidate, "from_entity", None) != metric_entity
                    or getattr(candidate, "to_entity", None) != dimension_entity
                ):
                    continue
                steps = list(getattr(candidate, "steps", ()) or ())
                if steps and getattr(steps[0], "from_column", None) == column:
                    return candidate
        return cls._resolve_path(
            semantic_model, metric_entity, dimension_entity, join_paths
        )

    @staticmethod
    def _resolve_path(
        semantic_model: SemanticModelContext,
        from_entity: str,
        to_entity: str,
        join_paths: list[ResolvedJoinPath],
    ) -> ResolvedJoinPath | None:
        for path in join_paths:
            if path.to_entity == to_entity and path.from_entity == from_entity:
                return path
        path = SemanticModelLoader.resolve_join_path(
            semantic_model.model, from_entity, to_entity
        )
        if path is None or not path.safe:
            return None
        return path

    @staticmethod
    def _time_filter(time_field: str | None, date_context: Any | None) -> str | None:
        if not time_field:
            return None
        ranges = [
            (getattr(item, "start_date", None), getattr(item, "end_date", None))
            for item in (getattr(date_context, "ranges", None) or [])
        ]
        clauses: list[str] = []
        for start, end in ranges:
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            clauses.append(
                f"{time_field} BETWEEN {QuerySpecCompiler._time_literal(time_field, start)}"
                f" AND {QuerySpecCompiler._time_literal(time_field, end)}"
            )
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return " OR ".join(f"({clause})" for clause in clauses)

    @staticmethod
    def _time_literal(time_field: str, iso_date: str) -> str:
        if time_field.casefold().endswith("_key"):
            digits = iso_date.replace("-", "").replace("/", "")
            if digits.isdigit():
                return digits
        return "'" + iso_date.replace("'", "''") + "'"

    @staticmethod
    def _alias(name: str) -> str:
        if name and (name[0].isalpha() or name[0] == "_") and all(
            character.isalnum() or character == "_" for character in name
        ):
            return name
        return '"' + name.replace('"', '""') + '"'
