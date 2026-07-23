"""Structured contracts for the integrated Agent Team runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# Task types are strings so deployments can register additional deterministic routes.
# The built-in values remain documented by pipeline_registry.PIPELINES.
TaskType = str
ComplexityProfile = Literal["simple", "complex"]
TaskStatus = Literal[
    "created",
    "routing",
    "running",
    "checkpoint_pending",
    "blocked",
    "failed",
    "completed",
]
ArtifactStatus = Literal["valid", "warning", "blocked", "degraded"]
VALID_ARTIFACT_STATUSES: set[str] = {"valid", "warning", "blocked", "degraded"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RoutingDecision(BaseModel):
    task_type: TaskType
    entrypoint: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    pipeline: str
    requires_orchestrator: bool = True
    complexity_profile: ComplexityProfile = "simple"
    complexity_score: int = Field(default=0, ge=0)
    complexity_reasons: list[str] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    artifact_id: str = Field(default_factory=lambda: f"art_{uuid4().hex}")
    artifact_type: str
    producer: str
    status: ArtifactStatus
    path: str
    created_at: str = Field(default_factory=utc_now)


class CheckpointState(BaseModel):
    checkpoint_id: str = Field(default_factory=lambda: f"cp_{uuid4().hex}")
    checkpoint_type: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    reason: str
    created_at: str = Field(default_factory=utc_now)


class TaskState(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    entrypoint: str
    classification: RoutingDecision
    status: TaskStatus = "created"
    current_phase: str = "routing"
    completed_phases: list[str] = Field(default_factory=list)
    pending_phases: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(
        default_factory=lambda: {"agent": 0, "sql": 0}
    )
    checkpoints: list[CheckpointState] = Field(default_factory=list)
    blocked_phase: str | None = None
    blocked_reason: str | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str | None = None
    original_question: str | None = None
    rewritten_question: str | None = None
    is_followup: bool = False
    followup_reason: str | None = None
    workflow_run_id: str | None = None
    last_error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None


class DeliveryReport(BaseModel):
    run_id: str
    task_id: str
    task_type: TaskType
    status: Literal["planned", "success", "degraded", "failed"]
    pipeline: list[str]
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    summary: str
    result: dict[str, Any] | None = None


from queryforge.orchestration.schemas.session import SessionMemory, SessionTurn
from queryforge.core.schemas.report import ReportArtifact, ReportSection

__all__ = [
    "ArtifactRef",
    "ArtifactStatus",
    "CheckpointState",
    "ComplexityProfile",
    "DeliveryReport",
    "RoutingDecision",
    "ReportArtifact",
    "ReportSection",
    "SessionMemory",
    "SessionTurn",
    "TaskState",
    "TaskStatus",
    "TaskType",
    "VALID_ARTIFACT_STATUSES",
    "utc_now",
]
