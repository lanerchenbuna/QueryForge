"""Standard-library logging and lightweight run/model observability."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = PROJECT_ROOT / ".queryforge/logs/queryforge.log"
DEFAULT_TRACE_DIR = PROJECT_ROOT / ".queryforge/traces"
_RUN_ID = ContextVar("queryforge_run_id", default="-")
_NODE_NAME = ContextVar("queryforge_node_name", default="-")
_TASK_ID = ContextVar("queryforge_task_id", default=None)
_SPAN_PATH = ContextVar("queryforge_span_path", default=())

#: Span kinds required by step 14: model, tool, sql, retrieval, step.
SPAN_KINDS = ("model", "tool", "sql", "retrieval", "step")

#: Terminal span statuses. ``cancelled`` is its own status so a cancelled run is
#: never reported as a plain failure.
SPAN_STATUSES = ("success", "failed", "cancelled")

#: Span attributes whose *values* are never recorded (names are enough).
_SENSITIVE_ATTRIBUTE_KEYS = (
    "prompt",
    "prompts",
    "messages",
    "response",
    "responses",
    "content",
    "sql",
    "sql_text",
    "statement",
    "query",
    "rows",
    "row",
    "row_values",
    "result",
    "results",
    "answer",
    "question",
    "secret",
    "password",
    "credential",
    "credentials",
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "dsn",
    "headers",
)

#: Suffixes that turn a sensitive-looking key into a safe aggregate: spans keep
#: sizes, counts, and digests, never the payload itself.
_SAFE_ATTRIBUTE_SUFFIXES = (
    "_chars",
    "_count",
    "_length",
    "_len",
    "_size",
    "_bytes",
    "_digest",
    "_hash",
    "_sha256",
    "_tokens",
    "_status",
    "_type",
    "_id",
    "_key",
    "_source",
    "_ms",
)

#: Maximum length of a recorded attribute string. Longer values are truncated;
#: spans carry sizes and identifiers, not payloads.
_ATTRIBUTE_MAX_CHARS = 160


def _is_sensitive_attribute(name: str) -> bool:
    """True when an attribute *key* names payload-bearing content."""

    lowered = name.lower()
    if lowered in _SENSITIVE_ATTRIBUTE_KEYS:
        return True
    if not any(marker in lowered for marker in _SENSITIVE_ATTRIBUTE_KEYS):
        return False
    # ``sql_chars``, ``prompt_tokens``, ``statement_digest`` are aggregates, not
    # payloads; only the bare/unsuffixed key is sensitive.
    return not lowered.endswith(_SAFE_ATTRIBUTE_SUFFIXES)

#: Bounded registry so a long-lived process cannot accumulate run recorders.
MAX_TRACKED_RUNS = 32


def new_run_id() -> str:
    return f"qf_{uuid.uuid4().hex}"


def current_run_id() -> str:
    return _RUN_ID.get()


def current_node_name() -> str:
    return _NODE_NAME.get()


def current_task_id() -> str | None:
    return _TASK_ID.get()


class _RunActivity:
    """Process-wide registry entry for one active run.

    Threads spawned by ``ThreadPoolExecutor`` do not inherit ``contextvars``.
    ``run_logging_context`` therefore also registers the active run (and its
    node stack) here, so a worker thread that cannot see the context vars can
    still be attributed to the right run/step instead of silently losing it.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.task_id: str | None = None
        self.node_stack: list[str] = []
        self.lock = threading.RLock()


_ACTIVITY_LOCK = threading.RLock()
_ACTIVE_RUNS: "OrderedDict[str, _RunActivity]" = OrderedDict()


def _activity_for(run_id: str | None) -> _RunActivity | None:
    if not run_id or run_id == "-":
        return None
    with _ACTIVITY_LOCK:
        return _ACTIVE_RUNS.get(run_id)


def _register_activity(run_id: str) -> _RunActivity:
    with _ACTIVITY_LOCK:
        activity = _ACTIVE_RUNS.get(run_id)
        if activity is None:
            activity = _RunActivity(run_id)
            _ACTIVE_RUNS[run_id] = activity
        _ACTIVE_RUNS.move_to_end(run_id)
        while len(_ACTIVE_RUNS) > MAX_TRACKED_RUNS:
            _ACTIVE_RUNS.popitem(last=False)
        return activity


def _single_active_activity() -> _RunActivity | None:
    with _ACTIVITY_LOCK:
        if len(_ACTIVE_RUNS) == 1:
            return next(iter(_ACTIVE_RUNS.values()))
        return None


