"""Expose SQL produced by existing GenSqlNode/FixNode as a candidate artifact."""

from __future__ import annotations

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class SQLDeveloperAgent(RoleAgent):
    agent_name = "SQLDeveloperAgent"
    artifact_type = "sql_candidate"

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        if context.sql_context is None:
            return self.emit(
                state,
                {
                    "attempt": self._next_attempt(state),
                    "sql": None,
                    "dialect": "sqlite",
                    "explanation": None,
                    "tables_used": [],
                    "generated_by": "existing GenSqlNode/FixNode",
                    "reason": "GenSqlNode/FixNode did not produce an SQL candidate",
                    "governance_required": True,
                },
                status="blocked",
            )
        return self.emit(
            state,
            {
                "attempt": self._next_attempt(state),
                "sql": context.sql_context.sql,
                "dialect": "sqlite",
                "explanation": context.sql_context.explanation,
                "tables_used": context.sql_context.tables_used,
                "generated_by": "existing GenSqlNode/FixNode",
                "fix_attempts": [
                    fix.model_dump(mode="json") for fix in context.fix_attempts
                ],
                "governance_required": True,
                "reasoning": (
                    context.reasoning_result.model_dump(mode="json")
                    if context.reasoning_result
                    else None
                ),
                "reasoning_validation": context.reasoning_validation,
            },
        )

    def _next_attempt(self, state: TaskState) -> int:
        return (
            len(
                [
                    artifact for artifact in state.artifacts
                    if artifact.artifact_type == self.artifact_type
                ]
            )
            + 1
        )
