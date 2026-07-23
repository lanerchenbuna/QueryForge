"""End-to-end acceptance checks for the completed Phase 2 capability set."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from queryforge.orchestration.agents.entry_router import EntryRouterAgent
from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


class FinalAcceptanceLLM:
    def generate_json(self, prompt: str) -> dict:
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the request.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


class Phase2FinalAcceptanceTest(unittest.TestCase):
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
            report_output_dir=str(self.root / "reports"),
        )
        self.service = AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: FinalAcceptanceLLM(),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def options(self, run_id: str) -> AgentOptions:
        return AgentOptions(
            database=str(self.database),
            skills=[],
            run_id=run_id,
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def test_all_task_types_route_to_declared_pipelines(self):
        cases = {
            "List item names": "ask_sql",
            "Review SQL: SELECT name FROM items": "sql_review",
            "SQL error: SELECT missing FROM items": "troubleshoot_sql",
            "Explain result for item names": "explain_result",
            "Build report for item names": "build_report",
            "List tables": "metadata_query",
            "...": "unknown",
        }
        router = EntryRouterAgent()
        for question, task_type in cases.items():
            with self.subTest(question=question):
                self.assertEqual(router.route(question).task_type, task_type)

    def test_default_ask_sql_keeps_compatible_output_and_minimal_features(self):
        output = self.service.ask("List item names", self.options("phase2_default"))
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["rows"], [["alpha"]])
        self.assertEqual(output["tool_loop"]["status"], "disabled")
        self.assertIsNone(output["candidate_selection"])
        self.assertIsNone(output["subject"])
        self.assertNotIn("session", output)
        for key in ("sql", "explanation", "columns", "rows", "row_count", "agent_team"):
            self.assertIn(key, output)

    def test_report_and_stream_complete_without_changing_query_security_path(self):
        reported = self.service.ask(
            "List item names",
            replace(self.options("phase2_report"), report=True),
        )
        self.assertTrue(Path(reported["report"]["file_path"]).is_file())
        stream = self.service.stream("List item names", self.options("phase2_stream"))
        events = list(stream)
        self.assertIsNone(stream.error)
        self.assertEqual(stream.result["sql"], "SELECT name FROM items ORDER BY name")
        self.assertEqual(events[0].event_type, "run_started")
        self.assertEqual(events[-1].event_type, "final_result")


if __name__ == "__main__":
    unittest.main()
