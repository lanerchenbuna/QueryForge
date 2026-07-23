"""Reuse QueryForge visualization rules and always emit a presentation artifact."""

from __future__ import annotations

from queryforge.workflow.node.visualization_node import VisualizationNode
from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class VisualizationAgent(RoleAgent):
    agent_name = "VisualizationAgent"
    artifact_type = "visualization_artifact"

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        if context.execution_result is None or context.sql_context is None:
            return self.emit(
                state,
                {
                    "chart_type": "table",
                    "chart_config": {
                        "format": "table",
                        "columns": [],
                        "rows": [],
                    },
                    "chart_path": None,
                    "reason": "Visualization requires SQL and an execution result.",
                    "error": "missing_execution_result",
                    "source": "agent_team_fallback",
                    "table_fallback": True,
                },
                status="degraded",
            )
        try:
            visualization = (
                context.visualization_result
                or VisualizationNode.build_visualization(
                    question=context.task.question,
                    sql=context.sql_context.sql,
                    columns=context.execution_result.columns,
                    rows=context.execution_result.rows,
                )
            )
        except Exception as exc:
            return self.emit(
                state,
                {
                    "chart_type": "table",
                    "chart_config": {
                        "format": "table",
                        "columns": context.execution_result.columns,
                        "rows": context.execution_result.rows,
                    },
                    "chart_path": None,
                    "reason": "Visualization generation failed; table fallback is available.",
                    "error": str(exc),
                    "source": "agent_team_fallback",
                    "table_fallback": True,
                },
                status="degraded",
            )
        return self.emit(
            state,
            {
                **visualization.model_dump(mode="json"),
                "source": (
                    "existing VisualizationNode output"
                    if context.visualization_result
                    else "existing VisualizationNode rules"
                ),
                "table_fallback": visualization.chart_type == "table",
            },
            status="degraded" if visualization.error else "valid",
        )
