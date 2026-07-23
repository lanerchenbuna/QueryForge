"""Read-only operational contract validation for governed semantic models."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from queryforge.domain.semantic.schemas import (
    ContractQualityRule,
    SemanticEntity,
    SemanticModel,
)


class ContractCheck(BaseModel):
    """One schema or data-quality assertion with auditable observed evidence."""

    scope: Literal["entity", "dimension", "metric", "join_path", "relationship"]
    subject: str
    rule: str
    severity: Literal["error", "warning"] = "error"
    status: Literal["passed", "failed", "skipped"]
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class ContractValidationReport(BaseModel):
    """Report emitted before a semantic model is made available to QueryForge."""

    model_name: str
    database_path: str
    checked_at: str
    checks: list[ContractCheck] = Field(default_factory=list)

    @property
    def blocking_failures(self) -> list[ContractCheck]:
        return [
            check
            for check in self.checks
            if check.status == "failed" and check.severity == "error"
        ]

    @property
    def passed(self) -> bool:
        return not self.blocking_failures

    def summary(self) -> dict[str, int | bool]:
        return {
            "passed": self.passed,
            "checks": len(self.checks),
            "passed_checks": sum(check.status == "passed" for check in self.checks),
            "failed_checks": sum(check.status == "failed" for check in self.checks),
            "skipped_checks": sum(check.status == "skipped" for check in self.checks),
            "blocking_failures": len(self.blocking_failures),
        }


class SemanticContractValidator:
    """Validate operational data contracts without writing to the source database."""

    @classmethod
    def validate(
        cls,
        model: SemanticModel,
        database_path: str | Path,
    ) -> ContractValidationReport:
        database = Path(database_path).expanduser().resolve()
        if not database.is_file():
            raise ValueError(f"SQLite database does not exist: {database}")
        report = ContractValidationReport(
            model_name=model.name,
            database_path=str(database),
            checked_at=datetime.now(UTC).isoformat(),
        )
        uri = f"{database.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            entity_by_name = {entity.name: entity for entity in model.entities}
            for entity in model.entities:
                cls._validate_entity_schema(connection, entity, report)
                cls._validate_primary_key(connection, entity, report)
                cls._validate_rules(
                    connection,
                    report,
                    scope="entity",
                    subject=entity.name,
                    table=entity.table,
                    default_column=None,
                    rules=entity.quality_rules,
                )
                for dimension in entity.dimensions:
                    cls._validate_rules(
                        connection,
                        report,
                        scope="dimension",
                        subject=f"{entity.name}.{dimension.name}",
                        table=entity.table,
                        default_column=dimension.column,
                        rules=dimension.quality_rules,
                    )

            for metric in model.metrics:
                entity = entity_by_name.get(metric.entity)
                if entity is None:
                    continue
                cls._validate_rules(
                    connection,
                    report,
                    scope="metric",
                    subject=metric.name,
                    table=entity.table,
                    default_column=None,
                    rules=metric.quality_rules,
                )

            relationships = {relationship.name: relationship for relationship in model.relationships}
            for relationship in model.relationships:
                cls._validate_foreign_key(
                    connection,
                    report,
                    scope="relationship",
                    subject=relationship.name,
                    source_ref=relationship.from_ref,
                    target_ref=relationship.to_ref,
                    severity="error",
                )
            for join_path in model.join_paths:
                for relationship_name in join_path.relationships:
                    relationship = relationships.get(relationship_name)
                    if relationship is None:
                        continue
                    cls._validate_foreign_key(
                        connection,
                        report,
                        scope="join_path",
                        subject=f"{join_path.name}:{relationship_name}",
                        source_ref=relationship.from_ref,
                        target_ref=relationship.to_ref,
                        severity="error",
                    )
                for rule in join_path.quality_rules:
                    if rule.rule != "foreign_key":
                        report.checks.append(
                            ContractCheck(
                                scope="join_path",
                                subject=join_path.name,
                                rule=rule.rule,
                                severity=rule.severity,
                                status="skipped",
                                message=(
                                    "Join Path quality rules currently support "
                                    "foreign_key through declared relationships."
                                ),
                            )
                        )
        return report

    @classmethod
    def _validate_entity_schema(
        cls,
        connection: sqlite3.Connection,
        entity: SemanticEntity,
        report: ContractValidationReport,
    ) -> None:
        actual = cls._table_columns(connection, entity.table)
        expected = set(entity.expected_columns)
        if not expected:
            report.checks.append(
                ContractCheck(
                    scope="entity",
                    subject=entity.name,
                    rule="schema_drift",
                    status="skipped",
                    message="No expected_columns contract is configured.",
                )
            )
            return
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        failed = bool(missing) or (
            bool(unexpected) and not entity.allow_additive_columns
        )
        report.checks.append(
            ContractCheck(
                scope="entity",
                subject=entity.name,
                rule="schema_drift",
                status="failed" if failed else "passed",
                expected={
                    "columns": sorted(expected),
                    "allow_additive_columns": entity.allow_additive_columns,
                },
                observed={"missing": missing, "unexpected": unexpected},
                message=(
                    "Physical table matches the declared schema contract."
                    if not failed
                    else "Physical table drift violates the declared schema contract."
                ),
            )
        )

    @classmethod
    def _validate_primary_key(
        cls,
        connection: sqlite3.Connection,
        entity: SemanticEntity,
        report: ContractValidationReport,
    ) -> None:
        columns = entity.primary_key or entity.effective_grain
        if not columns:
            report.checks.append(
                ContractCheck(
                    scope="entity",
                    subject=entity.name,
                    rule="primary_key",
                    status="skipped",
                    message="No primary key or grain is declared.",
                )
            )
            return
        available = cls._table_columns(connection, entity.table)
        missing = sorted(set(columns) - available)
        if missing:
            report.checks.append(
                ContractCheck(
                    scope="entity",
                    subject=entity.name,
                    rule="primary_key",
                    status="failed",
                    expected={"columns": columns},
                    observed={"missing_columns": missing},
                    message="Primary key contract references missing physical columns.",
                )
            )
            return
        null_predicate = " OR ".join(
            f"{_quote(column)} IS NULL" for column in columns
        )
        null_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote(entity.table)} WHERE {null_predicate}"
            ).fetchone()[0]
        )
        grouped = ", ".join(_quote(column) for column in columns)
        duplicate_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM (SELECT {grouped} FROM {_quote(entity.table)} "
                f"GROUP BY {grouped} HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
        report.checks.append(
            ContractCheck(
                scope="entity",
                subject=entity.name,
                rule="primary_key",
                status="passed" if not null_count and not duplicate_count else "failed",
                expected={"columns": columns, "null_rows": 0, "duplicate_groups": 0},
                observed={
                    "null_rows": null_count,
                    "duplicate_groups": duplicate_count,
                },
                message="Primary key/grain is non-null and unique.",
            )
        )

    @classmethod
    def _validate_rules(
        cls,
        connection: sqlite3.Connection,
        report: ContractValidationReport,
        *,
        scope: Literal["entity", "dimension", "metric"],
        subject: str,
        table: str,
        default_column: str | None,
        rules: list[ContractQualityRule],
    ) -> None:
        for rule in rules:
            column_table, column = _resolve_column(rule.column, table, default_column)
            if rule.rule == "foreign_key":
                if column is None:
                    report.checks.append(
                        ContractCheck(
                            scope=scope,
                            subject=subject,
                            rule=rule.rule,
                            severity=rule.severity,
                            status="skipped",
                            message="foreign_key rule requires a column or scoped dimension.",
                        )
                    )
                    continue
                cls._validate_foreign_key(
                    connection,
                    report,
                    scope=scope,
                    subject=subject,
                    source_ref=f"{column_table}.{column}",
                    target_ref=f"{rule.referenced_table}.{rule.referenced_column}",
                    severity=rule.severity,
                )
                continue
            if column is None:
                report.checks.append(
                    ContractCheck(
                        scope=scope,
                        subject=subject,
                        rule=rule.rule,
                        severity=rule.severity,
                        status="skipped",
                        message="Rule requires column or a scoped dimension.",
                    )
                )
                continue
            if column not in cls._table_columns(connection, column_table):
                report.checks.append(
                    ContractCheck(
                        scope=scope,
                        subject=subject,
                        rule=rule.rule,
                        severity=rule.severity,
                        status="failed",
                        expected={"column": column},
                        message=f"Physical column {column_table}.{column} does not exist.",
                    )
                )
                continue
            if rule.rule == "null_rate":
                cls._validate_null_rate(
                    connection, report, scope, subject, column_table, column, rule
                )
            elif rule.rule == "unique":
                cls._validate_unique(
                    connection, report, scope, subject, column_table, column, rule
                )
            elif rule.rule == "range":
                cls._validate_range(
                    connection, report, scope, subject, column_table, column, rule
                )

    @classmethod
    def _validate_null_rate(
        cls,
        connection: sqlite3.Connection,
        report: ContractValidationReport,
        scope: Literal["entity", "dimension", "metric"],
        subject: str,
        table: str,
        column: str,
        rule: ContractQualityRule,
    ) -> None:
        total, nulls = connection.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN {_quote(column)} IS NULL "
            f"OR TRIM(CAST({_quote(column)} AS TEXT)) = '' THEN 1 ELSE 0 END) "
            f"FROM {_quote(table)}"
        ).fetchone()
        null_rate = (int(nulls or 0) / int(total)) if total else 0.0
        report.checks.append(
            ContractCheck(
                scope=scope,
                subject=subject,
                rule="null_rate",
                severity=rule.severity,
                status="passed" if null_rate <= (rule.max_null_rate or 0.0) else "failed",
                expected={"max_null_rate": rule.max_null_rate, "column": f"{table}.{column}"},
                observed={"row_count": total, "null_count": int(nulls or 0), "null_rate": null_rate},
                message="Null-rate budget evaluated.",
            )
        )

    @classmethod
    def _validate_unique(
        cls,
        connection: sqlite3.Connection,
        report: ContractValidationReport,
        scope: Literal["entity", "dimension", "metric"],
        subject: str,
        table: str,
        column: str,
        rule: ContractQualityRule,
    ) -> None:
        duplicates = int(
            connection.execute(
                f"SELECT COUNT(*) FROM (SELECT {_quote(column)} FROM {_quote(table)} "
                f"GROUP BY {_quote(column)} HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
        report.checks.append(
            ContractCheck(
                scope=scope,
                subject=subject,
                rule="unique",
                severity=rule.severity,
                status="passed" if not duplicates else "failed",
                expected={"duplicate_groups": 0, "column": f"{table}.{column}"},
                observed={"duplicate_groups": duplicates},
                message="Uniqueness contract evaluated.",
            )
        )

    @classmethod
    def _validate_range(
        cls,
        connection: sqlite3.Connection,
        report: ContractValidationReport,
        scope: Literal["entity", "dimension", "metric"],
        subject: str,
        table: str,
        column: str,
        rule: ContractQualityRule,
    ) -> None:
        minimum, maximum, invalid = connection.execute(
            f"SELECT MIN({_quote(column)}), MAX({_quote(column)}), "
            f"SUM(CASE WHEN ({_quote(column)} IS NOT NULL) AND "
            f"((? IS NOT NULL AND {_quote(column)} < ?) OR "
            f"(? IS NOT NULL AND {_quote(column)} > ?)) THEN 1 ELSE 0 END) "
            f"FROM {_quote(table)}",
            (rule.minimum, rule.minimum, rule.maximum, rule.maximum),
        ).fetchone()
        report.checks.append(
            ContractCheck(
                scope=scope,
                subject=subject,
                rule="range",
                severity=rule.severity,
                status="passed" if not invalid else "failed",
                expected={
                    "minimum": rule.minimum,
                    "maximum": rule.maximum,
                    "column": f"{table}.{column}",
                },
                observed={"minimum": minimum, "maximum": maximum, "invalid_rows": int(invalid or 0)},
                message="Value range contract evaluated.",
            )
        )

    @classmethod
    def _validate_foreign_key(
        cls,
        connection: sqlite3.Connection,
        report: ContractValidationReport,
        *,
        scope: Literal["entity", "dimension", "metric", "join_path", "relationship"],
        subject: str,
        source_ref: str,
        target_ref: str,
        severity: Literal["error", "warning"],
    ) -> None:
        source_table, source_column = _parse_ref(source_ref)
        target_table, target_column = _parse_ref(target_ref)
        source_columns = cls._table_columns(connection, source_table)
        target_columns = cls._table_columns(connection, target_table)
        if source_column not in source_columns or target_column not in target_columns:
            report.checks.append(
                ContractCheck(
                    scope=scope,
                    subject=subject,
                    rule="foreign_key",
                    severity=severity,
                    status="failed",
                    expected={"source": source_ref, "target": target_ref},
                    observed={
                        "source_exists": source_column in source_columns,
                        "target_exists": target_column in target_columns,
                    },
                    message="Foreign-key contract references missing physical columns.",
                )
            )
            return
        orphan_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote(source_table)} AS source "
                f"LEFT JOIN {_quote(target_table)} AS target "
                f"ON source.{_quote(source_column)} = target.{_quote(target_column)} "
                f"WHERE source.{_quote(source_column)} IS NOT NULL "
                f"AND target.{_quote(target_column)} IS NULL"
            ).fetchone()[0]
        )
        report.checks.append(
            ContractCheck(
                scope=scope,
                subject=subject,
                rule="foreign_key",
                severity=severity,
                status="passed" if not orphan_count else "failed",
                expected={"source": source_ref, "target": target_ref, "orphan_rows": 0},
                observed={"orphan_rows": orphan_count},
                message="Referential consistency contract evaluated.",
            )
        )

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
        }


def _parse_ref(reference: str) -> tuple[str, str]:
    table, separator, column = reference.partition(".")
    if not separator or not table or not column:
        raise ValueError(f"Invalid contract reference {reference!r}; expected table.column")
    return table, column


def _resolve_column(
    configured: str | None,
    table: str,
    default: str | None,
) -> tuple[str, str | None]:
    value = configured or default
    if value is None:
        return table, None
    if "." in value:
        return _parse_ref(value)
    return table, value


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
