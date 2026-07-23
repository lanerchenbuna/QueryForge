"""Portable static analytical report contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReportSection(BaseModel):
    id: str
    title: str
    type: Literal["text", "table", "chart", "metrics", "sql"]
    content: dict[str, Any] = Field(default_factory=dict)


class ReportArtifact(BaseModel):
    artifact_type: str = "report"
    title: str
    summary: str
    sections: list[ReportSection] = Field(default_factory=list)
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    data_source: str
    sql: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    file_path: str
    manifest_path: str
