"""Project existing execution and reflection checks into a QA artifact."""

from __future__ import annotations

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class DataQAAgent(RoleAgent):
    agent_name = "DataQAAgent"
    artifact_type = "qa_report"

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