@contextmanager
def run_logging_context(run_id: str):
    token = _RUN_ID.set(run_id)
    _register_activity(run_id)
    try:
        yield
    finally:
        _RUN_ID.reset(token)


@contextmanager
def node_logging_context(node_name: str):
    token = _NODE_NAME.set(node_name)
    run_id = _RUN_ID.get()
    activity = _activity_for(run_id)
    if activity is not None:
        with activity.lock:
            activity.node_stack.append(node_name)
    try:
        yield
    finally:
        if activity is not None:
            with activity.lock:
                if activity.node_stack and activity.node_stack[-1] == node_name:
                    activity.node_stack.pop()
                elif node_name in activity.node_stack:
                    activity.node_stack.remove(node_name)
        _NODE_NAME.reset(token)


@dataclass(frozen=True)
class RunContext:
    """Immutable snapshot of run/node/task identity.

    Worker threads (parallel candidates, tool loops) can be handed this snapshot
    explicitly instead of relying on implicit ``contextvars`` inheritance.
    """

    run_id: str
    node_name: str = "-"
    task_id: str | None = None
    span_path: tuple[str, ...] = ()
    source: str = "contextvar"

    @property
    def attributed(self) -> bool:
        return self.run_id not in {"", "-"}


def capture_run_context() -> RunContext:
    """Capture the caller's context for explicit propagation to a worker thread."""

    return RunContext(
        run_id=_RUN_ID.get(),
        node_name=_NODE_NAME.get(),
        task_id=_TASK_ID.get(),
        span_path=tuple(_SPAN_PATH.get()),
    )


def current_run_context(fallback: RunContext | None = None) -> RunContext:
    """Resolve the effective run context, with an explicit thread fallback.

    Resolution order:

    1. the ``contextvars`` of the calling thread (correct for the thread that
       started the run and for every explicitly propagated worker);
    2. the snapshot a component captured when it was constructed in the run
       thread (used by :class:`ObservedModelProvider`);
    3. the single active run registered process-wide, which covers worker
       threads created without propagation while exactly one run is in flight.

    When several runs are active and no context is visible, the context stays
    unattributed rather than guessing a run.
    """

    run_id = _RUN_ID.get()
    if run_id not in {"", "-"}:
        activity = _activity_for(run_id)
        node_name = _NODE_NAME.get()
        if node_name in {"", "-"} and activity is not None:
            with activity.lock:
                node_name = activity.node_stack[-1] if activity.node_stack else "-"
        return RunContext(
            run_id=run_id,
            node_name=node_name,
            task_id=_TASK_ID.get() or (activity.task_id if activity else None),
            span_path=tuple(_SPAN_PATH.get()),
            source="contextvar",
        )
    if fallback is not None and fallback.attributed:
        activity = _activity_for(fallback.run_id)
        node_name = fallback.node_name
        if (node_name in {"", "-"}) and activity is not None:
            with activity.lock:
                node_name = activity.node_stack[-1] if activity.node_stack else "-"
        return RunContext(
            run_id=fallback.run_id,
            node_name=node_name,
            task_id=fallback.task_id or (activity.task_id if activity else None),
            span_path=fallback.span_path,
            source="run_context",
        )
    activity = _single_active_activity()
    if activity is not None:
        with activity.lock:
            node_name = activity.node_stack[-1] if activity.node_stack else "-"
        return RunContext(
            run_id=activity.run_id,
            node_name=node_name,
            task_id=activity.task_id,
            source="inherited",
        )
    return RunContext(run_id="-", node_name=_NODE_NAME.get(), source="unattributed")


@contextmanager
def use_run_context(context: RunContext):
    """Apply a captured context inside a worker thread, then restore it."""

    run_token = _RUN_ID.set(context.run_id)
    node_token = _NODE_NAME.set(context.node_name)
    task_token = _TASK_ID.set(context.task_id)
    span_token = _SPAN_PATH.set(context.span_path)
    try:
        yield
    finally:
        _SPAN_PATH.reset(span_token)
        _TASK_ID.reset(task_token)
        _NODE_NAME.reset(node_token)
        _RUN_ID.reset(run_token)


