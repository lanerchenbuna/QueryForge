"""Blocking iterator that carries worker-thread workflow events."""

from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Event
from typing import Iterator

from queryforge.workflow.event_emitter import WorkflowEvent

TERMINAL_EVENT_TYPE = "final_result"


class WorkflowEventStream(Iterator[WorkflowEvent]):
    """Consume progress events plus one terminal result/error event.

    Progress events are bounded: when the queue is full the newest progress
    event is dropped (progress-only information may be lost). The terminal
    ``final_result`` event is never dropped; it evicts the oldest progress
    event to make room when necessary. ``cancel`` cooperatively asks the
    worker to stop at the next node boundary.
    """

    def __init__(self, emitter, queue_maxsize: int = 100) -> None:
        self.emitter = emitter
        self.result: dict | None = None
        self.error: Exception | None = None
        self._queue: Queue[WorkflowEvent | None] = Queue(
            maxsize=max(queue_maxsize, 1)
        )
        self._cancelled = Event()

    def _publish(self, event: WorkflowEvent) -> None:
        if event.event_type == TERMINAL_EVENT_TYPE:
            self._put_terminal(event)
            return
        try:
            self._queue.put_nowait(event)
        except Full:
            # Progress-only events may be dropped under backpressure.
            return

    def _put_terminal(self, event: WorkflowEvent) -> None:
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except Full:
                try:
                    self._queue.get_nowait()
                except Empty:
                    continue

    def _close(self) -> None:
        # Sentinel delivery must never block a finishing worker.
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except Full:
                try:
                    self._queue.get_nowait()
                except Empty:
                    continue

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def __iter__(self) -> "WorkflowEventStream":
        return self

    def __next__(self) -> WorkflowEvent:
        event = self._queue.get()
        if event is None:
            raise StopIteration
        return event
