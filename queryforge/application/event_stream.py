"""Bounded event buffer that always delivers the terminal event.

Progress-only events may be dropped under backpressure (a slow SSE consumer must
not grow memory without bound), but the run's *terminal* event and the close
sentinel are never dropped: they evict the oldest buffered progress event
instead. The buffer therefore works with a capacity of 1.

A run that closes without ever producing a terminal event is a protocol
violation: it is recorded on the stream (``protocol_violation``) and logged, so
no transport can present "no terminal event" as success.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone
from threading import Condition, Event
from typing import Any

from queryforge.workflow.event_emitter import (
    OUTCOMES,
    PROTOCOL_VERSION,
    TERMINAL_EVENT_TYPE,
    WorkflowEvent,
    resolve_outcome,
)

LOGGER = logging.getLogger("queryforge.events")


class WorkflowEventStream(Iterator[WorkflowEvent]):
    """Consume progress events plus exactly one terminal result/error event.

    ``cancel`` cooperatively asks the worker to stop at the next node boundary
    (and interrupts an in-flight SQL statement); the worker still emits its own
    terminal ``cancelled`` event, so a client always receives exactly one
    terminal event before the stream closes.
    """

    def __init__(self, emitter, queue_maxsize: int = 100) -> None:
        self.emitter = emitter
        self.result: dict | None = None
        self.error: Exception | None = None
        self.queue_maxsize = max(int(queue_maxsize), 1)
        self._items: deque[WorkflowEvent] = deque()
        self._condition = Condition()
        self._cancelled = Event()
        self._closed = False
        self._terminal: WorkflowEvent | None = None
        self._terminal_consumed = False
        self._protocol_violation = False
        self.dropped_progress = 0
        #: True when the workflow finished before cancellation took effect and the
        #: already persisted outcome was therefore **preserved** (not rewritten).
        #: False means the late cancel did rewrite the run (or nothing was
        #: persisted to begin with). It is published on the terminal event.
        self.cancelled_after_completion = False

    # ------------------------------------------------------------- protocol

    @property
    def protocol_version(self) -> str:
        return PROTOCOL_VERSION

    @property
    def run_id(self) -> str:
        metadata = getattr(self.emitter, "run_metadata", None) or {}
        run_id = metadata.get("run_id")
        if run_id:
            return str(run_id)
        if self._terminal is not None:
            return self._terminal.run_id
        try:
            return str(self.emitter.get_events()[0].run_id)
        except (AttributeError, IndexError):
            return "-"

    @property
    def outcome(self) -> str | None:
        """Protocol outcome of the terminal event, if one was produced."""

        if self._terminal is None:
            return None
        return self._terminal.outcome or resolve_outcome(
            self._terminal.status, self._terminal.error
        )

    @property
    def terminal_event(self) -> WorkflowEvent | None:
        return self._terminal

    @property
    def terminal_consumed(self) -> bool:
        """True once the terminal event was handed to the consumer."""

        return self._terminal_consumed

    @property
    def protocol_violation(self) -> bool:
        """True when the run closed without ever producing a terminal event."""

        return self._protocol_violation

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def finished(self) -> bool:
        """True once the worker closed the stream and the buffer is drained."""

        with self._condition:
            return self._closed and not self._items

    @property
    def buffered(self) -> int:
        with self._condition:
            return len(self._items)

    # -------------------------------------------------------------- publish

    def _publish(self, event: WorkflowEvent) -> None:
        if event.event_type == TERMINAL_EVENT_TYPE:
            self._put_terminal(event)
            return
        with self._condition:
            if self._closed or self._terminal is not None:
                # The protocol forbids anything after the terminal event; the
                # emitter already drops it, and this is the belt-and-braces path
                # for a publisher that bypasses the emitter.
                return
            if len(self._items) >= self.queue_maxsize:
                # Progress-only events may be dropped under backpressure.
                self.dropped_progress += 1
                return
            self._items.append(event)
            self._condition.notify()

    def _put_terminal(self, event: WorkflowEvent) -> None:
        """Buffer the terminal event, evicting progress events if needed."""

        with self._condition:
            if self._terminal is not None:
                LOGGER.warning(
                    "stream_protocol_violation run_id=%s duplicate_terminal=true",
                    event.run_id,
                )
                return
            if event.outcome is None or event.outcome not in OUTCOMES:
                event.outcome = resolve_outcome(event.status, event.error)
            while len(self._items) >= self.queue_maxsize:
                # Never evict the terminal event itself: only buffered progress.
                if not self._items or self._items[0] is self._terminal:
                    break
                self._items.popleft()
                self.dropped_progress += 1
            self._items.append(event)
            self._terminal = event
            self._condition.notify_all()

    def _close(self) -> None:
        """Close the stream; the sentinel is the closed flag and cannot be evicted."""

        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ----------------------------------------------------------- iteration

    def __iter__(self) -> "WorkflowEventStream":
        return self

    def __next__(self) -> WorkflowEvent:
        event = self._next_event(None)
        if event is None:
            raise StopIteration
        return event

    def next_event(self, timeout: float | None = None) -> WorkflowEvent | None:
        """Return the next event, or ``None`` on timeout / end of stream.

        Transports that must stay responsive (for example the async SSE route
        polling ``is_disconnected``) use a finite timeout and then check
        :attr:`finished`, instead of blocking forever.
        """

        return self._next_event(timeout)

    def _next_event(self, timeout: float | None) -> WorkflowEvent | None:
        with self._condition:
            if not self._items:
                if self._closed:
                    self._finish_locked()
                    return None
                if not self._condition.wait(timeout):
                    return None
                if not self._items:
                    if self._closed:
                        self._finish_locked()
                    return None
            event = self._items.popleft()
            if event.event_type == TERMINAL_EVENT_TYPE:
                self._terminal_consumed = True
            return event

    def _finish_locked(self) -> None:
        """Record the end of iteration and flag a missing terminal event."""

        if self._closed and self._terminal is None:
            self._protocol_violation = True
            LOGGER.warning(
                "stream_protocol_violation run_id=%s missing_terminal_event=true",
                self.run_id,
            )

    def snapshot(self) -> dict[str, Any]:
        """Protocol-level status of this stream, for transports and tests."""

        return {
            "protocol_version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "task_id": self._terminal.task_id if self._terminal else None,
            "cancelled": self.is_cancelled(),
            "closed": self._closed,
            "buffered": len(self._items),
            "dropped_progress": self.dropped_progress,
            "terminal_event_type": (
                self._terminal.event_type if self._terminal else None
            ),
            "outcome": self.outcome,
            "protocol_violation": self._protocol_violation,
            "cancelled_after_completion": self.cancelled_after_completion,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
