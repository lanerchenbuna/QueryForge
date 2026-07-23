"""Offline end-to-end acceptance for Phase B and C capabilities."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


class PhaseBCLLM:
    """Deterministic provider that drives one combined advanced workflow."""

    def __init__(self) -> None:
        self.candidate_responses = [
            {
                "sql": "SELECT missing FROM items",
                "explanation": "Invalid candidate.",
                "tables_used": ["items"],
            },
            {
                "sql": "SELECT category, name FROM items ORDER BY category, name",
                "explanation": "List item names by category.",
                "tables_used": ["items"],
                "reasoning": {
                    "goal": "List item names by category",
                    "grain": "item",
                    "tables": ["items"],
                    "dimensions": [],
                    "sorting": [{"column": "category", "direction": "ASC"}],
                    "assumptions": [],
                    "risks": [],
                    "confidence": 0.9,
                    "strategy": "parallel_candidate",
                },
            },
        ]

    def generate_json(self, prompt: str) -> dict:
        if "Choose one bounded read-only action" in prompt:
            return {"action": "list_tables", "params": {}}
        if "Generate candidate" in prompt:
            return self.candidate_responses.pop(0)
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The selected SQL answers the request.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


class PhaseBCIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT, category TEXT)")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [("alpha", "books"), ("beta", "music")],
        )
        connection.commit()
        connection.close()
        self.subjects = self.root / "subjects.yml"
        self.subjects.write_text(
            """version: "1.0"
default_subject: catalog
subjects:
  - id: catalog
    name: Catalog
    description: Item catalog analytics
    synonyms: [item, items, category]
    tables: [items]
""",
            encoding="utf-8",
        )
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )
        self.service = AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: PhaseBCLLM(),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def options(self, run_id: str, **overrides) -> AgentOptions:
        values = {
            "database": str(self.database),
            "skills": [],
            "run_id": run_id,
            "orchestration_state_root": str(self.root / ".queryforge" / "runs"),
        }
        values.update(overrides)
        return AgentOptions(**values)

    def test_default_options_keep_advanced_features_minimal(self):
        output = self.service.ask("List item names", self.options("defaults"))
        self.assertNotIn("session", output)
        self.assertEqual(output["tool_loop"]["status"], "disabled")
        self.assertIsNone(output["candidate_selection"])
        self.assertIsNone(output["subject"])
        self.assertIsNone(output["reasoning"])

    def test_session_tool_loop_parallel_reasoning_and_subject_scope_work_together(self):
        first = self.service.ask(
            "List item names",
            self.options("phasebc_first", session_id="phasebc"),
        )
        advanced = self.service.ask(
            "by category",
            self.options(
                "phasebc_advanced",
                session_id="phasebc",
                tool_loop_enabled=True,
                tool_loop_max_rounds=1,
                parallel_candidates=2,
                parallel_max_preview=2,
                subject_tree_enabled=True,
                subject_tree_path=str(self.subjects),
            ),
        )

        self.assertEqual(first["status"], "success")
        self.assertTrue(advanced["session"]["is_followup"])
        self.assertEqual(advanced["subject"]["status"], "selected")
        self.assertEqual(advanced["subject"]["subject"]["id"], "catalog")
        self.assertEqual(advanced["relevant_tables"], ["items"])
        self.assertEqual(advanced["tool_loop"]["status"], "max_rounds")
        self.assertEqual(advanced["tool_loop"]["rounds"], 1)
        self.assertEqual(advanced["candidate_selection"]["selected_index"], 1)
        self.assertEqual(advanced["sql"], "SELECT category, name FROM items ORDER BY category, name")
        self.assertEqual(advanced["reasoning"]["goal"], "List item names by category")
        self.assertEqual(advanced["reasoning_validation"]["status"], "valid")

        state_path = Path(advanced["agent_team"]["state_path"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact_types = {item["artifact_type"] for item in state["artifacts"]}
        self.assertTrue(
            {
                "conversation_context",
                "subject_selection",
                "tool_loop_trace",
                "candidate_selection",
                "sql_candidate",
                "governance_report",
            }.issubset(artifact_types)
        )


if __name__ == "__main__":
    unittest.main()