def propagate_run_context(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``func`` so a worker thread inherits the submitting thread's context.

    Capture the snapshot when the wrapper is *called* (that is, on the submitting
    thread) and re-apply it inside the worker. The snapshot is plain data, so it
    is safe to reuse across concurrent workers (unlike ``copy_context()``).
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        context = capture_run_context()
        with use_run_context(context):
            return func(*args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "propagated")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    return wrapper


def redact_text(value: str) -> str:
    """Redact credential-looking substrings using the logging filter patterns."""

    for pattern in SafeContextFilter._PATTERNS:
        if pattern.groups:
            value = pattern.sub(r"\1[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value


def stable_digest(value: str) -> str:
    """Return a short, non-reversible digest for correlating opaque payloads."""

    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Strip payload-bearing values so spans keep sizes, counts, and identities.

    Sensitive keys (prompt, SQL, rows, secrets, ...) are dropped entirely; every
    other value is redacted and truncated. Callers that need the real payload
    must use the explicit ``debug_prompts`` trace path instead.
    """

    if not attributes:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        name = str(key)
        if _is_sensitive_attribute(name):
            continue
        if isinstance(value, str):
            text = redact_text(value)
            if len(text) > _ATTRIBUTE_MAX_CHARS:
                text = text[:_ATTRIBUTE_MAX_CHARS] + "…"
            sanitized[name] = text
        elif isinstance(value, (int, float, bool)) or value is None:
            sanitized[name] = value
        else:
            sanitized[name] = redact_text(str(value))[:_ATTRIBUTE_MAX_CHARS]
    return sanitized


@dataclass(frozen=True)
class ModelUsage:
    """Normalized token usage for one model call.

    ``estimated`` marks values that were derived from character counts because
    the provider reported nothing; an estimated value is never presented as a
    measured one, and token counts are never faked as zero.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: bool = False
    raw: dict[str, Any] | None = None

    #: Deterministic char-per-token ratio used only for estimates.
    CHARS_PER_TOKEN = 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "raw": self.raw,
        }

    @classmethod
    def estimate(cls, prompt_chars: int, response_chars: int) -> "ModelUsage":
        """Deterministic char-based estimate; never zero when text was sent."""

        prompt_tokens = max(1, math.ceil(max(prompt_chars, 0) / cls.CHARS_PER_TOKEN))
        completion_tokens = max(
            1, math.ceil(max(response_chars, 0) / cls.CHARS_PER_TOKEN)
        )
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated=True,
            raw=None,
        )


def normalize_usage(raw: Any) -> ModelUsage | None:
    """Normalize a provider usage payload into :class:`ModelUsage`.

    Accepts an existing :class:`ModelUsage`, a mapping, or an object with the
    usual OpenAI-style / Anthropic-style / Gemini-style attribute names. Returns
    ``None`` when the payload carries no token counts at all, so the caller can
    mark the call estimated instead of inventing a measured zero.
    """

    if raw is None:
        return None
    if isinstance(raw, ModelUsage):
        return raw

    def _read(*names: str) -> int | None:
        for name in names:
            if isinstance(raw, dict):
                value = raw.get(name)
            else:
                value = getattr(raw, name, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    prompt = _read("prompt_tokens", "input_tokens", "promptTokenCount")
    completion = _read(
        "completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount"
    )
    total = _read("total_tokens", "totalTokenCount")
    if prompt is None and completion is None and total is None:
        return None
    prompt = prompt or 0
    completion = completion or 0
    if total is None:
        total = prompt + completion
    payload: dict[str, Any]
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        payload = {
            key: value
            for key, value in (
                ("prompt_tokens", prompt),
                ("completion_tokens", completion),
                ("total_tokens", total),
            )
        }
    return ModelUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        estimated=False,
        raw=payload,
    )


@dataclass
class Span:
    """One observed unit of work: a model call, tool call, SQL, retrieval, step."""

    name: str
    kind: str
    run_id: str
    started_at: str
    duration_ms: float
    status: str = "success"
    task_id: str | None = None
    node_name: str | None = None
    parent: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    usage: ModelUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_name": self.node_name,
            "parent": self.parent,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": dict(self.attributes),
            "usage": self.usage.to_dict() if self.usage else None,
        }


