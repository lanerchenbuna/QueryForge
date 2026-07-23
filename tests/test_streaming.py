"""Offline tests for progress-only workflow event streaming."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import main as cli
from queryforge.workflow.event_emitter import EventEmitter, WorkflowEvent
from queryforge.interfaces.api.app import create_app
from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


class StreamingLLM:
    def __init__(self) -> None:
        self.generated = 0

    def generate_json(self, prompt: str) -> dict:
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The repaired query is correct.",
                "suggested_fix": None,
            }
        if "Repair the SQLite query" in prompt:
            return {
                "fixed_sql": "SELECT name FROM items ORDER BY name",
                "explanation": "Use the available name column.",
                "tables_used": ["items"],
            }
        self.generated += 1
        return {
            "sql": "SELECT missing FROM items",
            "explanation": "Initial invalid candidate.",
            "tables_used": ["items"],
        }


class StreamingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha')")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def service(self) -> AgentService:
        return AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: StreamingLLM(),
        )

    def options(self, run_id: str) -> AgentOptions:
        return AgentOptions(
            database=str(self.database),
            skills=[],
            run_id=run_id,
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def test_emitter_buffers_and_notifies_without_callback_failures(self):
        emitter = EventEmitter(buffer_size=2)
        received = []
        emitter.on_event(received.append)
        emitter.on_event(lambda _: (_ for _ in ()).throw(RuntimeError("ignored")))
        for index in range(3):
            emitter.emit(
                WorkflowEvent(
                    event_type="node_started",
                    run_id="qf_events",
                    node_name=f"node_{index}",
                )
            )
        self.assertEqual(len(received), 3)
        self.assertEqual(
            [event.node_name for event in emitter.get_events()],
            ["node_1", "node_2"],
        )

    def test_stream_emits_progress_retry_artifacts_and_safe_final_event(self):
        event_stream = self.service().stream(
            "List item names",
            self.options("stream_retry"),
        )
        events = list(event_stream)
        self.assertIsNone(event_stream.error)
        self.assertEqual(event_stream.result["rows"], [["alpha"]])
        event_types = [event.event_type for event in events]
        self.assertEqual(event_types[0], "run_started")
        self.assertIn("node_started", event_types)
        self.assertIn("node_completed", event_types)
        self.assertIn("retrying", event_types)
        self.assertIn("phase_started", event_types)
        self.assertIn("phase_completed", event_types)
        self.assertIn("artifact_created", event_types)
        self.assertEqual(event_types[-1], "final_result")
        serialized = json.dumps(
            [event.model_dump(mode="json") for event in events],
            ensure_ascii=False,
        )
        self.assertNotIn("SELECT", serialized)
        self.assertNotIn("alpha", serialized)
        self.assertNotIn("rows", serialized)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_api_sse_returns_progress_only_events(self):
        from fastapi.testclient import TestClient

        response = TestClient(create_app(self.service())).post(
            "/ask/stream",
            json={
                "question": "List item names",
                "database": str(self.database),
                "skills": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(payloads[0]["event_type"], "run_started")
        self.assertEqual(payloads[-1]["event_type"], "final_result")
        self.assertNotIn("rows", response.text)
        self.assertNotIn("SELECT", response.text)

    def test_cli_stream_writes_progress_to_stderr_and_json_to_stdout(self):
        class FakeStream:
            error = None
            result = {
                "status": "success",
                "run_id": "qf_cli_stream",
                "rows": [],
            }

            def __iter__(self):
                return iter(
                    [
                        WorkflowEvent(
                            event_type="node_started",
                            run_id="qf_cli_stream",
                            node_name="gen_sql",
                            message="Started gen_sql.",
                        ),
                        WorkflowEvent(
                            event_type="final_result",
                            run_id="qf_cli_stream",
                            status="success",
                            message="QueryForge workflow completed.",
                        ),
                    ]
                )

        with patch(
            "sys.argv",
            ["queryforge", "--question", "List names", "--stream"],
        ), patch.object(cli.AgentService, "stream", return_value=FakeStream()), redirect_stdout(
            stdout := StringIO()
        ), redirect_stderr(stderr := StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertIn("[node_started] gen_sql", stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["status"], "success")


if __name__ == "__main__":
    unittest.main()
