"""Offline Phase A acceptance tests for routing, role artifacts, and gates."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.orchestration.agents.knowledge import KnowledgeAgent
from queryforge.orchestration.agents.product_analyst import ProductAnalystAgent
from queryforge.orchestration.agents.schema_architect import SchemaArchitectAgent
from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService


class CountingPhaseALLM:
    """Deterministic provider used to assert Phase A adds no model calls."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, prompt: str) -> dict:
        self.calls += 1
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        if "Repair the SQLite query" in prompt:
            return {
                "fixed_sql": "SELECT name FROM items ORDER BY name",
                "explanation": "Use the existing name column.",
                "tables_used": ["items"],
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


class PhaseAIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "phase_a.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, "
            "FOREIGN KEY(customer_id) REFERENCES customers(id))"
        )
        connection.execute("INSERT INTO items VALUES ('alpha')")
        connection.execute("INSERT INTO customers VALUES (1, 'Ada')")
        connection.execute("INSERT INTO orders VALUES (1, 1)")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
        )
        self.llm = CountingPhaseALLM()
        self.service = AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: self.llm,
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

    def read_state(self, output: dict) -> tuple[dict, Path]:
        state_path = Path(output["agent_team"]["state_path"])
        return json.loads(state_path.read_text(encoding="utf-8")), state_path

    def artifact_payload(self, state: dict, state_path: Path, artifact_type: str) -> dict:
        reference = next(
            artifact for artifact in state["artifacts"]
            if artifact["artifact_type"] == artifact_type
        )
        return self.artifact_payload_for_reference(state_path, reference)

    @staticmethod
    def artifact_payload_for_reference(state_path: Path, reference: dict) -> dict:
        return json.loads(
            (state_path.parent / reference["path"]).read_text(encoding="utf-8")
        )["payload"]

    def test_router_recognizes_two_phrasings_for_every_task_type(self):
        cases = {
            "ask_sql": ("List item names", "How many items are there?"),
            "sql_review": (
                "Review SQL: SELECT name FROM items",
                "Check SQL: SELECT name FROM items",
            ),
            "metadata_query": ("List tables", "Show database schema"),
            "troubleshoot_sql": (
                "SQL error: SELECT missing FROM items",
                "Fix this SQL: SELECT missing FROM items",
            ),
            "explain_result": ("Explain result for items", "Why did this query return rows?"),
            "build_report": ("Build report for items", "Generate report for items"),
            "unknown": ("...", "!!!"),
        }
        for expected, prompts in cases.items():
            for prompt in prompts:
                with self.subTest(expected=expected, prompt=prompt):
                    self.assertEqual(
                        self.service.entry_router.route(prompt).task_type,
                        expected,
                    )

    def test_every_pipeline_persists_state_and_delivery(self):
        cases = (
            ("ask_sql", "List item names", "success", "qa_report"),
            ("sql_review", "Review SQL: SELECT name FROM items", "success", "review_report"),
            ("metadata_query", "List tables", "success", "schema_plan"),
            (
                "troubleshoot_sql",
                "SQL error: SELECT missing FROM items",
                "success",
                "sql_candidate",
            ),
            ("explain_result", "Explain result for item names", "success", "explanation_report"),
            ("build_report", "Build report for item names", "success", "report_artifact"),
            ("unknown", "...", "blocked", "analysis_request"),
        )
        for task_type, question, status, required_artifact in cases:
            with self.subTest(task_type=task_type):
                output = self.service.ask(question, self.options(f"phase_a_{task_type}"))
                self.assertEqual(output["agent_team"]["task_type"], task_type)
                self.assertEqual(output["status"], status)
                state, state_path = self.read_state(output)
                self.assertTrue(state_path.is_file())
                self.assertIn("delivery_report", [a["artifact_type"] for a in state["artifacts"]])
                self.assertIn(
                    required_artifact,
                    [artifact["artifact_type"] for artifact in state["artifacts"]],
                )
                self.assertIn("delivery_report", output)

    def test_troubleshoot_uses_extracted_sql_then_fix_loop(self):
        output = self.service.ask(
            "SQL error: SELECT missing FROM items",
            self.options("phase_a_troubleshoot_details"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["sql"], "SELECT name FROM items ORDER BY name")
        state, state_path = self.read_state(output)
        candidates = [
            self.artifact_payload_for_reference(state_path, artifact)
            for artifact in state["artifacts"]
            if artifact["artifact_type"] == "sql_candidate"
        ]
        self.assertEqual(
            candidates[0]["sql"].rstrip(";"),
            "SELECT missing FROM items",
        )
        self.assertEqual(candidates[-1]["sql"], "SELECT name FROM items ORDER BY name")

    def test_role_decisions_and_quality_gates_are_visible(self):
        output = self.service.ask(
            "Show top users growth",
            self.options("phase_a_role_decisions"),
        )
        self.assertEqual(output["status"], "success")
        state, state_path = self.read_state(output)
        analysis = self.artifact_payload(state, state_path, "analysis_request")
        schema = self.artifact_payload(state, state_path, "schema_plan")
        self.assertGreaterEqual(len(analysis["ambiguities"]), 3)
        self.assertEqual(
            set(analysis["ambiguities"]) & {
                "missing_ranking_dimension",
                "missing_time_range",
                "ambiguous_count_grain",
                "missing_comparison_baseline",
            },
            set(analysis["ambiguities"]),
        )
        self.assertTrue(schema["primary_tables"])
        self.assertTrue(schema["join_paths"])
        self.assertIn("recommended_fields", schema)
        self.assertIn("risks", schema)
        self.assertTrue(output["agent_team"]["warnings"])
        self.assertEqual(state["pending_phases"], [])
        self.assertTrue(
            all(
                artifact["status"] in {"valid", "warning", "blocked", "degraded"}
                for artifact in state["artifacts"]
            )
        )

    def test_simple_ask_sql_keeps_two_model_calls_and_compatible_output(self):
        output = self.service.ask(
            "List item names",
            self.options("phase_a_call_baseline"),
        )
        self.assertEqual(output["status"], "success")
        self.assertEqual(self.llm.calls, 2)
        for field in ("sql", "explanation", "columns", "rows", "row_count"):
            self.assertIn(field, output)
        self.assertEqual(output["rows"], [["alpha"]])

    def test_role_fallbacks_persist_warning_degraded_and_blocked_artifacts(self):
        with patch.object(
            ProductAnalystAgent,
            "_run_parsed",
            side_effect=RuntimeError("parse failed"),
        ):
            output = self.service.ask(
                "List item names",
                self.options("phase_a_product_fallback"),
            )
        state, state_path = self.read_state(output)
        analysis = self.artifact_payload(state, state_path, "analysis_request")
        self.assertEqual(analysis["status"], "warning")
        self.assertEqual(output["status"], "success")

        with patch.object(
            KnowledgeAgent,
            "_rank_history",
            side_effect=RuntimeError("history unavailable"),
        ):
            output = self.service.ask(
                "List item names",
                self.options("phase_a_knowledge_fallback"),
            )
        state, state_path = self.read_state(output)
        knowledge = self.artifact_payload(state, state_path, "knowledge_context")
        knowledge_ref = next(
            artifact for artifact in state["artifacts"]
            if artifact["artifact_type"] == "knowledge_context"
        )
        self.assertEqual(knowledge_ref["status"], "degraded")
        self.assertEqual(knowledge["retrieval_status"]["history"], "degraded")
        self.assertEqual(output["status"], "success")

        with patch.object(
            SchemaArchitectAgent,
            "_run_planned",
            side_effect=RuntimeError("schema unavailable"),
        ):
            output = self.service.ask(
                "List item names",
                self.options("phase_a_schema_fallback"),
            )
        state, state_path = self.read_state(output)
        schema = self.artifact_payload(state, state_path, "schema_plan")
        self.assertEqual(schema["status"], "blocked")
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(state["blocked_phase"], "analysis")


if __name__ == "__main__":
    unittest.main()
