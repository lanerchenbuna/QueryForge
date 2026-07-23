"""Shared artifact emission support for lightweight role agents."""

from __future__ import annotations

from typing import Any

from queryforge.orchestration.quality import normalize_artifact
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import ArtifactRef, TaskState


class RoleAgent:
    agent_name = "RoleAgent"
    artifact_type = "role_artifact"

    def __init__(self, state_store: AgentTeamStateStore) -> None:
        self.state_store = state_store

    def emit(
        self,
        state: TaskState,
        payload: dict[str, Any],
        *,
        status: str = "valid",
        artifact_type: str | None = None,
    ) -> ArtifactRef:
        resolved_type = artifact_type or self.artifact_type
        normalized_status, normalized_payload = normalize_artifact(
            resolved_type,
            payload,
            status,
        )
        return self.state_store.write_artifact(
            state,
            artifact_type=resolved_type,
            producer=self.agent_name,
            status=normalized_status,
            payload=normalized_payload,
        )
