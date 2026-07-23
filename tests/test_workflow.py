import unittest
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.workflow.workflow import Workflow, WorkflowError
from queryforge.workflow.node.base import Node
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, NodeResult, SqlTask


class WorkflowTest(unittest.TestCase):
    def test_full_workflow_reaches_output_with_fake_llm(self) -> None:
        temporary = tempfile.NamedTemporaryFile(suffix=".sqlite")
        database_path = Path(temporary.name)
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE schools (name TEXT, phone TEXT)")
        connection.execute("INSERT INTO schools VALUES ('Example School', '555-0100')")
        connection.commit()
        connection.close()

        class FakeLLM:
            def generate_json(self, prompt: str) -> dict[str, Any]:
                self.prompt = prompt
                if "Select local QueryForge skills" in prompt:
                    return {"skills": [], "reason": "No optional skill needed."}
                if "Evaluate whether the SQL and result" in prompt:
                    return {
                        "success": True,
                        "strategy": "SUCCESS",
                        "reason": "The query answers the question.",
                        "suggested_fix": None,
                    }
                return {
                    "sql": "SELECT phone FROM schools LIMIT 1",
                    "explanation": "Return the first school's phone number.",
                    "tables_used": ["schools"],
                }

        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="test-model",
            llm_base_url=None,
            database_path=str(database_path),
            api_key_env_names=("OPENAI_API_KEY",),
            history_db_path=str(database_path.with_suffix(".history.sqlite")),
        )
        output = WorkflowRunner(config, llm_factory=lambda _: FakeLLM()).run(
            SqlTask(
                question="What is the school's phone number?",
                database_path=str(database_path),
            )
        )

        self.assertEqual(output["status"], "success")
        self.assertEqual(output["columns"], ["phone"])
        self.assertEqual(output["rows"], [["555-0100"]])
        self.assertEqual(output["row_count"], 1)
        self.assertNotIn("semantic_model", output)
        temporary.close()

    def test_plan_mode_auto_approval_completes_workflow(self) -> None:
        temporary = tempfile.NamedTemporaryFile(suffix=".sqlite")
        database_path = Path(temporary.name)
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE events (id INTEGER, happened_on TEXT)")
        connection.execute("INSERT INTO events VALUES (1, '2025-05-15')")
        connection.commit()
        connection.close()

        class FakeLLM:
            def generate_json(self, prompt: str) -> dict[str, Any]:
                if "Select local QueryForge skills" in prompt:
                    return {"skills": [], "reason": "No optional skill needed."}
                if "Evaluate whether the SQL and result" in prompt:
                    return {
                        "success": True,
                        "strategy": "SUCCESS",
                        "reason": "The result is correct.",
                        "suggested_fix": None,
                    }
                return {
                    "sql": "SELECT id FROM events WHERE happened_on = '2025-05-15'",
                    "explanation": "Use the resolved date.",
                    "tables_used": ["events"],
                }

        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="test-model",
            llm_base_url=None,
            database_path=str(database_path),
            history_db_path=str(database_path.with_suffix(".history.sqlite")),
        )
        output = WorkflowRunner(
            config,
            llm_factory=lambda _: FakeLLM(),
            today_provider=lambda: date(2025, 5, 15),
            plan_mode=True,
            auto_approve_plan=True,
        ).run(
            SqlTask(question="Show today's events", database_path=str(database_path))
        )
        self.assertEqual(output["rows"], [[1]])
        self.assertTrue(output["plan"]["approved"])
        self.assertEqual(output["date_context"]["ranges"][0]["start_date"], "2025-05-15")
        temporary.close()

    def test_workflow_stops_after_failed_node(self) -> None:
        calls: list[str] = []

        class RecordingNode(Node):
            def __init__(self, name: str, succeeds: bool) -> None:
                self.name = name
                self.succeeds = succeeds

            def execute(self, context: Context) -> NodeResult:
                calls.append(self.name)
                if self.succeeds:
                    return self.success("ok")
                return self.failure("expected failure")

        context = Context(
            task=SqlTask(question="test", database_path="sample.sqlite")
        )
        workflow = Workflow(
            context,
            [
                RecordingNode("first", True),
                RecordingNode("failing", False),
                RecordingNode("must_not_run", True),
            ],
        )
        with self.assertRaisesRegex(WorkflowError, "node=failing"):
            workflow.run()
        self.assertEqual(calls, ["first", "failing"])


if __name__ == "__main__":
    unittest.main()
