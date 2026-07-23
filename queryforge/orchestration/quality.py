"""Artifact validation and deterministic quality gates for Agent Team runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from queryforge.orchestration.schemas import TaskState, VALID_ARTIFACT_STATUSES


class ArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class AnalysisRequestPayload(ArtifactPayload):
    question: str
    goal: str | None = None
    objective: str | None = None


class KnowledgeContextPayload(ArtifactPayload):
    question: str
    retrieval_status: dict[str, Any]


class SchemaPlanPayload(ArtifactPayload):
    tables: list[dict[str, Any]]
    selected_tables: list[str]


class SqlCandidatePayload(ArtifactPayload):
    sql: str | None = None


class GovernanceReportPayload(ArtifactPayload):
    allowed: bool
    checked_before_execution: bool


class QAReportPayload(ArtifactPayload):
    passed: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)


class VisualizationArtifactPayload(ArtifactPayload):
    chart_type: str


class OpsReportPayload(ArtifactPayload):
    smoke_test: bool


class DeliveryReportPayload(ArtifactPayload):
    run_id: str
    task_id: str
    status: str
    summary: str


class ReviewReportPayload(ArtifactPayload):
    findings: list[dict[str, Any]]
    executed: bool


ARTIFACT_SCHEMAS: dict[str, type[ArtifactPayload]] = {
    "analysis_request": AnalysisRequestPayload,
    "knowledge_context": KnowledgeContextPayload,
    "schema_plan": SchemaPlanPayload,
    "sql_candidate": SqlCandidatePayload,
    "governance_report": GovernanceReportPayload,
    "qa_report": QAReportPayload,
    "visualization_artifact": VisualizationArtifactPayload,
    "ops_report": OpsReportPayload,
    "delivery_report": DeliveryReportPayload,
    "review_report": ReviewReportPayload,
}


def normalize_artifact(
    artifact_type: str,
    payload: dict[str, Any],
    status: str,
) -> tuple[str, dict[str, Any]]:
    """Validate an artifact status and payload without rejecting extra fields."""

    if status not in VALID_ARTIFACT_STATUSES:
        raise ValueError(
            f"Invalid artifact status {status!r}; expected one of "
            f"{sorted(VALID_ARTIFACT_STATUSES)}"
        )
    schema = ARTIFACT_SCHEMAS.get(artifact_type)
    if schema is None:
        return status, payload
    try:
        schema.model_validate(payload)
    except ValidationError as exc:
        degraded = dict(payload)
        degraded["artifact_schema_error"] = exc.errors()
        degraded["artifact_schema_status"] = "degraded"
        return "degraded", degraded
    return status, payload


def warning_record(
    *,
    phase: str,
    artifact_type: str,
    producer: str,
    reason: str,
) -> dict[str, str]:
    return {
        "phase": phase,
        "artifact_type": artifact_type,
        "producer": producer,
        "reason": reason,
    }


def append_warning(
    state: TaskState,
    *,
    phase: str,
    artifact_type: str,
    producer: str,
    reason: str,
) -> None:
    record = warning_record(
        phase=phase,
        artifact_type=artifact_type,
        producer=producer,
        reason=reason,
    )
    if record not in state.warnings:
        state.warnings.append(record)
