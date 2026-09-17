"""Step 14: streaming event protocol, end-to-end cancellation, usage tracing.

Offline and deterministic: fake providers, a temporary SQLite database, and the
real FastAPI app only when the optional server dependencies are installed.

Coverage map (step 14 acceptance cases):

* 14-N1  real FastAPI SSE round trip (skipped, and therefore *unverified*, when
         ``fastapi`` is not installed)
* 14-E1  a normal async connection is not reported as disconnected, with no
         un-awaited coroutine warnings
* 14-C1  cancel before generation / during SQL / after completion
* 14-B1  backpressure with queue capacity 1 and 2 keeps the terminal event
* 14-E2  half-closed client / proxy timeout never sees a success outcome
* 14-I1  spans from parallel candidates + repair + retrieval are attributed to
         the right run/step and reconcile with the usage summary
* 14-M1  a provider without usage is ``estimated=True``, never zero-as-actual
* 14-S1  secrets, prompts, and result rows stay out of spans and logs
* 14-R1  CLI stderr streaming, service/REST, MCP and report paths agree
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import main as cli
from queryforge.application import AgentOptions, AgentService
from queryforge.application.event_stream import WorkflowEventStream
from queryforge.core.config import Config
from queryforge.core.observability import (
    ModelUsage,
    ObservedModelProvider,
    configure_logging,
    configure_price_table,
    discard_span_recorder,
    get_span_recorder,
    normalize_usage,
    run_logging_context,
    start_span_recorder,
)
from queryforge.interfaces.api.app import (
    await_disconnect,
    is_disconnected,
    sse_event_generator,
)
from queryforge.workflow.event_emitter import (
    PROTOCOL_VERSION,
    EventEmitter,
    WorkflowEvent,
    emit_event,
)

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

#: A statement that runs for ~4 seconds uninterrupted, so only a real
#: cancellation (the SQLite progress handler) can stop it quickly.
SLOW_SQL = (
    "SELECT count(*) FROM items a JOIN items b ON a.id <> b.id "
    "JOIN items c ON b.id <> c.id"
)
# Assembled at runtime so this deliberate redaction fixture is not itself a
# repository-hygiene hit (`scripts/check_repository.py` scans for `sk-...`).
SECRET_TOKEN = "sk-" + "live-ABCdef1234567890"
SECRET_ROW_VALUE = "iban-DE89370400440532013000"


class TracingLLM:
    """Deterministic fake provider that reports measured usage for every call."""

    def __init__(self) -> None:
        self.calls = 0
        self.candidate_calls = 0
        self.last_usage = None
        self._lock = threading.Lock()

    def _report_usage(self, prompt: str) -> None:
        with self._lock:
            self.calls += 1
        prompt_tokens = max(1, len(prompt) // 4)
        self.last_usage = ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=3,
            total_tokens=prompt_tokens + 3,
            estimated=False,
            raw={"prompt_tokens": prompt_tokens, "completion_tokens": 3},
        )

    def generate_json(self, prompt: str) -> dict:
        if "Select local QueryForge skills" in prompt:
            self._report_usage(prompt)
            return {"skills": [], "reason": "No optional skill."}
        if "Evaluate whether the SQL and result" in prompt:
            self._report_usage(prompt)
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        if "Repair the SQLite query" in prompt:
            self._report_usage(prompt)
            return {
                "fixed_sql": "SELECT name FROM items ORDER BY name",
                "explanation": "Use the available name column.",
                "tables_used": ["items"],
            }
        with self._lock:
            self.candidate_calls += 1
        self._report_usage(prompt)
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List names.",
            "tables_used": ["items"],
        }


class NoUsageLLM:
    """Provider that consumes tokens but reports no usage at all."""

    def __init__(self) -> None:
        self.last_usage = None

    def generate_json(self, prompt: str) -> dict:
        return {"sql": "SELECT 1", "explanation": "no usage reported"}


class BlockingLLM:
    """Blocks inside its first model call until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.last_usage = None

    def generate_json(self, prompt: str) -> dict:
        self.release.wait(timeout=15)
        return {"skills": [], "reason": "No optional skill."}


def _overlapping_usage_call(provider, prompt: str) -> dict:
    """Report one call's usage into ``provider.last_usage`` (shared fixture)."""

    if prompt == "slow":
        provider.last_usage = ModelUsage(9000, 7, 9007, False, {"call": "slow"})
        provider.entered.set()
        provider.release.wait(timeout=15)
    else:
        provider.last_usage = ModelUsage(3, 2, 5, False, {"call": "fast"})
    return {"ok": True}


class OverlappingUsageLLM:
    """Two concurrent calls whose measured usage must stay per call.

    The first call parks inside the adapter until the second one has finished —
    exactly the shape of the parallel-candidate path, where both calls write the
    adapter's single ``last_usage`` slot before either of them reads it back.
    """

    def __init__(self) -> None:
        self.last_usage = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_json(self, prompt: str) -> dict:
        return _overlapping_usage_call(self, prompt)


