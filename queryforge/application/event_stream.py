"""Blocking iterator that carries worker-thread workflow events."""

from __future__ import annotations

from queue import Queue
from typing import Iterator

from queryforge.workflow.event_emitter import WorkflowEvent


class WorkflowEventStream(Iterator[WorkflowEvent]):
    def __init__(self, emitter) -> None:
        self.emitter = emitter
        self.result: dict | None = None
        self.error: Exception | None = None
        self._queue: Queue[WorkflowEvent | None] = Queue()

    def _publish(self, event: WorkflowEvent) -> None:
        self._queue.put(event)

    def _close(self) -> None:
        self._queue.put(None)

    def __iter__(self) -> "WorkflowEventStream":
        return self

    def __next__(self) -> WorkflowEvent:
        event = self._queue.get()
        if event is None:
            raise StopIteration
        return event