class SpanRecorder:
    """Thread-safe span collector and per-run usage/latency aggregator."""

    def __init__(self, run_id: str, task_id: str | None = None) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self._spans: list[Span] = []
        self._started: dict[int, float] = {}
        #: Identities of spans already recorded, so ``end`` is idempotent.
        self._recorded: set[int] = set()
        self._lock = threading.RLock()
        self._closed = False

    # ----------------------------------------------------------------- record

    def set_task_id(self, task_id: str | None) -> None:
        """Attach the persisted task identity, back-filling earlier spans."""

        if not task_id:
            return
        with self._lock:
            self.task_id = task_id
            for span in self._spans:
                if not span.task_id:
                    span.task_id = task_id
        activity = _activity_for(self.run_id)
        if activity is not None:
            with activity.lock:
                activity.task_id = task_id

    def add(self, span: Span) -> Span:
        with self._lock:
            self._spans.append(span)
        return span

    @contextmanager
    def span(
        self,
        name: str,
        kind: str,
        *,
        attributes: dict[str, Any] | None = None,
        status: str = "success",
    ) -> Iterator[Span]:
        """Time one unit of work and record it even when it raises."""

        span = self.begin(name, kind, attributes=attributes, status=status)
        try:
            yield span
        except BaseException:
            if span.status == "success":
                span.status = "failed"
            raise
        finally:
            self.end(span)

    def begin(
        self,
        name: str,
        kind: str,
        *,
        attributes: dict[str, Any] | None = None,
        status: str = "success",
    ) -> Span:
        """Start a span; call :meth:`end` in a ``finally`` block."""

        if kind not in SPAN_KINDS:
            raise ValueError(f"unknown span kind: {kind!r}")
        context = current_run_context()
        if context.attributed and context.run_id != self.run_id:
            # A span recorded here belongs to *this* recorder's run: never let a
            # neighbouring run's context mis-attribute it.
            activity = _activity_for(self.run_id)
            node_name = context.node_name
            if activity is not None:
                with activity.lock:
                    node_name = (
                        activity.node_stack[-1] if activity.node_stack else node_name
                    )
            context = RunContext(
                run_id=self.run_id,
                node_name=node_name,
                task_id=self.task_id or context.task_id,
                span_path=context.span_path,
                source="recorder",
            )
        parent_path = _SPAN_PATH.get()
        span = Span(
            name=name,
            kind=kind,
            run_id=context.run_id if context.attributed else self.run_id,
            task_id=context.task_id or self.task_id,
            node_name=context.node_name,
            parent=parent_path[-1] if parent_path else None,
            started_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=0.0,
            status=status,
            attributes=sanitize_attributes(attributes),
        )
        with self._lock:
            self._started[id(span)] = time.perf_counter()
        return span

    def end(self, span: Span, *, status: str | None = None) -> Span:
        """Finish a span started by :meth:`begin` and add it to the run.

        Idempotent: a span is recorded exactly once. An explicit ``end`` and the
        ``finally`` of :meth:`span` can both run for the same span, and recording
        it twice would double-count its duration and its tokens in the run
        summary. Identities are safe to track here because a recorded span is
        retained in ``_spans``, so its ``id`` cannot be reused meanwhile.
        """

        with self._lock:
            if id(span) in self._recorded:
                if status is not None:
                    span.status = status
                return span
            started = self._started.pop(id(span), None)
            self._recorded.add(id(span))
        if started is not None:
            span.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        if status is not None:
            span.status = status
        self.add(span)
        return span

    # ------------------------------------------------------------------ read

    @property
    def spans(self) -> tuple[Span, ...]:
        with self._lock:
            return tuple(self._spans)

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def usage_summary(self) -> dict[str, Any]:
        """Aggregate token usage across every model span of the run."""

        with self._lock:
            spans = list(self._spans)
        model_spans = [span for span in spans if span.kind == "model"]
        by_model: dict[str, dict[str, Any]] = {}
        total_prompt = 0
        total_completion = 0
        total_tokens = 0
        estimated = False
        measured_calls = 0
        for span in model_spans:
            usage = span.usage
            key = str(
                span.attributes.get("model_key")
                or f"{span.attributes.get('provider')}/{span.attributes.get('model')}"
            )
            entry = by_model.setdefault(
                key,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "estimated_calls": 0,
                },
            )
            entry["calls"] += 1
            if usage is None:
                estimated = True
                entry["estimated_calls"] += 1
                continue
            entry["prompt_tokens"] += usage.prompt_tokens
            entry["completion_tokens"] += usage.completion_tokens
            entry["total_tokens"] += usage.total_tokens
            total_prompt += usage.prompt_tokens
            total_completion += usage.completion_tokens
            total_tokens += usage.total_tokens
            if usage.estimated:
                estimated = True
                entry["estimated_calls"] += 1
            else:
                measured_calls += 1
        cost = _estimate_cost(by_model)
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "model_calls": len(model_spans),
            "measured_calls": measured_calls,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "estimated": estimated,
            "estimated_cost_usd": cost,
            "price_table_configured": bool(_PRICE_TABLE),
            "by_model": by_model,
        }

    def latency_summary(self, *, end_to_end_ms: float | None = None) -> dict[str, Any]:
        """Per-kind latency breakdown plus the run's end-to-end duration."""

        with self._lock:
            spans = list(self._spans)
        by_kind: dict[str, dict[str, Any]] = {
            kind: {"count": 0, "duration_ms": 0.0, "max_duration_ms": 0.0}
            for kind in SPAN_KINDS
        }
        for span in spans:
            entry = by_kind.setdefault(
                span.kind, {"count": 0, "duration_ms": 0.0, "max_duration_ms": 0.0}
            )
            entry["count"] += 1
            entry["duration_ms"] = round(entry["duration_ms"] + span.duration_ms, 3)
            entry["max_duration_ms"] = max(entry["max_duration_ms"], span.duration_ms)
        if end_to_end_ms is None:
            end_to_end_ms = self._span_window_ms(spans)
        return {
            "end_to_end_ms": end_to_end_ms,
            "by_kind": by_kind,
            "span_count": len(spans),
        }

    @staticmethod
    def _span_window_ms(spans: list[Span]) -> float:
        """Wall-clock window covered by the recorded spans (approximation)."""

        if not spans:
            return 0.0
        try:
            starts = [datetime.fromisoformat(span.started_at) for span in spans]
        except ValueError:  # pragma: no cover - defensive
            return 0.0
        ends = [
            start + timedelta(milliseconds=span.duration_ms)
            for start, span in zip(starts, spans)
        ]
        return round((max(ends) - min(starts)).total_seconds() * 1000, 3)

    def summary(self, *, end_to_end_ms: float | None = None) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "usage": self.usage_summary(),
            "latency": self.latency_summary(end_to_end_ms=end_to_end_ms),
        }

    def to_dict(self, *, end_to_end_ms: float | None = None) -> dict[str, Any]:
        payload = self.summary(end_to_end_ms=end_to_end_ms)
        payload["spans"] = [span.to_dict() for span in self.spans]
        return payload


