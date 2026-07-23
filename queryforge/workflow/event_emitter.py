"""Thread-safe, bounded workflow progress events without result payloads."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


EventType = Literal[
    "run_started",
    "node_started",
    "node_completed",
    "node_failed",
    "phase_started",
    "phase_completed",
    "artifact_created",
    "retrying",
    "final_result",
]


class WorkflowEvent(BaseModel):
    """A progress-only event. SQL text, prompts, and result rows are excluded."""

    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    event_type: EventType
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_id: str
    node_name: str | None = None
    phase_name: str | None = None
    artifact_type: str | None = None
    status: str | None = None
    message: str | None = None
    data: dict[str, Any] | None = None


class EventEmitter:
    """Collect bounded progress events and synchronously notify subscribers."""

    def __init__(self, buffer_size: int = 100) -> None:
        if buffer_size < 1:
            raise ValueError("streaming event buffer size must be positive")
        self._events: deque[WorkflowEvent] = deque(maxlen=buffer_size)
        self._callbacks: list[Callable[[WorkflowEvent], None]] = []
        self._lock = Lock()

    def emit(self, event: WorkflowEvent) -> None:
        with self._lock:
            self._events.append(event)
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                # Progress callbacks must never affect the SQL workflow.
                continue

    def on_event(self, callback: Callable[[WorkflowEvent], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def get_events(self) -> list[WorkflowEvent]:
        with self._lock:
            return list(self._events)


def emit_event(
    emitter: EventEmitter | None,
    event_type: EventType,
    run_id: str,
    **fields: Any,
) -> None:
    """Emit only explicitly supplied, progress-safe metadata."""
    if emitter is not None:
        emitter.emit(WorkflowEvent(event_type=event_type, run_id=run_id, **fields))
