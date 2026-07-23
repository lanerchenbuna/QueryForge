"""Generate a static report artifact after verified SQL execution."""

from __future__ import annotations

from queryforge.workflow.report_generator import ReportGenerator
from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class ReportAgent(RoleAgent):
    agent_name = "ReportAgent"
    artifact_type = "report_artifact"

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        if context.execution_result is None or context.sql_context is None:
            return self.emit(
                state,
                {
                    "reason": "Report requires a completed SQL execution result.",
                    "file_path": None,
                },
                status="degraded",
            )
        try:
            artifact = ReportGenerator(
                context.report_output_dir,
                max_rows=context.report_max_rows,
                max_charts=context.report_max_charts,
            ).generate(context)
        except Exception as exc:
            return self.emit(
                state,
                {
                    "reason": "Report generation failed; query output remains available.",
                    "file_path": None,
                    "error": str(exc),
                },
                status="degraded",
            )
        payload = artifact.model_dump(mode="json")
        if context.final_output is not None:
            context.final_output["report"] = payload
        return self.emit(state, payload)
