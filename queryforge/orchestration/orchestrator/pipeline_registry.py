"""Declarative pipelines for the integrated Agent Team workflow."""

from __future__ import annotations

from queryforge.orchestration.schemas import TaskType


ANALYTICS_PIPELINE = ("analysis", "candidate", "execution", "completion", "delivery")
REVIEW_PIPELINE = ("analysis", "candidate", "review", "completion", "delivery")
METADATA_PIPELINE = ("analysis", "delivery")

PIPELINES: dict[TaskType, tuple[str, ...]] = {
    "ask_sql": ANALYTICS_PIPELINE,
    "sql_review": REVIEW_PIPELINE,
    "metadata_query": METADATA_PIPELINE,
    "troubleshoot_sql": ANALYTICS_PIPELINE,
    "explain_result": ANALYTICS_PIPELINE,
    "build_report": ANALYTICS_PIPELINE,
    "unknown": ANALYTICS_PIPELINE,
}


def register_pipeline(task_type: str, phases: tuple[str, ...]) -> None:
    """Register a validated pipeline for a deployment-specific task type."""
    normalized = task_type.strip()
    if not normalized:
        raise ValueError("task_type must not be blank")
    if not phases or any(not phase.strip() for phase in phases):
        raise ValueError("pipeline must contain non-blank phases")
    PIPELINES[normalized] = tuple(dict.fromkeys(phases))


def pipeline_for(task_type: TaskType) -> tuple[str, ...]:
    return PIPELINES.get(task_type, ANALYTICS_PIPELINE)


def describe_pipelines() -> dict[str, list[str]]:
    return {name: list(phases) for name, phases in PIPELINES.items()}
