"""Thread-safe, bounded workflow progress events without result payloads.

Step 14 turns the progress stream into a *protocol*:

* every event carries ``protocol_version``, a per-run gapless ``sequence`` and
  the stable identifiers ``run_id`` / ``task_id`` / node or tool;
* exactly one *terminal* event (``final_result``) closes a run, and it carries an
  ``outcome`` of ``success`` / ``partial`` / ``blocked`` / ``failed`` /
  ``cancelled``;
* nothing may follow a terminal event. A run that ends without one is a protocol
  violation, which :class:`~queryforge.application.event_stream.WorkflowEventStream`
  reports rather than silently treating as success.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


LOGGER = logging.getLogger("queryforge.events")

#: Version of the streaming event protocol. Bump only for breaking changes.
PROTOCOL_VERSION = "1"

#: One terminal outcome per run. ``cancelled`` is distinct from ``failed`` so a
#: client disconnect is never reported as a workflow failure.
EventOutcome = Literal["success", "partial", "blocked", "failed", "cancelled"]
OUTCOMES: tuple[str, ...] = ("success", "partial", "blocked", "failed", "cancelled")

#: The single terminal event type.
TERMINAL_EVENT_TYPE = "final_result"

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

#: Mapping from a workflow/agent status string onto the protocol outcome set.
_STATUS_OUTCOMES: dict[str, str] = {
    "success": "success",
    "completed": "success",
    "planned": "success",
    "partial": "partial",
    "degraded": "partial",
    "blocked": "blocked",
    "needs_clarification": "blocked",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def resolve_outcome(status: str | None, error: str | None = None) -> str:
    """Map a workflow status onto the protocol outcome vocabulary.

    Unknown statuses fall back to ``failed`` when an error is present and to
    ``partial`` otherwise, so an unfamiliar status can never reach a client as a
    success.
    """

    if status:
        outcome = _STATUS_OUTCOMES.get(status.strip().lower())
        if outcome is not None:
            return outcome
    return "failed" if error else "partial"


class WorkflowEvent(BaseModel):
    """A progress-only event. SQL text, prompts, and result rows are excluded.

    The terminal ``final_result`` event is the one exception: it carries the
    serialized workflow result (or a failure message) so streaming transports
    can deliver the answer without polling a second channel.
    """

    protocol_version: str = PROTOCOL_VERSION
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    event_type: EventType
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_id: str
    #: Monotonic, gapless per-run counter assigned by :class:`EventEmitter`.
    sequence: int = 0
    task_id: str | None = None
    node_name: str | None = None
    tool: str | None = None
    phase_name: str | None = None
    artifact_type: str | None = None
    status: str | None = None
    #: Set on the terminal event only; see :data:`EventOutcome`.
    outcome: EventOutcome | None = None
    message: str | None = None
    data: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.event_type == TERMINAL_EVENT_TYPE


class EventEmitter:
    """Collect bounded progress events and synchronously notify subscribers.

    The emitter owns protocol bookkeeping: it assigns per-run sequences, fills
    missing identifiers from bound run metadata, normalizes the terminal outcome
    and refuses to publish anything after a run's terminal event.
    """

    def __init__(self, buffer_size: int = 100) -> None:
        if buffer_size < 1:
            raise ValueError("streaming event buffer size must be positive")
        self._events: deque[WorkflowEvent] = deque(maxlen=buffer_size)
        self._callbacks: list[Callable[[WorkflowEvent], None]] = []
        self._lock = Lock()
        self._sequences: dict[str, int] = {}
        self._terminals: dict[str, WorkflowEvent] = {}
        self._run_metadata: dict[str, Any] = {}
        self._post_terminal_events = 0

    # ------------------------------------------------------------- protocol

    def bind(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        tool: str | None = None,
    ) -> None:
        """Bind stable identifiers that later events inherit when unset.

        The run identity (and, once the orchestrator has created the task state,
        the task identity) is bound once here instead of being repeated at every
        call site, so transports always see consistent identifiers.
        """

        with self._lock:
            if run_id:
                self._run_metadata["run_id"] = run_id
            if task_id:
                self._run_metadata["task_id"] = task_id
            if tool:
                self._run_metadata["tool"] = tool

    @property
    def run_metadata(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._run_metadata)

    def terminal_event(self, run_id: str) -> WorkflowEvent | None:
        with self._lock:
            return self._terminals.get(run_id)

    def has_terminal(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._terminals

    @property
    def post_terminal_events(self) -> int:
        """Events rejected because a terminal event already closed the run."""

        with self._lock:
            return self._post_terminal_events

    def emit(self, event: WorkflowEvent) -> None:
        with self._lock:
            metadata = self._run_metadata
            if event.task_id is None and metadata.get("task_id"):
                event.task_id = metadata["task_id"]
            if event.tool is None and metadata.get("tool"):
                event.tool = metadata["tool"]
            if event.terminal:
                if event.run_id in self._terminals:
                    self._post_terminal_events += 1
                    LOGGER.warning(
                        "stream_protocol_violation run_id=%s duplicate_terminal=true",
                        event.run_id,
                    )
                    return
                if event.outcome is None:
                    event.outcome = resolve_outcome(event.status, event.error)
            elif event.run_id in self._terminals:
                # Nothing may follow a terminal event for the same run.
                self._post_terminal_events += 1
                LOGGER.warning(
                    "stream_protocol_violation run_id=%s event_after_terminal=%s",
                    event.run_id,
                    event.event_type,
                )
                return
            event.sequence = self._sequences.get(event.run_id, 0) + 1
            self._sequences[event.run_id] = event.sequence
            self._events.append(event)
            if event.terminal:
                self._terminals[event.run_id] = event
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
