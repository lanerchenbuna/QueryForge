"""Budgeted, read-only runtime data-quality evidence for the current task.

Step 08 turns "the SQL ran" into "the data behind it is trustworthy enough to
interpret". Every check runs through the same policy-filtered
:class:`~queryforge.infrastructure.tools.database_tool.DatabaseTool` as model
generated SQL, is bounded by an explicit budget, and never reports ``ok`` when
it could not actually run: unverifiable checks return ``unknown``.

Status vocabulary: ``ok`` / ``warning`` / ``error`` / ``unknown``.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


QualityStatus = Literal["ok", "warning", "error", "unknown"]

SUPPORTED_CHECKS = (
    "grain_unique",
    "null_rate",
    "freshness",
    "coverage",
    "referential",
    "duplicates",
)

_STATUS_RANK = {"ok": 0, "unknown": 1, "warning": 2, "error": 3}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_DATE = re.compile(r"^\d{8}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER_TYPES = ("INT",)


class DataQualityBudget(BaseModel):
    """Resource limits for one quality pass."""

    model_config = ConfigDict(extra="forbid")

    max_rows_scanned_per_table: int = Field(default=200_000, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    max_columns_per_check: int = Field(default=12, ge=1)


class QualityCheckResult(BaseModel):
    """One executed (or explicitly not executed) quality check."""

    model_config = ConfigDict(extra="forbid")

    table: str
    check: str
    status: QualityStatus
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status == "error"


class DataQualityReport(BaseModel):
    """Outcome of one ``check``/``report`` call."""

    model_config = ConfigDict(extra="forbid")

    table: str | None = None
    checks: list[QualityCheckResult] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)

    @property
    def status(self) -> QualityStatus:
        if not self.checks:
            return "unknown"
        worst = max(self.checks, key=lambda item: _STATUS_RANK[item.status])
        return worst.status

    @property
    def errors(self) -> list[QualityCheckResult]:
        return [check for check in self.checks if check.status == "error"]

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("ok", "warning", "error", "unknown")}
        for check in self.checks:
            counts[check.status] += 1
        return counts

    def to_payload(self) -> dict[str, Any]:
        """Shape consumed by the agent-team ``qa_report`` artifact."""
        return {
            "status": self.status,
            "counts": self.counts(),
            "blocking": bool(self.errors),
            "checks": [check.model_dump(mode="json") for check in self.checks],
            "tables": sorted({check.table for check in self.checks if check.table}),
            "errors": [check.model_dump(mode="json") for check in self.errors],
            "budget": self.budget,
        }


class DataQualityTool:
    """Read-only, budgeted quality checks over a policy-filtered DatabaseTool."""

    #: Coverage windows longer than this many days are refused (unknown).
    MAX_WINDOW_DAYS = 3_660

    def __init__(
        self,
        database_tool: DatabaseTool,
        budget: DataQualityBudget | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.database_tool = database_tool
        self.budget = budget or DataQualityBudget()
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------------ public

    def check(
        self,
        table_name: str,
        checks: Sequence[str],
        *,
        time_field: str | None = None,
        window: tuple[str, str] | None = None,
        grain_columns: Sequence[str] | None = None,
        expected_max_date: str | None = None,
        referenced: tuple[str, str] | None = None,
        columns: Sequence[str] | None = None,
        max_null_rate: float = 0.05,
        tolerance_days: int = 1,
        min_observed_ratio: float = 0.5,
    ) -> DataQualityReport:
        """Run the requested checks against one table."""
        requested = [str(check).strip() for check in checks if str(check).strip()]
        report = DataQualityReport(
            table=table_name,
            budget={
                "max_rows_scanned_per_table": self.budget.max_rows_scanned_per_table,
                "timeout_seconds": self.budget.timeout_seconds,
            },
        )
        unsupported = [check for check in requested if check not in SUPPORTED_CHECKS]
        if unsupported:
            report.checks.append(
                QualityCheckResult(
                    table=table_name,
                    check="unsupported",
                    status="unknown",
                    reason="unsupported_check:" + ",".join(unsupported),
                    evidence={"requested": list(requested)},
                )
            )
            requested = [check for check in requested if check in SUPPORTED_CHECKS]

        deadline = self._clock() + self.budget.timeout_seconds
        schema = None
        failure_reason: str | None = None
        try:
            schema = self.database_tool.describe_table(table_name)
        except UnsafeSQLError as exc:
            failure_reason = f"policy_denied:{exc}"
        except Exception as exc:  # pragma: no cover - defensive
            failure_reason = f"table_unavailable:{exc}"

        for check in requested:
            if failure_reason is not None:
                report.checks.append(
                    QualityCheckResult(
                        table=table_name,
                        check=check,
                        status="unknown",
                        reason=failure_reason,
                    )
                )
                continue
            if self._clock() >= deadline:
                report.checks.append(self._timeout_result(table_name, check))
                continue
            try:
                report.checks.append(
                    self._run_check(
                        check,
                        schema,
                        deadline=deadline,
                        time_field=time_field,
                        window=window,
                        grain_columns=grain_columns,
                        expected_max_date=expected_max_date,
                        referenced=referenced,
                        columns=columns,
                        max_null_rate=max_null_rate,
                        tolerance_days=tolerance_days,
                        min_observed_ratio=min_observed_ratio,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                report.checks.append(
                    QualityCheckResult(
                        table=table_name,
                        check=check,
                        status="unknown",
                        reason=f"check_failed:{exc}",
                    )
                )
        return report

    def report(self, requests: Sequence[Any]) -> dict[str, Any]:
        """Run ``(table, checks, options)`` requests and summarize them.

        Requests may be ``(table, checks)`` tuples, ``(table, checks, options)``
        tuples or mappings with ``table``/``checks`` keys. The returned payload is
        the shape the agent-team ``qa_report`` artifact expects under
        ``quality_checks``.
        """
        checks: list[QualityCheckResult] = []
        for request in requests:
            table_name, requested, options = _normalize_request(request)
            checks.extend(self.check(table_name, requested, **options).checks)
        summary = DataQualityReport(
            checks=checks,
            budget={
                "max_rows_scanned_per_table": self.budget.max_rows_scanned_per_table,
                "timeout_seconds": self.budget.timeout_seconds,
            },
        )
        return summary.to_payload()

    # --------------------------------------------------------------- internals

    def _run_check(
        self,
        check: str,
        schema: Any,
        *,
        deadline: float,
        time_field: str | None,
        window: tuple[str, str] | None,
        grain_columns: Sequence[str] | None,
        expected_max_date: str | None,
        referenced: tuple[str, str] | None,
        columns: Sequence[str] | None,
        max_null_rate: float,
        tolerance_days: int,
        min_observed_ratio: float,
    ) -> QualityCheckResult:
        table = schema.table_name
        if check in {"grain_unique", "duplicates"}:
            grain = self._resolve_grain(schema, grain_columns)
            if isinstance(grain, str):
                return self._unknown(table, check, grain)
            return self._grain_check(table, check, grain, schema, deadline=deadline)
        if check == "null_rate":
            return self._null_rate_check(
                table,
                columns or [column.name for column in schema.columns],
                schema,
                max_null_rate=max_null_rate,
                deadline=deadline,
            )
        if check == "freshness":
            return self._freshness_check(
                table,
                schema,
                time_field=time_field,
                expected_max_date=expected_max_date,
                tolerance_days=tolerance_days,
                deadline=deadline,
            )
        if check == "coverage":
            return self._coverage_check(
                table,
                schema,
                time_field=time_field,
                window=window,
                min_observed_ratio=min_observed_ratio,
                deadline=deadline,
            )
        if check == "referential":
            return self._referential_check(
                table, schema, referenced=referenced, deadline=deadline
            )
        return self._unknown(table, check, "unsupported_check")

    def _grain_check(
        self,
        table: str,
        check: str,
        grain: list[str],
        schema: Any,
        *,
        deadline: float,
    ) -> QualityCheckResult:
        visible = {column.name for column in schema.columns}
        missing = [column for column in grain if column not in visible]
        if missing:
            return self._unknown(
                table,
                check,
                "column_not_visible:" + ",".join(missing),
                evidence={"grain_columns": list(grain)},
            )
        budget = self.budget.max_rows_scanned_per_table
        quoted_table = self._quote(table)
        columns_sql = ", ".join(self._quote(column) for column in grain)
        not_null = " AND ".join(
            f"{self._quote(column)} IS NOT NULL" for column in grain
        )
        null_predicate = " OR ".join(
            f"{self._quote(column)} IS NULL" for column in grain
        )
        sample = self._scalar(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted_table} LIMIT {budget + 1})",
            table=table,
            check=check,
            deadline=deadline,
        )
        if isinstance(sample, QualityCheckResult):
            return sample
        evidence: dict[str, Any] = {
            "grain_columns": list(grain),
            "sampled_rows": min(int(sample or 0), budget + 1),
            "row_budget": budget,
            "bounded": int(sample or 0) > budget,
        }
        if not sample:
            return self._unknown(table, check, "empty_table", evidence=evidence)
        null_rows = self._scalar(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted_table} "
            f"WHERE {null_predicate} LIMIT {budget})",
            table=table,
            check=check,
            deadline=deadline,
        )
        if isinstance(null_rows, QualityCheckResult):
            return null_rows
        duplicate_groups = self._scalar(
            f"SELECT COUNT(*) FROM (SELECT {columns_sql} FROM {quoted_table} "
            f"WHERE {not_null} GROUP BY {columns_sql} "
            f"HAVING COUNT(*) > 1 LIMIT {budget})",
            table=table,
            check=check,
            deadline=deadline,
        )
        if isinstance(duplicate_groups, QualityCheckResult):
            return duplicate_groups
        evidence["null_key_rows"] = int(null_rows or 0)
        evidence["duplicate_groups"] = int(duplicate_groups or 0)
        if check == "duplicates":
            if evidence["duplicate_groups"]:
                return QualityCheckResult(
                    table=table,
                    check=check,
                    status="warning",
                    reason="duplicate_rows_for_grouping_key",
                    evidence=evidence,
                )
            if evidence["bounded"]:
                return QualityCheckResult(
                    table=table,
                    check=check,
                    status="warning",
                    reason="bounded_scan_incomplete",
                    evidence=evidence,
                )
            return QualityCheckResult(
                table=table, check=check, status="ok", evidence=evidence
            )
        if evidence["duplicate_groups"]:
            return QualityCheckResult(
                table=table,
                check=check,
                status="error",
                reason="grain_not_unique",
                evidence=evidence,
            )
        if evidence["null_key_rows"]:
            return QualityCheckResult(
                table=table,
                check=check,
                status="error",
                reason="grain_key_has_nulls",
                evidence=evidence,
            )
        if evidence["bounded"]:
            return QualityCheckResult(
                table=table,
                check=check,
                status="warning",
                reason="bounded_scan_incomplete",
                evidence=evidence,
            )
        return QualityCheckResult(
            table=table, check=check, status="ok", evidence=evidence
        )

    def _null_rate_check(
        self,
        table: str,
        columns: Sequence[str],
        schema: Any,
        *,
        max_null_rate: float,
        deadline: float,
    ) -> QualityCheckResult:
        requested = [
            str(column) for column in columns if str(column).strip()
        ][: self.budget.max_columns_per_check]
        if not requested:
            return self._unknown(table, "null_rate", "no_columns_requested")
        visible = {column.name for column in schema.columns}
        budget = self.budget.max_rows_scanned_per_table
        quoted_table = self._quote(table)
        results: dict[str, Any] = {}
        skipped: list[str] = []
        worst_ratio: float | None = None
        for column in requested:
            if column not in visible:
                skipped.append(column)
                continue
            quoted = self._quote(column)
            row = self._row(
                f"SELECT COUNT(*), SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) "
                f"FROM (SELECT {quoted} FROM {quoted_table} LIMIT {budget})",
                table=table,
                check="null_rate",
                deadline=deadline,
            )
            if isinstance(row, QualityCheckResult):
                return row
            sampled = int(row[0] or 0) if row else 0
            nulls = int(row[1] or 0) if row and row[1] is not None else 0
            ratio = round(nulls / sampled, 6) if sampled else None
            results[column] = {"sampled": sampled, "nulls": nulls, "ratio": ratio}
            if ratio is not None:
                worst_ratio = ratio if worst_ratio is None else max(worst_ratio, ratio)
        evidence: dict[str, Any] = {
            "max_null_rate": max_null_rate,
            "row_budget": budget,
            "columns": results,
        }
        if skipped:
            evidence["columns_skipped"] = skipped
        if not results:
            return self._unknown(
                table,
                "null_rate",
                "column_not_visible:" + ",".join(skipped),
                evidence=evidence,
            )
        if worst_ratio is None:
            return self._unknown(table, "null_rate", "empty_table", evidence=evidence)
        if worst_ratio >= 1.0:
            return QualityCheckResult(
                table=table,
                check="null_rate",
                status="error",
                reason="column_entirely_null",
                evidence=evidence,
            )
        if worst_ratio > max_null_rate:
            return QualityCheckResult(
                table=table,
                check="null_rate",
                status="warning",
                reason="null_rate_above_threshold",
                evidence=evidence,
            )
        return QualityCheckResult(
            table=table, check="null_rate", status="ok", evidence=evidence
        )

    def _freshness_check(
        self,
        table: str,
        schema: Any,
        *,
        time_field: str | None,
        expected_max_date: str | None,
        tolerance_days: int,
        deadline: float,
    ) -> QualityCheckResult:
        visible = {column.name for column in schema.columns}
        if not time_field:
            return self._unknown(table, "freshness", "missing_time_field")
        if time_field not in visible:
            return self._unknown(
                table, "freshness", f"column_not_visible:{time_field}"
            )
        # An event-time maximum never proves ingestion completion, so the caller
        # must supply the date the data is expected to reach (publish/SLA bound).
        if not expected_max_date:
            return self._unknown(
                table,
                "freshness",
                "missing_expected_max_date",
                evidence={"time_field": time_field, "time_semantics": "event_time"},
            )
        expected = _coerce_date(expected_max_date)
        if expected is None:
            return self._unknown(
                table,
                "freshness",
                f"unsupported_expected_date:{expected_max_date}",
                evidence={"time_field": time_field},
            )
        raw = self._scalar(
            f"SELECT MAX({self._quote(time_field)}) FROM {self._quote(table)}",
            table=table,
            check="freshness",
            deadline=deadline,
        )
        if isinstance(raw, QualityCheckResult):
            return raw
        if raw is None:
            return self._unknown(
                table,
                "freshness",
                "no_time_values",
                evidence={
                    "time_field": time_field,
                    "time_semantics": "event_time",
                    "expected_max_date": expected.isoformat(),
                },
            )
        observed = _coerce_date(raw)
        if observed is None:
            return self._unknown(
                table,
                "freshness",
                f"unsupported_time_format:{raw}",
                evidence={
                    "time_field": time_field,
                    "time_semantics": "event_time",
                    "expected_max_date": expected.isoformat(),
                },
            )
        lag_days = (expected - observed).days
        evidence = {
            "time_field": time_field,
            "time_semantics": "event_time",
            "observed_max": observed.isoformat(),
            "expected_max_date": expected.isoformat(),
            "lag_days": lag_days,
            "tolerance_days": tolerance_days,
            "ingestion_time_available": False,
        }
        if lag_days <= 0:
            return QualityCheckResult(
                table=table, check="freshness", status="ok", evidence=evidence
            )
        if lag_days <= tolerance_days:
            return QualityCheckResult(
                table=table,
                check="freshness",
                status="warning",
                reason="freshness_within_tolerance",
                evidence=evidence,
            )
        return QualityCheckResult(
            table=table,
            check="freshness",
            status="error",
            reason="stale_event_data",
            evidence=evidence,
        )

    def _coverage_check(
        self,
        table: str,
        schema: Any,
        *,
        time_field: str | None,
        window: tuple[str, str] | None,
        min_observed_ratio: float,
        deadline: float,
    ) -> QualityCheckResult:
        visible = {column.name: column for column in schema.columns}
        if not time_field:
            return self._unknown(table, "coverage", "missing_time_field")
        if time_field not in visible:
            return self._unknown(
                table, "coverage", f"column_not_visible:{time_field}"
            )
        if not window or len(window) != 2:
            return self._unknown(table, "coverage", "missing_window")
        start = _coerce_date(window[0])
        end = _coerce_date(window[1])
        if start is None or end is None:
            return self._unknown(
                table, "coverage", f"unsupported_window:{tuple(window)!r}"
            )
        if end < start:
            return self._unknown(table, "coverage", "window_end_before_start")
        expected_days = (end - start).days + 1
        if expected_days > self.MAX_WINDOW_DAYS:
            return self._unknown(
                table,
                "coverage",
                "window_too_large",
                evidence={
                    "expected_days": expected_days,
                    "cap": self.MAX_WINDOW_DAYS,
                },
            )
        integer_key = any(
            marker in (visible[time_field].data_type or "").upper()
            for marker in _INTEGER_TYPES
        )
        quoted = self._quote(time_field)
        date_expression = (
            quoted if integer_key else f"substr(CAST({quoted} AS TEXT), 1, 10)"
        )
        if integer_key:
            lower, upper = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        else:
            lower, upper = start.isoformat(), end.isoformat()
        observed = self._scalar(
            f"SELECT COUNT(DISTINCT {date_expression}) FROM {self._quote(table)} "
            f"WHERE {quoted} IS NOT NULL AND {quoted} >= '{lower}' "
            f"AND {quoted} <= '{upper}'",
            table=table,
            check="coverage",
            deadline=deadline,
        )
        if isinstance(observed, QualityCheckResult):
            return observed
        observed_days = min(int(observed or 0), expected_days)
        missing_days = expected_days - observed_days
        ratio = (
            round(min(1.0, observed_days / expected_days), 6) if expected_days else 1.0
        )
        evidence = {
            "time_field": time_field,
            "time_semantics": "event_time",
            "window": [start.isoformat(), end.isoformat()],
            "expected_days": expected_days,
            "observed_days": observed_days,
            "missing_days": missing_days,
            "coverage_ratio": ratio,
            "min_observed_ratio": min_observed_ratio,
        }
        if observed_days == 0:
            return QualityCheckResult(
                table=table,
                check="coverage",
                status="error",
                reason="no_data_in_window",
                evidence=evidence,
            )
        if ratio < min_observed_ratio:
            return QualityCheckResult(
                table=table,
                check="coverage",
                status="error",
                reason="coverage_below_threshold",
                evidence=evidence,
            )
        if missing_days:
            return QualityCheckResult(
                table=table,
                check="coverage",
                status="warning",
                reason="missing_days_in_window",
                evidence=evidence,
            )
        return QualityCheckResult(
            table=table, check="coverage", status="ok", evidence=evidence
        )

    def _referential_check(
        self,
        table: str,
        schema: Any,
        *,
        referenced: tuple[str, str] | None,
        deadline: float,
    ) -> QualityCheckResult:
        if not referenced or len(referenced) != 2:
            return self._unknown(table, "referential", "missing_referenced_pair")
        referenced_table, referenced_column = str(referenced[0]), str(referenced[1])
        if not _IDENTIFIER.match(referenced_table) or not _IDENTIFIER.match(
            referenced_column
        ):
            return self._unknown(table, "referential", "invalid_referenced_pair")
        try:
            referenced_schema = self.database_tool.describe_table(referenced_table)
        except UnsafeSQLError as exc:
            return self._unknown(table, "referential", f"policy_denied:{exc}")
        except Exception as exc:
            return self._unknown(
                table, "referential", f"referenced_table_unavailable:{exc}"
            )
        referenced_columns = {column.name for column in referenced_schema.columns}
        if referenced_column not in referenced_columns:
            return self._unknown(
                table, "referential", f"column_not_visible:{referenced_column}"
            )
        column = self._local_key_column(schema, referenced_table)
        if column is None:
            return self._unknown(
                table, "referential", "missing_local_column"
            )
        local_column = self._quote(column)
        budget = self.budget.max_rows_scanned_per_table
        orphans = self._scalar(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {self._quote(table)} AS src "
            f"WHERE src.{local_column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {self._quote(referenced_table)} AS ref "
            f"WHERE ref.{self._quote(referenced_column)} = src.{local_column}) "
            f"LIMIT {budget + 1})",
            table=table,
            check="referential",
            deadline=deadline,
        )
        if isinstance(orphans, QualityCheckResult):
            return orphans
        evidence = {
            "column": column,
            "referenced_table": referenced_table,
            "referenced_column": referenced_column,
            "orphan_count": int(orphans or 0),
            "row_budget": budget,
            "bounded": int(orphans or 0) > budget,
        }
        if evidence["orphan_count"]:
            return QualityCheckResult(
                table=table,
                check="referential",
                status="error",
                reason="orphan_foreign_keys",
                evidence=evidence,
            )
        return QualityCheckResult(
            table=table, check="referential", status="ok", evidence=evidence
        )

    # ------------------------------------------------------------ SQL helpers

    def _resolve_grain(
        self, schema: Any, grain_columns: Sequence[str] | None
    ) -> list[str] | str:
        if grain_columns:
            return [str(column) for column in grain_columns]
        primary_key = [column.name for column in schema.columns if column.primary_key]
        if primary_key:
            return primary_key
        return "no_grain_declared"

    @staticmethod
    def _local_key_column(schema: Any, referenced_table: str) -> str | None:
        """Pick the local key column referencing ``referenced_table``."""
        for foreign_key in schema.foreign_keys:
            if foreign_key.referenced_table == referenced_table:
                return foreign_key.column
        singular = referenced_table
        for prefix in ("dim_", "fact_", "bridge_", "tbl_"):
            if singular.startswith(prefix):
                singular = singular[len(prefix) :]
                break
        candidates = sorted(
            column.name
            for column in schema.columns
            if column.name.lower().endswith("_id")
            or column.name.lower() in {"id", "key"}
        )
        preferences = [f"{singular}_id"]
        if singular.endswith("s"):
            preferences.append(f"{singular[:-1]}_id")
        preferences.extend(["id", "key"])
        for preference in preferences:
            if preference in candidates:
                return preference
        return candidates[0] if candidates else None

    def _scalar(
        self, sql: str, *, table: str, check: str, deadline: float
    ) -> Any:
        row = self._row(sql, table=table, check=check, deadline=deadline)
        if isinstance(row, QualityCheckResult):
            return row
        return row[0] if row else None

    def _row(
        self, sql: str, *, table: str, check: str, deadline: float
    ) -> Any:
        if self._clock() >= deadline:
            return self._timeout_result(table, check)
        restore = self._install_deadline_handler(deadline)
        try:
            result = self.database_tool.execute_sql(sql)
        except UnsafeSQLError as exc:
            return self._unknown(table, check, f"policy_denied:{exc}")
        except Exception as exc:
            return self._unknown(table, check, f"query_failed:{exc}")
        finally:
            restore()
        if not result.rows:
            return []
        return result.rows[0]

    def _install_deadline_handler(self, deadline: float) -> Callable[[], None]:
        """Interrupt a long-running SQLite statement once the deadline passes."""
        connection = getattr(
            getattr(self.database_tool, "connector", None), "_connection", None
        )
        from queryforge.orchestration.tools.budget import install_sql_deadline_handler
        return install_sql_deadline_handler(connection, deadline, clock=self._clock).restore

    def _timeout_result(self, table: str, check: str) -> QualityCheckResult:
        return QualityCheckResult(
            table=table,
            check=check,
            status="unknown",
            reason="timeout",
            evidence={"timeout_seconds": self.budget.timeout_seconds},
        )

    @staticmethod
    def _quote(identifier: str) -> str:
        return DatabaseTool._quote_identifier(identifier)

    @staticmethod
    def _unknown(
        table: str,
        check: str,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> QualityCheckResult:
        return QualityCheckResult(
            table=table,
            check=check,
            status="unknown",
            reason=reason,
            evidence=evidence or {},
        )


def _noop() -> None:
    return None


def _normalize_request(request: Any) -> tuple[str, Sequence[str], dict[str, Any]]:
    if isinstance(request, dict):
        table = str(request.get("table") or request.get("table_name") or "")
        checks = list(request.get("checks") or [])
        options = {
            key: value
            for key, value in request.items()
            if key not in {"table", "table_name", "checks"}
        }
        return table, checks, options
    if isinstance(request, (tuple, list)):
        if len(request) == 2:
            return str(request[0]), list(request[1]), {}
        if len(request) == 3:
            return str(request[0]), list(request[1]), dict(request[2] or {})
    raise ValueError(
        "quality request must be (table, checks[, options]) or a mapping"
    )


def _coerce_date(value: Any) -> date | None:
    """Coerce ISO dates, ``YYYYMMDD`` keys and integers into a calendar date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _coerce_date(str(value))
    if isinstance(value, float):
        return _coerce_date(str(int(value)))
    if isinstance(value, bytes):
        return _coerce_date(value.decode("utf-8", "ignore"))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _ISO_DATE.match(text[:10]):
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        if _KEY_DATE.match(text):
            try:
                return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return None
    return None
