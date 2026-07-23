"""Local, atomic state and artifact persistence for Agent Team runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from queryforge.workflow.event_emitter import EventEmitter, emit_event
from queryforge.orchestration.schemas import ArtifactRef, TaskState


class AgentTeamStateStore:
    def __init__(
        self,
        root: str | Path = ".queryforge/runs",
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.event_emitter = event_emitter

    def initialize(self, state: TaskState) -> Path:
        run_dir = self.run_dir(state.run_id)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.save_state(state)
        return run_dir

    def run_dir(self, run_id: str) -> Path:
        safe_run_id = "".join(
            character for character in run_id if character.isalnum() or character in "_-"
        )
        if not safe_run_id or safe_run_id != run_id:
            raise ValueError("run_id contains unsafe path characters")
        return self.root / safe_run_id

    def save_state(self, state: TaskState) -> Path:
        state.updated_at = state.updated_at if state.status == "created" else _now()
        path = self.run_dir(state.run_id) / "state.json"
        self._atomic_json(path, state.model_dump(mode="json"))
        return path

    def write_artifact(
        self,
        state: TaskState,
        *,
        artifact_type: str,
        producer: str,
        status: str,
        payload: dict[str, Any],
    ) -> ArtifactRef:
        from queryforge.orchestration.quality import normalize_artifact

        status, payload = normalize_artifact(artifact_type, payload, status)
        sequence = len(state.artifacts) + 1
        filename = f"{sequence:03d}_{artifact_type}.json"
        relative = Path("artifacts") / filename
        reference = ArtifactRef(
            artifact_type=artifact_type,
            producer=producer,
            status=status,
            path=str(relative),
        )
        document = {
            **reference.model_dump(mode="json"),
            "schema_version": "1.0",
            "run_id": state.run_id,
            "task_id": state.task_id,
            "payload": payload,
        }
        self._atomic_json(self.run_dir(state.run_id) / relative, document)
        state.artifacts.append(reference)
        self.save_state(state)
        emit_event(
            self.event_emitter,
            "artifact_created",
            state.run_id,
            artifact_type=artifact_type,
            status=status,
            message=f"Created {artifact_type} artifact.",
        )
        return reference

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)


def _now() -> str:
    from queryforge.orchestration.schemas import utc_now

    return utc_now()
