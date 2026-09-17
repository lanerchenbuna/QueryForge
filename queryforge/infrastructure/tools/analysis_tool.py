"""Governed SQL assembly for the step-11 analysis inputs.

The analysis tools in :mod:`queryforge.domain.analysis.analysis_tools` compute
over plain rows; this module is the only place that *fetches* those rows.  Every
statement goes through the same policy-checked
:class:`~queryforge.infrastructure.tools.database_tool.DatabaseTool` as model
generated SQL (there is no parallel unguarded path), is bounded by a caller row
budget and a SQLite deadline, and records the exact SQL plus the grain, unit and
version metadata that the downstream computation must respect.

Two rules follow from step 11's "inputs come from artefacts" requirement:

* a capped result is returned with ``truncated=True``, never as a silently
  complete series; and
* a truncated result may not feed a trend or contribution computation unless the
  caller explicitly opts in with ``allow_truncated=True`` - a partial series
  would produce a wrong trend, and a partial breakdown would report a wrong
  total change.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Literal, TypeVar

import sqlglot

from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError
from queryforge.domain.analysis.analysis_tools import (
    TIME_GRAINS,
    require_consistent_grain,
    require_consistent_units,
    require_consistent_versions,
)

from pydantic import BaseModel, ConfigDict, Field

#: Aggregations this assembler is allowed to emit.  Anything else is refused:
#: the metric contract is the source of truth, not free-form SQL.
SUPPORTED_AGGREGATIONS: tuple[str, ...] = (
    "sum",
    "count",
    "count_distinct",
    "avg",
    "min",
    "max",
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: SQLite strftime bucket expression per time grain (keys mirror TIME_GRAINS).
_GRAIN_BUCKETS: dict[str, str] = {
    "daily": "strftime('%Y-%m-%d', {column})",
    "weekly": "strftime('%Y-W%W', {column})",
    "monthly": "strftime('%Y-%m', {column})",
    "quarterly": (
        "strftime('%Y', {column}) || '-Q' || CAST((CAST(strftime('%m', {column}) "
        "AS INTEGER) + 2) / 3 AS INTEGER)"
    ),
    "yearly": "strftime('%Y', {column})",
}


class AnalysisInputBudget(BaseModel):
    """Resource limits for one input-assembly call."""

    model_config = ConfigDict(extra="forbid")

    max_rows: int = Field(default=1_000, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0.0)


class MetricResolution(BaseModel):
    """The governed metric definition an analysis input is assembled from.

    ``expression`` is optional: ``count`` without an expression means
    ``COUNT(*)``.  A non-identifier expression is accepted because governed
    semantic models declare metric formulas, but it must parse as a single
    SQLite scalar expression (no statement, no subquery) and it still passes
    through the SQL policy engine.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    entity_table: str = Field(min_length=1)
    aggregation: str = "sum"
    expression: str | None = None
    time_field: str | None = None
    dimension: str | None = None
    filters: dict[str, list[str | int | float | None]] = Field(default_factory=dict)
    unit: str | None = None
    version: str | None = None


