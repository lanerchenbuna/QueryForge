"""Project existing execution, reflection and data-quality checks into a QA artifact."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState, utc_now
from queryforge.core.schemas.models import Context
from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.security import load_sql_policy
from queryforge.infrastructure.db.adapters import open_database as SQLiteConnector
from queryforge.infrastructure.tools.data_quality_tool import (
    DataQualityBudget,
    DataQualityTool,
)

DatabaseToolFactory = Callable[[Context], Any]


@contextmanager
def open_policy_filtered_database_tool(context: Context) -> Iterator[Any]:
    """Rebuild the workflow's governed DatabaseTool from the shared context.

    ``context.sql_policy`` is the public summary of the policy the run already
    used, so reloading the same source path (or the built-in default when the
    policy was inline) reproduces exactly the same table/column scope. Quality
    checks therefore cannot reach columns the run itself may not query.
    """
    from queryforge.infrastructure.tools.database_tool import DatabaseTool

    policy_source = None
    if isinstance(context.sql_policy, dict):
        source = context.sql_policy.get("source_path")
        policy_source = str(source) if source else None
    policy, loaded_source = load_sql_policy(policy_source)
    with SQLiteConnector(context.task.database_path) as connector:
        yield DatabaseTool(
            connector,
            policy,
            policy_source_path=loaded_source,
        )


class DataQAAgent(RoleAgent):
    agent_name = "DataQAAgent"
    artifact_type = "qa_report"

    #: Distinct metric tables that get runtime quality evidence per run.
    MAX_QUALITY_METRICS = 3

    def __init__(
        self,
        state_store,
        *,
        database_tool_factory: DatabaseToolFactory | None = None,
        quality_budget: DataQualityBudget | None = None,
    ) -> None:
        super().__init__(state_store)
        self.database_tool_factory = database_tool_factory
        self.quality_budget = quality_budget

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        if context.execution_result is None or context.sql_context is None:
            return self.emit(
                state,
                {
                    "passed": False,
                    "row_count": 0,
                    "columns": [],
                    "row_count_consistent": False,
                    "answers_question": False,
                    "empty_result": True,
                    "issues": [
                        {
                            "rule": "missing_execution_result",
                            "severity": "warning",
                            "reason": "QA requires SQL and an execution result.",
                        }
                    ],
                    "quality_checks": [],
                    "quality_status": "skipped",
                    "reflection": None,
                    "retry_recommendation": None,
                    "sql_attempts": [
                        attempt.model_dump(mode="json")
                        for attempt in context.sql_attempt_history
                    ],
                },
                status="warning",
            )
        result = context.execution_result
        issues: list[dict[str, str]] = []
        if result.row_count != len(result.rows):
            issues.append(
                {
                    "rule": "row_count_mismatch",
                    "severity": "error",
                    "reason": "row_count does not match the number of returned rows.",
                }
            )
        if not result.columns:
            issues.append(
                {
                    "rule": "missing_columns",
                    "severity": "error",
                    "reason": "The result has no columns.",
                }
            )
        if result.row_count == 0:
            issues.append(
                {
                    "rule": "empty_result",
                    "severity": "warning",
                    "reason": "The query returned no rows; this may still be semantically valid.",
                }
            )
        quality_payload, quality_issues = self._quality_evidence(context)
        issues.extend(quality_issues)
        context.task_context["data_quality"] = quality_payload
        reflection = context.reflection_result
        reflection_passed = bool(reflection and reflection.success)
        hard_errors = [issue for issue in issues if issue["severity"] == "error"]
        passed = reflection_passed and not hard_errors
        retry_recommendation = None
        if reflection and not reflection.success:
            retry_recommendation = reflection.strategy
        elif hard_errors:
            retry_recommendation = "REGENERATE"
        return self.emit(
            state,
            {
                "passed": passed,
                "row_count": result.row_count,
                "columns": result.columns,
                "row_count_consistent": result.row_count == len(result.rows),
                "answers_question": reflection_passed,
                "empty_result": result.row_count == 0,
                "issues": issues,
                "quality_checks": quality_payload.get("checks", []),
                "quality_status": quality_payload.get("status", "skipped"),
                "quality_reason": quality_payload.get("reason", ""),
                "quality_counts": quality_payload.get("counts", {}),
                "reflection": (
                    reflection.model_dump(mode="json") if reflection else None
                ),
                "retry_recommendation": retry_recommendation,
                "sql_attempts": [
                    attempt.model_dump(mode="json")
                    for attempt in context.sql_attempt_history
                ],
            },
            status="valid" if passed else "warning",
        )

    # -------------------------------------------------------------- quality QA

    @staticmethod
    def _quality_lineage(context: Context) -> dict[str, Any]:
        """Bind quality evidence to the versioned inputs it was computed from."""
        model = context.semantic_model.model if context.semantic_model else None
        return {
            "run_id": context.run_id,
            "database_path": context.task.database_path,
            "semantic_model": model.name if model else None,
            "semantic_model_version": model.version if model else None,
            "semantic_model_source": (
                context.semantic_model.source_path if context.semantic_model else None
            ),
            "checked_at_utc": utc_now(),
        }

    def _quality_evidence(
        self, context: Context
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Run step-08 quality checks for the metrics this task actually used."""
        payload: dict[str, Any] = {
            "status": "skipped",
            "checks": [],
            "counts": {},
            "blocking": False,
            "tables": [],
            "lineage": self._quality_lineage(context),
        }
        if context.semantic_model is None or not context.metric_matches:
            payload["reason"] = "no_semantic_metric_context"
            return payload, []
        requests, descriptions = self._quality_requests(context)
        if not requests:
            payload["reason"] = "no_matched_metric_entity"
            return payload, []
        factory = self.database_tool_factory or open_policy_filtered_database_tool
        try:
            with factory(context) as database_tool:
                tool = DataQualityTool(
                    database_tool,
                    self.quality_budget or DataQualityBudget(),
                )
                report = tool.report(requests)
        except Exception as exc:
            # An unavailable quality tool is not a pass: report it as unknown.
            payload["status"] = "unknown"
            payload["reason"] = f"quality_tool_unavailable:{exc}"
            payload["requirements"] = descriptions
            return payload, []
        payload.update(report)
        payload["requirements"] = descriptions
        quality_issues = [
            {
                "rule": f"data_quality_{check['check']}",
                "severity": "error",
                "reason": (
                    f"{check['table']}.{check['check']} failed: "
                    f"{check.get('reason') or check['status']}"
                ),
            }
            for check in report.get("errors", [])
        ]
        return payload, quality_issues

    def _quality_requests(
        self, context: Context
    ) -> tuple[list[tuple[str, list[str], dict[str, Any]]], list[dict[str, Any]]]:
        assert context.semantic_model is not None
        model = context.semantic_model.model
        entities = {entity.name: entity for entity in model.entities}
        window = self._date_window(context)
        requests: list[tuple[str, list[str], dict[str, Any]]] = []
        descriptions: list[dict[str, Any]] = []
        seen_tables: set[str] = set()
        for match in context.metric_matches:
            if len(requests) >= self.MAX_QUALITY_METRICS:
                break
            metric = match.metric
            entity = entities.get(metric.entity)
            if entity is None or entity.table in seen_tables:
                continue
            seen_tables.add(entity.table)
            columns = _metric_column_refs(
                [metric.expression, *metric.default_filters], entity.table
            )
            checks = ["grain_unique", "duplicates", "null_rate"]
            options: dict[str, Any] = {
                "grain_columns": list(entity.effective_grain),
                "columns": columns or [column.name for column in entity.dimensions],
            }
            requirement = {
                "metric": metric.name,
                "table": entity.table,
                "grain_columns": list(entity.effective_grain),
                "metric_columns": columns,
            }
            if metric.time_field and window is not None:
                time_table, time_column = SemanticModelLoader._parse_column_reference(
                    metric.time_field
                ) or (entity.table, metric.time_field)
                if time_table == entity.table:
                    expected_max_date = self._expected_max_date(context, window)
                    checks.extend(["freshness", "coverage"])
                    options.update(
                        {
                            "time_field": time_column,
                            "window": window,
                            "expected_max_date": expected_max_date,
                        }
                    )
                    requirement.update(
                        {
                            "time_field": time_column,
                            "window": list(window),
                            "expected_max_date": expected_max_date,
                        }
                    )
            requests.append((entity.table, checks, options))
            descriptions.append(requirement)
        return requests, descriptions

    @staticmethod
    def _date_window(context: Context) -> tuple[str, str] | None:
        date_context = context.date_context
        if date_context is None or not date_context.ranges:
            return None
        first = date_context.ranges[0]
        return (first.start_date, first.end_date)

    @staticmethod
    def _expected_max_date(
        context: Context, window: tuple[str, str]
    ) -> str | None:
        """Expected latest event date for a freshness check.

        A closed historical window is expected to be complete up to its own end;
        only open-ended windows (ending on/after the reference date) are expected
        to reach the run's reference date. Without a reference date the check
        cannot run and stays ``unknown``.
        """
        date_context = context.date_context
        if date_context is None or not date_context.reference_date:
            return None
        window_end = window[1]
        if window_end < date_context.reference_date:
            return window_end
        return date_context.reference_date


def _metric_column_refs(
    expressions: list[str], table: str
) -> list[str]:
    """Local physical ``table.column`` references used by a metric contract."""
    pattern = re.compile(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))'
    )
    columns: list[str] = []
    for expression in expressions:
        for match in pattern.finditer(expression):
            if match.group(1) != table:
                continue
            column = match.group(2) or match.group(3)
            if column not in columns:
                columns.append(column)
    return columns
