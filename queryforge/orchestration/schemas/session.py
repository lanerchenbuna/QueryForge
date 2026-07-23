"""Persistent, privacy-bounded conversation memory contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from queryforge.orchestration.schemas import utc_now


class SessionTurn(BaseModel):
    turn_number: int = Field(ge=1)
    question: str
    rewritten_question: str | None = None
    sql: str | None = None
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    result_schema: list[str] = Field(default_factory=list)
    status: Literal["success", "planned", "blocked", "failed"]
    created_at: str = Field(default_factory=utc_now)


class SessionMemory(BaseModel):
    session_id: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    turn_count: int = 0
    last_question: str | None = None
    last_sql: str | None = None
    last_result_schema: list[str] = Field(default_factory=list)
    last_metrics: list[str] = Field(default_factory=list)
    last_dimensions: list[str] = Field(default_factory=list)
    last_filters: list[dict[str, Any]] = Field(default_factory=list)
    last_time_range: dict[str, Any] | None = None
    history: list[SessionTurn] = Field(default_factory=list)
