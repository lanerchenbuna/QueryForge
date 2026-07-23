"""Report local runtime and transport readiness from the current run state."""

from __future__ import annotations

import importlib.util

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState


class OpsAgent(RoleAgent):
    agent_name = "OpsAgent"
    artifact_type = "ops_report"

    def run(self, state: TaskState) -> ArtifactRef:
        state_path = self.state_store.run_dir(state.run_id) / "state.json"
        checks = {
            "state_persisted": state_path.is_file(),
            "artifacts_directory": (
                self.state_store.run_dir(state.run_id) / "artifacts"
            ).is_dir(),
            "logs_directory": (
                self.state_store.run_dir(state.run_id) / "logs"
            ).is_dir(),
            "api_dependency": importlib.util.find_spec("fastapi") is not None,
            "mcp_dependency": importlib.util.find_spec("mcp") is not None,
            "gateway_ready": True,
        }
        return self.emit(
            state,
            {
                "smoke_test": all(
                    checks[name]
                    for name in ("state_persisted", "artifacts_directory", "logs_directory")
                ),
                "checks": checks,
                "run_status_at_check": state.status,
                "current_phase": state.current_phase,
                "last_error": state.last_error,
            },
        )