#: Optional price table, configured by deployments/evaluation, never by config
#: loading here: {"provider/model": {"prompt_per_1k": float,
#: "completion_per_1k": float}}. Empty means "cost is unknown", not zero.
_PRICE_TABLE: dict[str, dict[str, float]] = {}


def configure_price_table(table: dict[str, dict[str, float]] | None) -> None:
    """Install (or clear) the optional per-1K token price table."""

    _PRICE_TABLE.clear()
    for key, prices in (table or {}).items():
        _PRICE_TABLE[str(key)] = {
            "prompt_per_1k": float(prices.get("prompt_per_1k", 0.0)),
            "completion_per_1k": float(prices.get("completion_per_1k", 0.0)),
        }


def _estimate_cost(by_model: dict[str, dict[str, Any]]) -> float | None:
    if not _PRICE_TABLE:
        return None
    total = 0.0
    priced = False
    for key, entry in by_model.items():
        prices = _PRICE_TABLE.get(key)
        if prices is None:
            continue
        priced = True
        total += entry["prompt_tokens"] / 1000.0 * prices["prompt_per_1k"]
        total += entry["completion_tokens"] / 1000.0 * prices["completion_per_1k"]
    return round(total, 6) if priced else None


_RECORDER_LOCK = threading.RLock()
_RECORDERS: "OrderedDict[str, SpanRecorder]" = OrderedDict()
#: Overflow is reported once per episode: the soft cap is crossed deliberately
#: rather than by dropping a live recorder, and repeating the warning for every
#: further run would only flood the log.
_RECORDER_OVERFLOW_WARNED = False
_RECORDER_LOGGER = logging.getLogger("queryforge.observability")


def start_span_recorder(run_id: str, task_id: str | None = None) -> SpanRecorder:
    """Create (or reuse) the span recorder of one run."""

    global _RECORDER_OVERFLOW_WARNED
    with _RECORDER_LOCK:
        recorder = _RECORDERS.get(run_id)
        if recorder is None or recorder.closed:
            recorder = SpanRecorder(run_id, task_id)
            _RECORDERS[run_id] = recorder
        elif task_id:
            recorder.set_task_id(task_id)
        _RECORDERS.move_to_end(run_id)
        while len(_RECORDERS) > MAX_TRACKED_RUNS:
            # Evict a finished run first: an in-flight run's recorder must not be
            # dropped while it is still collecting spans. Evicting a live one
            # made ``get_span_recorder`` return None mid-run, so the terminal
            # event lost its usage/latency summary entirely.
            oldest_closed = next(
                (key for key, value in _RECORDERS.items() if value.closed and key != run_id),
                None,
            )
            if oldest_closed is None:
                # Every tracked run is still open: the cap is a soft bound and
                # the registry grows instead of discarding live evidence.
                if not _RECORDER_OVERFLOW_WARNED:
                    _RECORDER_OVERFLOW_WARNED = True
                    _RECORDER_LOGGER.warning(
                        "span_recorder_registry_over_cap tracked=%s cap=%s "
                        "reason=every_tracked_run_is_open",
                        len(_RECORDERS),
                        MAX_TRACKED_RUNS,
                    )
                break
            _RECORDERS.pop(oldest_closed, None)
        if len(_RECORDERS) <= MAX_TRACKED_RUNS:
            _RECORDER_OVERFLOW_WARNED = False
        return recorder