class SlottedUsageLLM:
    """Same adapter with a layout that forbids the per-thread usage slot.

    A ``__slots__``-only instance cannot be re-classed for isolation, so this
    provider exercises the conservative attribution path.
    """

    __slots__ = ("last_usage", "entered", "release")

    def __init__(self) -> None:
        self.last_usage = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_json(self, prompt: str) -> dict:
        return _overlapping_usage_call(self, prompt)


class RepairingLLM(TracingLLM):
    """Candidate generation plus one reflection-driven repair cycle."""

    def __init__(self) -> None:
        super().__init__()
        self.reflections = 0

    def generate_json(self, prompt: str) -> dict:
        if "Evaluate whether the SQL and result" in prompt:
            self.reflections += 1
            self._report_usage(prompt)
            if self.reflections == 1:
                return {
                    "success": False,
                    "strategy": "FIX_SQL",
                    "reason": "The selected candidate omits the ordering contract.",
                    "suggested_fix": "Order by name.",
                }
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The repaired query is correct.",
                "suggested_fix": None,
            }
        if "Repair the SQLite query" in prompt:
            self._report_usage(prompt)
            return {
                "fixed_sql": "SELECT name FROM items ORDER BY name",
                "explanation": "Add the ordering contract.",
                "tables_used": ["items"],
            }
        if "Select local QueryForge skills" in prompt:
            self._report_usage(prompt)
            return {"skills": [], "reason": "No optional skill."}
        with self._lock:
            self.candidate_calls += 1
            index = self.candidate_calls
        self._report_usage(prompt)
        sql = (
            "SELECT name FROM items"
            if index % 2
            else "SELECT id, name FROM items"
        )
        return {
            "sql": sql,
            "explanation": f"Candidate {index}.",
            "tables_used": ["items"],
        }


class SlowSqlLLM(TracingLLM):
    """Produces one statement that only cancellation can stop promptly."""

    def generate_json(self, prompt: str) -> dict:
        if "Select local QueryForge skills" in prompt:
            self._report_usage(prompt)
            return {"skills": [], "reason": "No optional skill."}
        if "Evaluate whether the SQL and result" in prompt:
            self._report_usage(prompt)
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "ok",
                "suggested_fix": None,
            }
        self._report_usage(prompt)
        return {"sql": SLOW_SQL, "explanation": "slow", "tables_used": ["items"]}


def usage_total(spans) -> int:
    return sum(span.usage.total_tokens for span in spans if span.usage is not None)


class UsageTracingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [
                (index, SECRET_ROW_VALUE if index == 0 else f"item-{index}")
                for index in range(400)
            ],
        )
        connection.commit()
        connection.close()
        self.state_root = self.root / ".queryforge" / "runs"
        self.log_path = self.root / "logs" / "queryforge.log"
        configure_logging("INFO", log_path=self.log_path, console=False)
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.state_root),
        )
        self._run_counter = 0

    def tearDown(self) -> None:
        configure_price_table(None)
        configure_logging("WARNING", console=False)
        self.directory.cleanup()

    # ------------------------------------------------------------- helpers

    def service(self, llm) -> AgentService:
        return AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: llm,
        )

    def options(self, run_id: str, **overrides) -> AgentOptions:
        base = dict(
            database=str(self.database),
            skills=[],
            run_id=run_id,
            orchestration_state_root=str(self.state_root),
        )
        base.update(overrides)
        return AgentOptions(**base)

    def next_run_id(self, label: str) -> str:
        self._run_counter += 1
        return f"qf_{label}_{self._run_counter}"

    def state(self, run_id: str) -> dict:
        path = self.state_root / run_id / "state.json"
        self.assertTrue(path.is_file(), f"missing persisted state for {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def log_text(self) -> str:
        return (
            self.log_path.read_text(encoding="utf-8") if self.log_path.is_file() else ""
        )

    # ------------------------------------------------------ 14-B1 backpressure

    def test_14_b1_backpressure_keeps_terminal_event_with_capacity_one_and_two(self):
        for capacity in (1, 2):
            with self.subTest(capacity=capacity):
                emitter = EventEmitter(buffer_size=capacity)
                stream = WorkflowEventStream(emitter, queue_maxsize=capacity)
                emitter.on_event(stream._publish)
                for index in range(40):
                    emit_event(
                        emitter,
                        "node_started",
                        "qf_backpressure",
                        node_name=f"node_{index}",
                    )
                # A slow consumer has read nothing yet: memory stays bounded.
                self.assertLessEqual(stream.buffered, capacity)
                emit_event(
                    emitter,
                    "final_result",
                    "qf_backpressure",
                    status="success",
                    result={"status": "success", "rows": [["kept"]]},
                )
                self.assertLessEqual(stream.buffered, capacity)
                stream._close()
                events = list(stream)
                self.assertEqual(events[-1].event_type, "final_result")
                self.assertEqual(events[-1].result["rows"], [["kept"]])
                self.assertEqual(events[-1].outcome, "success")
                self.assertTrue(stream.finished)
                self.assertGreater(stream.dropped_progress, 0)
                self.assertFalse(stream.protocol_violation)

    def test_14_b1_slow_consumer_still_receives_exactly_one_terminal_event(self):
        emitter = EventEmitter(buffer_size=2)
        stream = WorkflowEventStream(emitter, queue_maxsize=2)
        emitter.on_event(stream._publish)
        emit_event(emitter, "run_started", "qf_slow_consumer", status="running")
        first = stream.next_event(timeout=5.0)
        self.assertEqual(first.event_type, "run_started")
        # The consumer stalls while more progress events pile up.
        for index in range(30):
            emit_event(
                emitter,
                "node_started",
                "qf_slow_consumer",
                node_name=f"node_{index}",
            )
        emit_event(
            emitter,
            "final_result",
            "qf_slow_consumer",
            status="cancelled",
            result={"status": "cancelled"},
        )
        stream._close()
        remaining = []
        while True:
            event = stream.next_event(timeout=5.0)
            if event is None:
                self.assertTrue(stream.finished)
                break
            remaining.append(event)
        self.assertEqual(
            [event.event_type for event in remaining].count("final_result"), 1
        )
        self.assertEqual(remaining[-1].outcome, "cancelled")

    def test_14_b1_run_without_terminal_event_is_a_protocol_violation(self):
        emitter = EventEmitter(buffer_size=4)
        stream = WorkflowEventStream(emitter, queue_maxsize=4)
        emitter.on_event(stream._publish)
        emit_event(emitter, "run_started", "qf_no_terminal", status="running")
        stream._close()
        self.assertEqual([event.event_type for event in stream], ["run_started"])
        self.assertTrue(stream.protocol_violation)
        self.assertIsNone(stream.outcome)

    def test_14_b1_events_after_the_terminal_event_are_rejected(self):
        emitter = EventEmitter(buffer_size=8)
        events = []
        emitter.on_event(events.append)
        emit_event(emitter, "run_started", "qf_terminal_first", status="running")
        emit_event(
            emitter,
            "final_result",
            "qf_terminal_first",
            status="success",
            result={"status": "success"},
        )
        emit_event(emitter, "node_started", "qf_terminal_first", node_name="late")
        emit_event(
            emitter,
            "final_result",
            "qf_terminal_first",
            status="failed",
            error="late duplicate",
        )
        self.assertEqual(
            [event.event_type for event in events], ["run_started", "final_result"]
        )
        self.assertEqual(events[-1].outcome, "success")
        self.assertEqual(emitter.post_terminal_events, 2)
        self.assertEqual([event.sequence for event in events], [1, 2])

    # ------------------------------------------------------- 14-E1 disconnect

    def test_14_e1_normal_connection_is_not_disconnected_and_no_coroutine_warning(self):
        class NormalRequest:
            def __init__(self) -> None:
                self.calls = 0

            async def is_disconnected(self) -> bool:
                self.calls += 1
                return False

        request = NormalRequest()
        frames: list[str] = []
        event_stream = self.service(TracingLLM()).stream(
            "List item names", self.options("qf_e1_http")
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertFalse(asyncio.run(is_disconnected(request)))
            self.assertGreaterEqual(request.calls, 1)
            # The synchronous compatibility shim must not leave a coroutine
            # un-awaited either.
            self.assertFalse(await_disconnect(NormalRequest()))

            async def collect():
                async for frame in sse_event_generator(event_stream, NormalRequest()):
                    frames.append(frame)

            asyncio.run(collect())
        runtime_warnings = [
            str(item.message)
            for item in caught
            if issubclass(item.category, RuntimeWarning)
        ]
        self.assertEqual(runtime_warnings, [])
        self.assertTrue(frames)
        self.assertIn('"event_type":"run_started"', frames[0])
        self.assertIn('"event_type":"final_result"', frames[-1])
        self.assertEqual(frames[-1].count("final_result"), 1)
        self.assertIn('"protocol_version":"%s"' % PROTOCOL_VERSION, frames[0])
        # The terminal frame carries the payload-free usage/latency summary.
        terminal = json.loads(frames[-1].removeprefix("data: ").strip())
        observability = terminal["data"]["observability"]
        self.assertGreater(observability["usage"]["total_tokens"], 0)
        self.assertGreater(observability["latency"]["end_to_end_ms"], 0.0)
        self.assertGreater(observability["latency"]["by_kind"]["step"]["count"], 0)
        serialized_observability = json.dumps(observability)
        self.assertNotIn("SELECT", serialized_observability)
        self.assertNotIn('"rows"', serialized_observability)
        self.assertNotIn(SECRET_ROW_VALUE, serialized_observability)
        self.assertEqual(terminal["outcome"], "success")
        # The terminal event may carry the answer (SQL and rows) — only progress
        # frames must stay payload-free.
        self.assertEqual(terminal["result"]["status"], "success")

    # ----------------------------------------------------- 14-N1 HTTP round trip

    @unittest.skipUnless(
        FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed"
    )
    def test_14_n1_real_fastapi_sse_matches_non_streaming_result(self):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        service = self.service(TracingLLM())
        response = TestClient(create_app(service)).post(
            "/ask/stream",
            json={
                "question": "List item names",
                "database": str(self.database),
                "skills": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"].split(";")[0], "text/event-stream"
        )
        self.assertEqual(
            response.headers["x-queryforge-event-protocol"], PROTOCOL_VERSION
        )
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(payloads[0]["event_type"], "run_started")
        self.assertEqual(
            [payload["protocol_version"] for payload in payloads],
            [PROTOCOL_VERSION] * len(payloads),
        )
        self.assertEqual(
            [payload["sequence"] for payload in payloads],
            list(range(1, len(payloads) + 1)),
        )
        self.assertEqual(
            [payload["event_type"] for payload in payloads].count("final_result"), 1
        )
        terminal = payloads[-1]
        self.assertEqual(terminal["event_type"], "final_result")
        self.assertEqual(terminal["outcome"], "success")
        self.assertTrue(terminal["task_id"])
        # The terminal payload equals what the non-streaming call returns.
        direct = service.ask("List item names", self.options("qf_n1_direct"))
        self.assertEqual(terminal["result"]["status"], direct["status"])
        self.assertEqual(terminal["result"]["rows"], direct["rows"])
        self.assertEqual(terminal["result"]["sql"], direct["sql"])
        # Progress frames stay free of SQL text and result rows.
        progress = json.dumps(payloads[:-1], ensure_ascii=False)
        self.assertNotIn("SELECT", progress)
        self.assertNotIn('"rows"', progress)

    # ----------------------------------------------------------- 14-C1 cancel

    def test_14_c1_cancel_before_generation_reports_cancelled_not_failed(self):
        blocking = BlockingLLM()
        stream = self.service(blocking).stream(
            "List item names", self.options("qf_c1_before")
        )
        first = next(stream)
        self.assertEqual(first.event_type, "run_started")
        stream.cancel()
        blocking.release.set()
        events = [first, *stream]
        terminal = events[-1]
        self.assertEqual(terminal.event_type, "final_result")
        self.assertEqual(terminal.outcome, "cancelled")
        self.assertIsNone(stream.error)
        self.assertEqual(stream.result["status"], "cancelled")
        self.assertEqual(self.state("qf_c1_before")["status"], "cancelled")
        self.assertEqual(self.state("qf_c1_before")["outcome"], "cancelled")
        self.assertNotIn('"output_status": "failed"', self.log_text())

    def test_14_c1_cancel_during_sql_interrupts_the_statement(self):
        stream = self.service(SlowSqlLLM()).stream(
            "Count item pairs", self.options("qf_c1_sql")
        )
        started = time.perf_counter()
        saw_execute = False
        for event in stream:
            if event.event_type == "node_started" and event.node_name == "execute_sql":
                saw_execute = True
                stream.cancel()
        elapsed = time.perf_counter() - started
        self.assertTrue(saw_execute)
        # The SQLite progress handler aborts the statement instead of letting a
        # ~4s join finish, so the whole run is far shorter than the query.
        self.assertLess(elapsed, 3.0)
        self.assertEqual(stream.outcome, "cancelled")
        self.assertEqual(stream.result["status"], "cancelled")
        self.assertEqual(self.state("qf_c1_sql")["status"], "cancelled")
        self.assertNotEqual(self.state("qf_c1_sql")["status"], "failed")
        recorder = get_span_recorder("qf_c1_sql")
        self.assertTrue([span for span in recorder.spans if span.kind == "sql"])
        # Cancellation is not a retry trigger: the repair node never ran.
        self.assertNotIn("step.fix", [span.name for span in recorder.spans])

    def test_14_c1_cancel_after_completion_does_not_rewrite_the_completed_run(self):
        from queryforge.application.agent_service import persist_cancelled_outcome

        stream = self.service(TracingLLM()).stream(
            "List item names", self.options("qf_c1_after")
        )
        events = list(stream)
        terminal = events[-1]
        self.assertEqual(terminal.outcome, "success")
        self.assertEqual(terminal.result["status"], "success")
        self.assertEqual(self.state("qf_c1_after")["status"], "completed")
        # A late cancel must not rewrite an already completed run.
        stream.cancel()
        self.assertIsNone(
            persist_cancelled_outcome(
                state_root=self.state_root,
                run_id="qf_c1_after",
                reason="late cancel after completion",
            )
        )
        self.assertEqual(self.state("qf_c1_after")["status"], "completed")
        self.assertEqual(stream.outcome, "success")
        self.assertFalse(stream.cancelled_after_completion)

        # The published flag means "the persisted outcome was PRESERVED", so it is
        # the case where the cancel wrote nothing. The old expression returned the
        # inverse, which told every client the opposite of what happened.
        from queryforge.application.agent_service import (
            late_cancel_preserved_the_outcome,
        )

        self.assertTrue(late_cancel_preserved_the_outcome(None))
        self.assertFalse(
            late_cancel_preserved_the_outcome(
                {"run_id": "r", "status": "cancelled", "outcome": "cancelled"}
            )
        )

    # ------------------------------------------------------------ 14-E2 faults

    def test_14_e2_half_closed_client_never_sees_a_success_outcome(self):
        class HalfClosedRequest:
            def __init__(self, disconnect_after: int) -> None:
                self.calls = 0
                self.disconnect_after = disconnect_after

            async def is_disconnected(self) -> bool:
                self.calls += 1
                return self.calls > self.disconnect_after

        blocking = BlockingLLM()
        event_stream = self.service(blocking).stream(
            "List item names", self.options("qf_e2_half_closed")
        )
        frames: list[str] = []

        async def collect():
            async for frame in sse_event_generator(
                event_stream, HalfClosedRequest(disconnect_after=1)
            ):
                frames.append(frame)

        asyncio.run(collect())
        self.assertTrue(event_stream.is_cancelled())
        blocking.release.set()
        # Let the worker finish so the persisted outcome is observable.
        list(event_stream)
        self.assertTrue(frames)
        self.assertFalse(any("final_result" in frame for frame in frames))
        self.assertFalse(any('"outcome":"success"' in frame for frame in frames))
        # The front end never saw a success, and the run is honestly cancelled.
        self.assertEqual(self.state("qf_e2_half_closed")["status"], "cancelled")
        self.assertNotEqual(self.state("qf_e2_half_closed")["status"], "failed")

    def test_14_e2_abandoned_stream_reports_no_success_and_stays_bounded(self):
        llm = BlockingLLM()
        event_stream = self.service(llm).stream(
            "List item names", self.options("qf_e2_abandoned")
        )
        first = event_stream.next_event(timeout=5.0)
        self.assertEqual(first.event_type, "run_started")
        event_stream.cancel()
        llm.release.set()
        events = []
        while True:
            event = event_stream.next_event(timeout=5.0)
            if event is None:
                break
            events.append(event)
        terminals = [event for event in events if event.terminal]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].outcome, "cancelled")
        self.assertFalse(event_stream.protocol_violation)
        self.assertLessEqual(event_stream.buffered, event_stream.queue_maxsize)

    # ----------------------------------------------------------- 14-I1 tracing

    def test_14_i1_spans_attribute_parallel_candidates_repair_and_retrieval(self):
        llm = RepairingLLM()
        output = self.service(llm).ask(
            "List item names",
            self.options(
                "qf_i1",
                parallel_candidates=2,
                max_retries=1,
                show_run_summary=True,
            ),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(llm.reflections, 2, "one repair cycle must have run")
        recorder = get_span_recorder("qf_i1")
        self.assertIsNotNone(recorder)
        spans = list(recorder.spans)
        task_id = self.state("qf_i1")["task_id"]
        for span in spans:
            self.assertEqual(span.run_id, "qf_i1")
            self.assertEqual(span.task_id, task_id, span.name)
        kinds = {span.kind for span in spans}
        self.assertTrue({"model", "tool", "sql", "retrieval", "step"} <= kinds, kinds)
        # Parallel candidates run in worker threads yet are attributed to the
        # right run *and* the right step.
        candidate_spans = [
            span
            for span in spans
            if span.kind == "model" and span.node_name == "parallel_candidates"
        ]
        self.assertEqual(len(candidate_spans), 2)
        self.assertTrue(
            all(
                span.attributes["model_key"] == "openai/offline"
                for span in candidate_spans
            )
        )
        repair_spans = [
            span for span in spans if span.kind == "model" and span.node_name == "fix"
        ]
        self.assertEqual(len(repair_spans), 1)
        reflection_spans = [
            span for span in spans if span.kind == "model" and span.node_name == "reflect"
        ]
        self.assertEqual(len(reflection_spans), 2)
        retrieval_spans = [span for span in spans if span.kind == "retrieval"]
        self.assertEqual(len(retrieval_spans), 2)
        model_spans = [span for span in spans if span.kind == "model"]
        # Every model call the fake provider served is accounted for exactly once.
        self.assertEqual(len(model_spans), llm.calls)
        usage = recorder.usage_summary()
        self.assertEqual(usage["total_tokens"], usage_total(model_spans))
        self.assertEqual(usage["model_calls"], len(model_spans))
        self.assertEqual(usage["measured_calls"], len(model_spans))
        self.assertFalse(usage["estimated"])
        summary = output["run_summary"]
        self.assertEqual(summary["usage"]["total_tokens"], usage_total(model_spans))
        self.assertEqual(summary["spans"]["total"], len(spans))
        self.assertGreater(summary["latency"]["end_to_end_ms"], 0.0)
        self.assertGreater(summary["latency"]["by_kind"]["step"]["count"], 0)
        self.assertGreater(summary["latency"]["by_kind"]["model"]["count"], 0)
        self.assertGreater(summary["latency"]["by_kind"]["retrieval"]["count"], 0)

    def test_14_i1_usage_reports_cost_only_when_prices_are_configured(self):
        first = self._run_with_usage()
        self.assertIsNone(first["usage"]["estimated_cost_usd"])
        self.assertFalse(first["usage"]["price_table_configured"])
        configure_price_table(
            {"openai/offline": {"prompt_per_1k": 1.0, "completion_per_1k": 2.0}}
        )
        priced = self._run_with_usage()["usage"]
        self.assertTrue(priced["price_table_configured"])
        expected = (
            priced["prompt_tokens"] / 1000.0 * 1.0
            + priced["completion_tokens"] / 1000.0 * 2.0
        )
        self.assertAlmostEqual(
            priced["estimated_cost_usd"], round(expected, 6), places=6
        )

    def _run_with_usage(self) -> dict:
        run_id = self.next_run_id("cost")
        output = self.service(TracingLLM()).ask(
            "List item names", self.options(run_id, show_run_summary=True)
        )
        return output["run_summary"]

    # ------------------------------------------------------------- 14-M1 usage

    def _overlapping_run(self, provider) -> tuple:
        """Run one slow and one fast call against ``provider`` concurrently.

        The fast call completes while the slow one is still inside the adapter,
        so both calls have written the adapter's usage slot by the time the first
        of them reads it back.
        """

        run_id = self.next_run_id(f"race_{type(provider).__name__.lower()}")
        # A fresh recorder for this provider only: run ids must not be reused
        # across tests, or one test's spans leak into the next assertion.
        discard_span_recorder(run_id)
        self.addCleanup(discard_span_recorder, run_id)
        recorder = start_span_recorder(run_id)
        with run_logging_context(run_id):
            # Built inside the run context so the worker thread, which inherits
            # no contextvars, still attributes its span to this recorder.
            observed = ObservedModelProvider(
                provider,
                provider_name="fake",
                model_name="race",
                trace_dir=self.root / "traces",
            )
            slow = threading.Thread(target=lambda: observed.generate_json("slow"))
            slow.start()
            self.assertTrue(provider.entered.wait(timeout=15))
            observed.generate_json("fast")
            provider.release.set()
            slow.join(timeout=15)
        self.assertFalse(slow.is_alive())
        spans = [
            span
            for span in recorder.spans
            if span.kind == "model" and span.usage is not None
        ]
        self.assertTrue(all(span.run_id == run_id for span in spans))
        return recorder, spans

    def test_14_m1_overlapping_calls_keep_their_own_measured_usage(self):
        """Regression: a shared usage slot swapped tokens between two calls.

        Both calls reported the *fast* call's measured usage (5 tokens), so the
        slow call's tokens were recorded as a measurement of the wrong call.
        """

        recorder, spans = self._overlapping_run(OverlappingUsageLLM())
        by_call = {span.usage.raw["call"]: span for span in spans}
        self.assertEqual(sorted(by_call), ["fast", "slow"])
        self.assertEqual(by_call["fast"].usage.total_tokens, 5)
        self.assertEqual(by_call["slow"].usage.total_tokens, 9007)
        self.assertFalse(by_call["slow"].usage.estimated)
        self.assertEqual(by_call["slow"].attributes["usage_source"], "reported")
        summary = recorder.usage_summary()
        self.assertEqual(summary["total_tokens"], 9012)
        self.assertEqual(summary["measured_calls"], 2)
        self.assertFalse(summary["estimated"])

    def test_14_m1_non_isolatable_provider_never_credits_a_neighbour_usage(self):
        """Without per-call slots the shared one is estimated, never guessed."""

        _, spans = self._overlapping_run(SlottedUsageLLM())
        self.assertEqual(len(spans), 2)
        # The shared slot cannot be attributed to either overlapping call, so
        # neither may claim the other's measured tokens.
        self.assertTrue(all(span.usage.estimated for span in spans))
        self.assertTrue(
            all(span.attributes["usage_source"] == "estimated" for span in spans)
        )
        self.assertTrue(all(span.usage.total_tokens > 0 for span in spans))


    def test_14_m1_missing_provider_usage_is_estimated_never_zero(self):
        recorder = start_span_recorder("qf_m1_spans")
        measured = ObservedModelProvider(
            TracingLLM(),
            provider_name="openai",
            model_name="offline",
            trace_dir=self.root / "traces",
        )
        estimated = ObservedModelProvider(
            NoUsageLLM(),
            provider_name="qwen",
            model_name="qwen-plus",
            trace_dir=self.root / "traces",
        )
        prompt = "x" * 400
        with run_logging_context("qf_m1_spans"):
            measured.generate_json(prompt)
            estimated.generate_json(prompt)
        model_spans = [span for span in recorder.spans if span.kind == "model"]
        self.assertEqual(len(model_spans), 2)
        measured_span, estimated_span = model_spans
        self.assertFalse(measured_span.usage.estimated)
        self.assertEqual(measured_span.attributes["usage_source"], "reported")
        self.assertGreater(measured_span.usage.total_tokens, 0)
        self.assertTrue(estimated_span.usage.estimated)
        self.assertEqual(estimated_span.attributes["usage_source"], "estimated")
        self.assertGreater(estimated_span.usage.total_tokens, 0)
        summary = recorder.usage_summary()
        self.assertTrue(summary["estimated"])
        self.assertEqual(summary["measured_calls"], 1)
        self.assertEqual(summary["total_tokens"], usage_total(model_spans))
        # A provider that reports nothing must not be credited with a stale
        # measurement from an earlier call.
        provider = NoUsageLLM()
        provider.last_usage = ModelUsage(99, 99, 198, False, None)
        observed = ObservedModelProvider(
            provider, provider_name="qwen", model_name="qwen-plus"
        )
        observed.generate_json("another prompt")
        self.assertIsNone(provider.last_usage)
        self.assertIn("usage_estimated=True", self.log_text())
        self.assertIn(f"prompt_chars={len(prompt)}", self.log_text())

    def test_14_m1_usage_normalization_keeps_raw_and_never_fakes_zero(self):
        self.assertIsNone(normalize_usage(None))
        self.assertIsNone(normalize_usage({}))
        measured = normalize_usage(
            {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        )
        self.assertEqual(measured.prompt_tokens, 11)
        self.assertEqual(measured.total_tokens, 18)
        self.assertFalse(measured.estimated)
        self.assertEqual(measured.raw["completion_tokens"], 7)
        anthropic_style = normalize_usage({"input_tokens": 5, "output_tokens": 4})
        self.assertEqual(anthropic_style.total_tokens, 9)
        self.assertFalse(anthropic_style.estimated)
        already = ModelUsage(1, 1, 2, False, None)
        self.assertIs(normalize_usage(already), already)
        empty_estimate = ModelUsage.estimate(0, 0)
        self.assertTrue(empty_estimate.estimated)
        self.assertGreater(empty_estimate.total_tokens, 0)
        self.assertEqual(
            empty_estimate.total_tokens,
            empty_estimate.prompt_tokens + empty_estimate.completion_tokens,
        )

    # ------------------------------------------------------------ 14-S1 privacy

    def test_14_s1_secrets_rows_and_prompts_stay_out_of_spans_and_logs(self):
        service = self.service(TracingLLM())
        service.ask(
            f"List names; api_key={SECRET_TOKEN}",
            self.options("qf_s1", debug_prompts=False),
        )
        recorder = get_span_recorder("qf_s1")
        spans = json.dumps(
            [span.to_dict() for span in recorder.spans], ensure_ascii=False
        )
        log = self.log_text()
        self.assertTrue(spans)
        for haystack, label in ((spans, "spans"), (log, "logs")):
            self.assertNotIn(SECRET_TOKEN, haystack, label)
            self.assertNotIn(SECRET_ROW_VALUE, haystack, label)
        # Spans never carry the statement text, only its size and digest.
        self.assertNotIn("SELECT name FROM items", spans)
        self.assertIn("prompt_chars", log)
        sql_spans = [span for span in recorder.spans if span.kind == "sql"]
        self.assertTrue(sql_spans)
        self.assertIn("statement_digest", sql_spans[0].attributes)
        self.assertNotIn("statement", sql_spans[0].attributes)

    def test_14_s1_debug_prompts_stays_opt_in_and_redacts_secrets(self):
        trace_dir = self.root / "debug_traces"
        provider = ObservedModelProvider(
            TracingLLM(),
            provider_name="openai",
            model_name="offline",
            debug_prompts=True,
            trace_dir=trace_dir,
        )
        with run_logging_context("qf_s1_debug"):
            provider.generate_json(f"prompt with api_key={SECRET_TOKEN}")
        payload = json.loads(
            next((trace_dir / "qf_s1_debug").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertIn("[REDACTED]", payload["prompt"])
        self.assertNotIn(SECRET_TOKEN, payload["prompt"])
        self.assertNotIn(SECRET_TOKEN, self.log_text())
        self.assertIsNotNone(payload["usage"])

    # --------------------------------------------------------- 14-R1 transports

    def test_14_r1_cli_stream_service_mcp_and_report_paths_agree(self):
        class FakeStream:
            error = None
            result = {
                "status": "success",
                "run_id": "qf_r1_cli",
                "rows": [["item-0"]],
            }

            def __iter__(self):
                return iter(
                    [
                        WorkflowEvent(
                            event_type="node_started",
                            run_id="qf_r1_cli",
                            node_name="gen_sql",
                            message="Started gen_sql.",
                        ),
                        WorkflowEvent(
                            event_type="final_result",
                            run_id="qf_r1_cli",
                            status="success",
                            result=self.result,
                        ),
                    ]
                )

        with patch(
            "sys.argv", ["queryforge", "--question", "List names", "--stream"]
        ), patch.object(
            cli.AgentService, "stream", return_value=FakeStream()
        ), redirect_stdout(stdout := StringIO()), redirect_stderr(stderr := StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertIn("[node_started] gen_sql", stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "success")

        service = self.service(TracingLLM())
        direct = service.ask("List item names", self.options("qf_r1_service"))
        self.assertEqual(direct["status"], "success")
        self.assertTrue(direct["rows"])
        self.assertEqual(self._mcp_ask_status(service), "success")

        with self.assertRaises(ValueError):
            service.report_path("qf_r1_missing_report")

        # A cancelled run leaves no delivery report claiming success or failure.
        blocking = BlockingLLM()
        stream = self.service(blocking).stream(
            "List item names", self.options("qf_r1_cancelled")
        )
        next(stream)
        stream.cancel()
        blocking.release.set()
        list(stream)
        artifacts = list(
            (self.state_root / "qf_r1_cancelled" / "artifacts").glob(
                "*delivery_report*.json"
            )
        )
        self.assertEqual(artifacts, [])
        self.assertEqual(self.state("qf_r1_cancelled")["status"], "cancelled")

    def _mcp_ask_status(self, service: AgentService) -> str:
        """Call the MCP ``ask_sql`` tool through the real server wiring."""

        class FakeFastMCP:
            def __init__(self, name, json_response=False) -> None:
                self.name = name
                self.tools: dict = {}
                self.resources: dict = {}
                self.prompts: dict = {}

            def tool(self):
                def decorator(function):
                    self.tools[function.__name__] = function
                    return function

                return decorator

            def resource(self, uri):
                def decorator(function):
                    self.resources[uri] = function
                    return function

                return decorator

            def prompt(self, name=None):
                def decorator(function):
                    self.prompts[name or function.__name__] = function
                    return function

                return decorator

        mcp_module = types.ModuleType("mcp")
        mcp_module.__path__ = []
        server_module = types.ModuleType("mcp.server")
        server_module.__path__ = []
        fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        fastmcp_module.FastMCP = FakeFastMCP
        with patch.dict(
            sys.modules,
            {
                "mcp": mcp_module,
                "mcp.server": server_module,
                "mcp.server.fastmcp": fastmcp_module,
            },
        ):
            from queryforge.interfaces.mcp.server import create_mcp_server

            server = create_mcp_server(service)
        result = server.tools["ask_sql"](
            "List item names",
            database=str(self.database),
            skills=[],
        )
        self.assertTrue(result["rows"])
        return result["status"]

    # -------------------------------------------------------- protocol units

    def test_protocol_version_and_outcome_vocabulary_are_stable(self):
        self.assertEqual(PROTOCOL_VERSION, "1")
        for status, expected in (
            ("success", "success"),
            ("blocked", "blocked"),
            ("cancelled", "cancelled"),
            ("failed", "failed"),
            ("degraded", "partial"),
        ):
            emitter = EventEmitter(4)
            events = []
            emitter.on_event(events.append)
            emit_event(emitter, "run_started", "qf_outcome", status="running")
            emit_event(emitter, "final_result", "qf_outcome", status=status)
            self.assertEqual(events[-1].outcome, expected)
        unknown = EventEmitter(2)
        events = []
        unknown.on_event(events.append)
        emit_event(unknown, "final_result", "qf_unknown", status="something-new")
        # An unknown status can never be presented as success.
        self.assertEqual(events[-1].outcome, "partial")

    def test_emitter_sequences_are_gapless_and_per_run(self):
        emitter = EventEmitter(16)
        events = []
        emitter.on_event(events.append)
        for index in range(5):
            emit_event(emitter, "node_started", "qf_seq_a", node_name=f"n{index}")
        emit_event(emitter, "run_started", "qf_seq_b", status="running")
        emit_event(
            emitter,
            "final_result",
            "qf_seq_a",
            status="success",
            result={"status": "success"},
        )
        self.assertEqual(
            [event.sequence for event in events if event.run_id == "qf_seq_a"],
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(
            [event.sequence for event in events if event.run_id == "qf_seq_b"], [1]
        )

    def test_task_identity_is_bound_to_progress_events(self):
        emitter = EventEmitter(8)
        emitter.bind(run_id="qf_bound", task_id="task_bound")
        events = []
        emitter.on_event(events.append)
        emit_event(emitter, "phase_started", "qf_bound", phase_name="routing")
        emit_event(emitter, "final_result", "qf_bound", status="success", result={})
        self.assertTrue(events)
        self.assertTrue(all(event.task_id == "task_bound" for event in events))
        self.assertEqual(emitter.run_metadata["run_id"], "qf_bound")


if __name__ == "__main__":
    unittest.main()