class AnalysisInput(BaseModel):
    """Shared metadata of one assembled analysis input."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    method: str
    grain: str | None = None
    unit: str | None = None
    unit_source: Literal["declared", "unknown"] = "unknown"
    version: str | None = None
    truncated: bool = False
    requested_row_limit: int | None = None
    row_limit: int = 0
    row_count: int = 0
    sql: str = ""
    window: list[str] | None = None
    filters: dict[str, list[Any]] = Field(default_factory=dict)
    purpose: str = ""
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class MetricValueInput(AnalysisInput):
    """A single scalar metric value for one window."""

    value: float | None = None


class DimensionInput(AnalysisInput):
    """A categorical breakdown (one row per dimension value)."""

    dimension: str = ""
    buckets: list[dict[str, Any]] = Field(default_factory=list)
    bucket_sum: float | None = None
    category_count: int = 0
    null_value_categories: list[str] = Field(default_factory=list)


class PeriodInput(AnalysisInput):
    """A time-bucketed series (one row per time bucket)."""

    time_field: str = ""
    points: list[dict[str, Any]] = Field(default_factory=list)
    null_value_points: list[str] = Field(default_factory=list)
    period_count: int = 0


InputT = TypeVar("InputT", bound=AnalysisInput)


def assert_usable(
    result: InputT,
    *,
    purpose: str,
    allow_truncated: bool = False,
) -> InputT:
    """Refuse a truncated input for a computation that needs the whole shape.

    Step 11 第 7 条: a bounded preview may be shown to a person, but a trend,
    coverage figure, or contribution total computed from a capped result is
    simply wrong.  The caller must therefore opt in explicitly
    (``allow_truncated=True``), and the returned payload still carries
    ``truncated=True`` so the limitation travels with the number.
    """

    if result.truncated and not allow_truncated:
        raise ValueError(
            f"truncated_analysis_input: {purpose} needs a complete input but the "
            f"governed query hit the row budget (returned {result.row_count} rows "
            f"with row_limit={result.row_limit}); raise the budget or pass "
            "allow_truncated=True to accept an explicitly marked partial input "
            "(trend/contribution numbers computed from it would be wrong)."
        )
    return result


class AnalysisInputAssembler:
    """Assemble analysis tool inputs from one governed DatabaseTool."""

    def __init__(
        self,
        database_tool: DatabaseTool,
        budget: AnalysisInputBudget | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.database_tool = database_tool
        self.budget = budget or AnalysisInputBudget()
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------------ public

    def metric_value(
        self,
        resolution: MetricResolution,
        *,
        window: tuple[str, str] | None = None,
        allow_truncated: bool = False,
        purpose: str = "metric_value",
    ) -> MetricValueInput:
        """One scalar aggregate for ``window`` (no dimension)."""

        normalized = self._prepare(resolution, window=window)
        sql = (
            f"SELECT {self._aggregate_sql(normalized)} AS value"
            f" FROM {self._quoted_identifier(normalized.entity_table)}"
            f"{self._where_sql(normalized, window)} LIMIT 2"
        )
        rows = self._execute(sql, purpose=purpose)
        truncated = len(rows) > 1
        value = _to_float(rows[0][0]) if rows else None
        result = MetricValueInput(
            **self._metadata(
                normalized,
                sql=sql,
                window=window,
                purpose=purpose,
                truncated=truncated,
                row_limit=1,
                row_count=min(len(rows), 1),
                grain="scalar",
            ),
            value=value,
            undefined_reason=None if value is not None else "no_rows",
        )
        if value is None:
            result.limitations.append(
                "The window returned no rows (or a NULL aggregate): the value is "
                "undefined rather than zero."
            )
        return assert_usable(result, purpose=purpose, allow_truncated=allow_truncated)

    def metric_by_dimension(
        self,
        resolution: MetricResolution,
        *,
        dimension: str | None = None,
        window: tuple[str, str] | None = None,
        row_limit: int | None = None,
        allow_truncated: bool = False,
        purpose: str = "dimension_breakdown",
    ) -> DimensionInput:
        """GROUP BY one dimension, ordered by value desc (bounded)."""

        normalized = self._prepare(resolution, window=window, dimension=dimension)
        if not normalized.dimension:
            raise ValueError(
                "metric_by_dimension needs a dimension column (pass dimension=... or "
                "declare it on the metric resolution)"
            )
        effective_limit = self._effective_row_limit(row_limit)
        column = self._quoted_identifier(normalized.dimension)
        sql = (
            f"SELECT {column} AS category, {self._aggregate_sql(normalized)} AS value"
            f" FROM {self._quoted_identifier(normalized.entity_table)}"
            f"{self._where_sql(normalized, window)}"
            " GROUP BY 1 ORDER BY 2 DESC, 1 ASC"
            f" LIMIT {effective_limit + 1}"
        )
        rows = self._execute(sql, purpose=purpose)
        truncated = len(rows) > effective_limit
        rows = rows[:effective_limit]
        buckets: list[dict[str, Any]] = []
        null_categories: list[str] = []
        for row in rows:
            category = "" if row[0] is None else str(row[0])
            if row[0] is None:
                null_categories.append("(null)")
            value = _to_float(row[1])
            if value is None:
                null_categories.append(category or "(null)")
            buckets.append({"category": category, "value": 0.0 if value is None else value})
        defined = [bucket["value"] for bucket in buckets]
        result = DimensionInput(
            **self._metadata(
                normalized,
                sql=sql,
                window=window,
                purpose=purpose,
                truncated=truncated,
                row_limit=effective_limit,
                row_count=len(rows),
                grain="categorical",
                requested_row_limit=row_limit,
            ),
            dimension=normalized.dimension or "",
            buckets=buckets,
            bucket_sum=float(sum(defined)) if buckets else None,
            category_count=len(buckets),
            null_value_categories=sorted(set(null_categories)),
        )
        result.limitations.append(
            "Ordered by value desc and capped at row_limit+1 probe rows: when "
            "truncated, the smallest categories are the ones missing, so the "
            "bucket sum is a lower bound of the total."
        )
        return assert_usable(result, purpose=purpose, allow_truncated=allow_truncated)

    def metric_by_period(
        self,
        resolution: MetricResolution,
        *,
        grain: str = "daily",
        window: tuple[str, str] | None = None,
        row_limit: int | None = None,
        allow_truncated: bool = False,
        purpose: str = "period_series",
    ) -> PeriodInput:
        """Time-bucketed series ordered oldest-first (bounded)."""

        if grain not in TIME_GRAINS:
            raise ValueError(
                f"unsupported time grain {grain!r}; declared grains are "
                f"{', '.join(TIME_GRAINS)}"
            )
        normalized = self._prepare(resolution, window=window)
        if not normalized.time_field:
            raise ValueError(
                "metric_by_period needs the metric's time_field to bucket the series"
            )
        effective_limit = self._effective_row_limit(row_limit)
        column = self._quoted_identifier(normalized.time_field)
        bucket = _GRAIN_BUCKETS[grain].format(column=column)
        sql = (
            f"SELECT {bucket} AS period, {self._aggregate_sql(normalized)} AS value"
            f" FROM {self._quoted_identifier(normalized.entity_table)}"
            f"{self._where_sql(normalized, window)}"
            " GROUP BY 1 ORDER BY 1 ASC"
            f" LIMIT {effective_limit + 1}"
        )
        rows = self._execute(sql, purpose=purpose)
        truncated = len(rows) > effective_limit
        rows = rows[:effective_limit]
        points: list[dict[str, Any]] = []
        null_points: list[str] = []
        for row in rows:
            period = "" if row[0] is None else str(row[0])
            value = _to_float(row[1])
            if value is None:
                null_points.append(period)
            points.append({"period": period, "value": value})
        result = PeriodInput(
            **self._metadata(
                normalized,
                sql=sql,
                window=window,
                purpose=purpose,
                truncated=truncated,
                row_limit=effective_limit,
                row_count=len(rows),
                grain=grain,
                requested_row_limit=row_limit,
            ),
            time_field=normalized.time_field,
            points=points,
            null_value_points=null_points,
            period_count=len(points),
        )
        result.limitations.append(
            "Buckets are strftime-derived: weeks start on Monday and the calendar "
            "is the database's, not the user's locale; empty buckets are absent "
            "rather than zero-filled."
        )
        return assert_usable(result, purpose=purpose, allow_truncated=allow_truncated)

    def combine_metadata(self, *inputs: AnalysisInput) -> dict[str, Any]:
        """Merge metadata, refusing to mix units, grains, or versions (11-E1)."""

        if not inputs:
            raise ValueError("combine_metadata needs at least one input")
        units = {item.unit for item in inputs if item.unit is not None}
        versions = {item.version for item in inputs if item.version is not None}
        grains = {item.grain for item in inputs if item.grain not in (None, "scalar")}
        return {
            "unit": require_consistent_units(units),
            "version": require_consistent_versions(versions),
            "grain": require_consistent_grain(grains),
            "truncated": any(item.truncated for item in inputs),
            "sources": [item.sql for item in inputs],
        }

    # ----------------------------------------------------------------- private

    def _prepare(
        self,
        resolution: MetricResolution,
        *,
        window: tuple[str, str] | None,
        dimension: str | None = None,
    ) -> MetricResolution:
        if not isinstance(resolution, MetricResolution):
            resolution = MetricResolution.model_validate(resolution)
        if resolution.aggregation not in SUPPORTED_AGGREGATIONS:
            raise ValueError(
                f"unsupported aggregation {resolution.aggregation!r}; declared "
                f"aggregations are {', '.join(SUPPORTED_AGGREGATIONS)}"
            )
        if resolution.aggregation in {"sum", "avg", "min", "max", "count_distinct"} and not resolution.expression:
            raise ValueError(
                f"aggregation {resolution.aggregation!r} needs a metric expression"
            )
        if resolution.expression:
            self._check_expression(resolution.expression)
        tables = self.database_tool.list_tables()
        if resolution.entity_table not in tables:
            raise ValueError(
                f"metric {resolution.name!r} targets table "
                f"{resolution.entity_table!r} which is not in the authorised, "
                f"policy-visible table scope ({', '.join(sorted(tables)) or 'none'})"
            )
        columns = self._visible_columns(resolution.entity_table)
        for label, column in (
            ("time_field", resolution.time_field),
            ("dimension", dimension if dimension is not None else resolution.dimension),
        ):
            if column and column not in columns:
                raise ValueError(
                    f"metric {resolution.name!r} {label} {column!r} is not a visible "
                    f"column of {resolution.entity_table!r} "
                    f"({', '.join(sorted(columns))})"
                )
        for column, values in resolution.filters.items():
            if column not in columns:
                raise ValueError(
                    f"filter column {column!r} is not a visible column of "
                    f"{resolution.entity_table!r}"
                )
            for value in values:
                if value is not None and not isinstance(value, (str, int, float)):
                    raise ValueError(
                        f"filter value {value!r} for {column!r} must be a string, "
                        "number, or None"
                    )
        if window is not None:
            if len(window) != 2 or any(not str(bound).strip() for bound in window):
                raise ValueError("window must be a (start, end) pair of ISO dates")
        if dimension is not None:
            return resolution.model_copy(update={"dimension": dimension})
        return resolution

    def _check_expression(self, expression: str) -> None:
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("metric expression must be a non-empty string")
        try:
            tree = sqlglot.parse_one(expression, read="sqlite")
        except Exception as exc:  # pragma: no cover - depends on sqlglot wording
            raise ValueError(f"metric expression could not be parsed: {exc}") from exc
        if tree is None or tree.find(sqlglot.exp.Select) or tree.find(sqlglot.exp.Subquery):
            raise ValueError(
                "metric expression must be a single scalar expression over the "
                "entity table (no subquery, no statement)"
            )

    def _visible_columns(self, table: str) -> set[str]:
        schema = self.database_tool.describe_table(table)
        return {column.name for column in schema.columns}

    def _quoted_identifier(self, identifier: str) -> str:
        try:
            return DatabaseTool._quote_identifier(identifier)
        except UnsafeSQLError as exc:
            raise ValueError(
                f"invalid SQL identifier {identifier!r} in the metric resolution"
            ) from exc

    def _aggregate_sql(self, resolution: MetricResolution) -> str:
        aggregation = resolution.aggregation
        expression = resolution.expression
        if aggregation == "count" and not expression:
            return "COUNT(*)"
        if not _IDENTIFIER.fullmatch(expression or ""):
            # A governed metric formula: the policy engine still validates it.
            inner = f"({expression})"
        else:
            inner = self._quoted_identifier(expression or "")
        if aggregation == "count_distinct":
            return f"COUNT(DISTINCT {inner})"
        return f"{aggregation.upper()}({inner})"

    def _where_sql(self, resolution: MetricResolution, window: tuple[str, str] | None) -> str:
        clauses: list[str] = []
        if window is not None:
            if not resolution.time_field:
                raise ValueError("a window requires the metric's time_field")
            column = self._quoted_identifier(resolution.time_field)
            start, end = (str(bound) for bound in window)
            clauses.append(
                f"{column} >= {_literal(start)} AND {column} <= {_literal(end)}"
            )
        for column, values in sorted(resolution.filters.items()):
            if not values:
                raise ValueError(
                    f"filter for column {column!r} lists no values; an empty filter "
                    "would silently drop every row"
                )
            quoted = self._quoted_identifier(column)
            allowed = [value for value in values if value is not None]
            parts: list[str] = []
            if allowed:
                parts.append(
                    f"{quoted} IN ({', '.join(_literal(value) for value in allowed)})"
                )
            if len(allowed) != len(values):
                parts.append(f"{quoted} IS NULL")
            clauses.append(f"({' OR '.join(parts)})")
        return f" WHERE {' AND '.join(clauses)}" if clauses else ""

    def _effective_row_limit(self, requested: int | None) -> int:
        if requested is not None:
            if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
                raise ValueError("row_limit must be a positive integer")
        budget = self.budget.max_rows
        return budget if requested is None else min(requested, budget)

    def _metadata(
        self,
        resolution: MetricResolution,
        *,
        sql: str,
        window: tuple[str, str] | None,
        purpose: str,
        truncated: bool,
        row_limit: int,
        row_count: int,
        grain: str,
        requested_row_limit: int | None = None,
    ) -> dict[str, Any]:
        return {
            "metric": resolution.name,
            "method": "governed_sql",
            "grain": grain,
            "unit": resolution.unit,
            "unit_source": "declared" if resolution.unit is not None else "unknown",
            "version": resolution.version,
            "truncated": truncated,
            "requested_row_limit": requested_row_limit,
            "row_limit": row_limit,
            "row_count": row_count,
            "sql": sql,
            "window": list(window) if window else None,
            "filters": {key: list(values) for key, values in sorted(resolution.filters.items())},
            "purpose": purpose,
            "limitations": [
                "Input assembled through the governed SQL policy engine; the SQL is "
                "recorded verbatim so the number can be recomputed.",
                "unit/version are carried from the metric resolution: an undeclared "
                "unit stays None rather than being assumed.",
            ],
        }

    def _execute(self, sql: str, *, purpose: str) -> list[list[Any]]:
        deadline = self._clock() + self.budget.timeout_seconds
        restore = self._install_deadline_handler(deadline)
        try:
            result = self.database_tool.execute_sql(sql)
        except UnsafeSQLError:
            raise
        except Exception as exc:
            raise ValueError(
                f"analysis_input_query_failed: {purpose}: {exc}"
            ) from exc
        finally:
            restore()
        return [list(row) for row in result.rows]

    def _install_deadline_handler(self, deadline: float) -> Callable[[], None]:
        """Interrupt a long-running SQLite statement once the deadline passes."""

        connection = getattr(
            getattr(self.database_tool, "connector", None), "_connection", None
        )
        if connection is None or not hasattr(connection, "set_progress_handler"):
            return _noop

        def handler() -> int:
            return 1 if self._clock() >= deadline else 0

        try:  # pragma: no cover - depends on the sqlite3 build
            connection.set_progress_handler(handler, 10_000)
        except Exception:  # pragma: no cover - defensive
            return _noop

        def restore() -> None:
            try:  # pragma: no cover - defensive
                connection.set_progress_handler(None, 0)
            except Exception:  # pragma: no cover - defensive
                pass

        return restore


def _noop() -> None:
    return None


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        raise ValueError("boolean filter values are not supported")
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "SUPPORTED_AGGREGATIONS",
    "AnalysisInput",
    "AnalysisInputAssembler",
    "AnalysisInputBudget",
    "DimensionInput",
    "MetricResolution",
    "MetricValueInput",
    "PeriodInput",
    "assert_usable",
]