def get_span_recorder(run_id: str | None = None) -> SpanRecorder | None:
    """Return the recorder of ``run_id`` (default: the current run context)."""

    resolved = run_id or current_run_context().run_id
    if not resolved or resolved == "-":
        return None
    with _RECORDER_LOCK:
        return _RECORDERS.get(resolved)


def discard_span_recorder(run_id: str) -> None:
    with _RECORDER_LOCK:
        _RECORDERS.pop(run_id, None)


def current_span_recorder(run_id: str | None = None) -> SpanRecorder | None:
    """Return the recorder of the given run (default: the current run context)."""

    return get_span_recorder(run_id)


class SafeContextFilter(logging.Filter):
    """Attach context and redact common credential shapes before formatting."""

    _PATTERNS = (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
        re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or current_run_id()
        record.node_name = getattr(record, "node_name", None) or current_node_name()
        message = record.getMessage()
        for pattern in self._PATTERNS:
            if pattern.groups:
                message = pattern.sub(r"\1[REDACTED]", message)
            else:
                message = pattern.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(
    level: str | None = None,
    *,
    log_path: str | Path | None = None,
    console: bool = True,
) -> Path:
    """Configure QueryForge loggers without touching third-party root logging."""

    load_dotenv()
    level_name = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    numeric_level = logging.getLevelNamesMapping().get(level_name)
    if not isinstance(numeric_level, int):
        supported = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
        raise ValueError(f"Invalid LOG_LEVEL {level_name!r}. Supported: {supported}")

    path = Path(log_path or os.getenv("LOG_FILE") or DEFAULT_LOG_PATH).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()

    logger = logging.getLogger("queryforge")
    logger.setLevel(numeric_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_queryforge_handler", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s run_id=%(run_id)s node=%(node_name)s "
        "logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    safe_filter = SafeContextFilter()

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(safe_filter)
        console_handler._queryforge_handler = True  # type: ignore[attr-defined]
        logger.addHandler(console_handler)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(safe_filter)
        file_handler._queryforge_handler = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("file_logging_unavailable path=%s error=%s", path, exc)
    return path


def ensure_logging_configured() -> Path:
    logger = logging.getLogger("queryforge")
    if not any(getattr(handler, "_queryforge_handler", False) for handler in logger.handlers):
        return configure_logging()
    configured = next(
        (
            Path(handler.baseFilename)
            for handler in logger.handlers
            if getattr(handler, "_queryforge_handler", False)
            and isinstance(handler, logging.FileHandler)
        ),
        DEFAULT_LOG_PATH,
    )
    return configured


#: Attribute under which an isolated provider keeps its per-thread usage store.
_USAGE_THREAD_STORE = "_queryforge_usage_thread_store"
_USAGE_ISOLATION_LOCK = threading.RLock()
_USAGE_ISOLATION_SUBCLASSES: dict[type, type] = {}


def _thread_keyed_usage_property() -> property:
    """``last_usage`` descriptor whose value belongs to the calling thread.

    Why this exists: providers report usage by assigning ``self.last_usage`` on
    the adapter, and one adapter instance is shared by every concurrent model
    call. With a single slot, the parallel-candidate path made two overlapping
    calls swap values, so a call could report a *neighbouring* call's tokens as
    measured. Keying the slot by thread keeps each invocation's own value
    readable by the invocation that produced it, without serialising calls.
    """

    def _get(instance: Any) -> ModelUsage | None:
        store = instance.__dict__.get(_USAGE_THREAD_STORE)
        return getattr(store, "value", None) if store is not None else None

    def _set(instance: Any, value: ModelUsage | None) -> None:
        store = instance.__dict__.get(_USAGE_THREAD_STORE)
        if store is None:
            # One store per provider instance, created here and never replaced:
            # a lazy per-thread creation would race and split the threads over
            # different stores.
            store = threading.local()
            instance.__dict__[_USAGE_THREAD_STORE] = store
        store.value = value

    return property(_get, _set)


def _isolate_usage_slot(provider: Any) -> bool:
    """Give ``provider`` a per-thread ``last_usage`` slot when that is possible.

    Returns ``True`` when the slot is isolated. Providers that cannot be
    re-classed (non-Python objects, ``__slots__`` layouts) keep the shared slot
    and are handled conservatively by the caller.
    """

    cls = type(provider)
    if cls.__dict__.get("_queryforge_usage_isolated"):
        # Already isolated (the same adapter observed twice): only make sure the
        # instance has its store.
        provider.__dict__.setdefault(_USAGE_THREAD_STORE, threading.local())
        return True
    with _USAGE_ISOLATION_LOCK:
        subclass = _USAGE_ISOLATION_SUBCLASSES.get(cls)
        if subclass is None:
            try:
                subclass = type(
                    cls.__name__,
                    (cls,),
                    {
                        "last_usage": _thread_keyed_usage_property(),
                        "_queryforge_usage_isolated": True,
                    },
                )
            except TypeError:  # pragma: no cover - exotic adapter class
                return False
            _USAGE_ISOLATION_SUBCLASSES[cls] = subclass
    try:
        provider.__class__ = subclass
    except (TypeError, AttributeError):
        # Layout-incompatible instance (e.g. ``__slots__``): the shared slot
        # stays, and ``ObservedModelProvider`` then only trusts it when no other
        # call is in flight.
        return False
    provider.__dict__[_USAGE_THREAD_STORE] = threading.local()
    return True


class ObservedModelProvider:
    """Duck-typed provider decorator that records summaries, never prompts by default.

    Every call becomes one ``model`` :class:`Span` carrying normalized
    :class:`ModelUsage`. Usage reported by the adapter is recorded as measured;
    an adapter that reports nothing yields ``estimated=True`` with a
    deterministic character-based estimate. Prompt text is never part of a span
    or a log line: only sizes. The explicit ``debug_prompts`` trace remains the
    one opt-in path that stores prompts (redacted), for controlled debugging.

    Usage is read from the adapter's ``last_usage`` slot through a per-call
    view: overlapping calls (parallel candidates) each read only their own
    reported usage, and a provider that reports nothing is estimated rather than
    credited with another call's tokens.
    """

    def __init__(
        self,
        provider: Any,
        *,
        provider_name: str,
        model_name: str,
        debug_prompts: bool = False,
        trace_dir: str | Path | None = None,
    ) -> None:
        self._provider = provider
        self.provider = provider_name
        self.model = model_name
        self.debug_prompts = debug_prompts
        path = Path(trace_dir or DEFAULT_TRACE_DIR).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.trace_dir = path.resolve()
        self._counter = 0
        self._lock = threading.Lock()
        self._logger = logging.getLogger("queryforge.model")
        # Threads spawned without contextvars (parallel candidates, tool loop)
        # still resolve to this run through the construction-time snapshot.
        self._bound_context = capture_run_context()
        # One usage slot per call: the adapter's own slot is keyed by thread when
        # its class allows it, otherwise overlapping calls must not trust it.
        self._usage_slot_isolated = (
            _isolate_usage_slot(provider) if hasattr(provider, "last_usage") else False
        )
        self._inflight = 0
        self._serial = 0
        self._inflight_lock = threading.Lock()

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return self._observe(
            "generate_json", prompt, lambda: self._provider.generate_json(prompt)
        )

    def generate_text(self, prompt: str) -> str:
        return self._observe(
            "generate_text", prompt, lambda: self._provider.generate_text(prompt)
        )

    def generate_with_messages(
        self, messages: list[dict[str, str]], json_mode: bool = False
    ) -> str:
        prompt = json.dumps(messages, ensure_ascii=False)
        return self._observe(
            "generate_with_messages",
            prompt,
            lambda: self._provider.generate_with_messages(messages, json_mode=json_mode),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def _observe(
        self, method: str, prompt: str, operation: Callable[[], Any]
    ) -> Any:
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        response: Any = None
        error: Exception | None = None
        # Clear any usage from a previous call: a provider that reports nothing
        # must be estimated, never credited with the last call's measured tokens.
        # With an isolated slot this clears only this call's own slot.
        try:
            if hasattr(self._provider, "last_usage"):
                self._provider.last_usage = None
        except (AttributeError, TypeError):  # pragma: no cover - defensive
            pass
        call_serial = self._begin_call()
        try:
            response = operation()
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            # The adapter's usage slot is only trustworthy for this call when no
            # other call of the same provider overlapped it (or when the slot is
            # keyed per thread, which is the normal case).
            exclusive = self._end_call(call_serial)
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            response_text = self._response_text(response)
            usage = self._resolve_usage(
                prompt,
                response_text,
                error,
                shared_slot_trusted=exclusive or self._usage_slot_isolated,
            )
            context = current_run_context(fallback=self._bound_context)
            recorder = get_span_recorder(
                context.run_id if context.attributed else self._bound_context.run_id
            )
            if recorder is not None:
                self._record_span(
                    recorder,
                    context=context,
                    method=method,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    usage=usage,
                    prompt_chars=len(prompt),
                    response_chars=len(response_text),
                    status="success" if error is None else "failed",
                )
            fields = (
                f"model_call method={method} provider={self.provider} model={self.model} "
                f"prompt_chars={len(prompt)} response_chars={len(response_text)} "
                f"duration_ms={duration_ms} "
                f"prompt_tokens={usage.prompt_tokens} "
                f"completion_tokens={usage.completion_tokens} "
                f"total_tokens={usage.total_tokens} usage_estimated={usage.estimated} "
                f"success={error is None}"
            )
            if error is None:
                self._logger.info(fields)
            else:
                self._logger.error("%s error=%s", fields, error)
            if self.debug_prompts:
                self._write_trace(
                    method, prompt, response_text, duration_ms, error, usage
                )

    def _begin_call(self) -> int:
        """Register one in-flight call and return its serial number."""

        with self._inflight_lock:
            self._inflight += 1
            self._serial += 1
            return self._serial

    def _end_call(self, serial: int) -> bool:
        """Deregister a call; report whether it overlapped no other call."""

        with self._inflight_lock:
            exclusive = self._inflight == 1 and self._serial == serial
            self._inflight -= 1
            return exclusive

    def _resolve_usage(
        self,
        prompt: str,
        response: str,
        error: BaseException | None,
        *,
        shared_slot_trusted: bool,
    ) -> ModelUsage:
        """Prefer measured provider usage; otherwise estimate, never fake zero.

        ``shared_slot_trusted`` is ``False`` only for a provider whose usage slot
        could not be keyed per call *and* which ran concurrently with another
        call of the same provider: that slot may already hold a neighbour's
        tokens, and reporting those as this call's measured usage would corrupt
        the run summary. Such a call is estimated instead.
        """

        reported = (
            getattr(self._provider, "last_usage", None)
            if shared_slot_trusted
            else None
        )
        normalized = normalize_usage(reported)
        if normalized is not None:
            return normalized
        return ModelUsage.estimate(len(prompt), len(response))

    def _record_span(
        self,
        recorder: SpanRecorder,
        *,
        context: RunContext,
        method: str,
        started_at: str,
        duration_ms: float,
        usage: ModelUsage,
        prompt_chars: int,
        response_chars: int,
        status: str,
    ) -> None:
        attributes = sanitize_attributes(
            {
                "provider": self.provider,
                "model": self.model,
                "model_key": f"{self.provider}/{self.model}",
                "method": method,
                "prompt_chars": prompt_chars,
                "response_chars": response_chars,
                "context_source": context.source,
                "usage_source": "estimated" if usage.estimated else "reported",
            }
        )
        recorder.add(
            Span(
                name=(
                    f"{context.node_name}.model"
                    if context.node_name not in {"", "-"}
                    else f"model.{method}"
                ),
                kind="model",
                run_id=context.run_id if context.attributed else recorder.run_id,
                task_id=context.task_id or recorder.task_id,
                node_name=context.node_name,
                started_at=started_at,
                duration_ms=duration_ms,
                status=status,
                attributes=attributes,
                usage=usage,
            )
        )

    def _write_trace(
        self,
        method: str,
        prompt: str,
        response: str,
        duration_ms: float,
        error: BaseException | None,
        usage: ModelUsage | None = None,
    ) -> None:
        try:
            with self._lock:
                self._counter += 1
                counter = self._counter
            context = current_run_context(fallback=self._bound_context)
            run_id = context.run_id
            node_name = self._safe_name(context.node_name)
            run_dir = self.trace_dir / self._safe_name(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = run_dir / f"{counter:03d}_{node_name}_{timestamp}.json"
            payload = {
                "run_id": run_id,
                "task_id": context.task_id,
                "node": context.node_name,
                "method": method,
                "provider": self.provider,
                "model": self.model,
                "prompt_chars": len(prompt),
                "response_chars": len(response),
                "duration_ms": duration_ms,
                "usage": usage.to_dict() if usage else None,
                # Explicit opt-in debug path: prompts are stored, but secrets are
                # still redacted so a debug run cannot leak a credential.
                "prompt": redact_text(prompt),
                "response": redact_text(response),
                "error": str(error) if error else None,
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._logger.info("prompt_trace_written path=%s", path)
        except Exception as exc:
            self._logger.warning("prompt_trace_write_failed error=%s", exc)

    @staticmethod
    def _response_text(response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        try:
            return json.dumps(response, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(response)

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return safe or "unknown"
