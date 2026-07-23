"""Offline tests for persistent conversation memory and deterministic rewrites."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.orchestration.agents.product_analyst import ProductAnalystAgent
from queryforge.orchestration.schemas.session import SessionMemory
from queryforge.core.config import Config
from queryforge.interfaces.gateway import GatewayAdapter
from queryforge.application import AgentOptions, AgentService


class ConversationLLM:
    def generate_json(self, prompt: str) -> dict:
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


class ConversationMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT, category TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha', 'books')")
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
            llm_factory=lambda _: ConversationLLM(),
        )

    def options(
        self,
        run_id: str,
        *,
        session_id: str | None = None,
        reset_session: bool = False,
    ) -> AgentOptions:
        return AgentOptions(
            database=str(self.database),
            skills=[],
            run_id=run_id,
            session_id=session_id,
            reset_session=reset_session,
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )

    def test_followup_rules_cover_required_operations(self):
        memory = SessionMemory(
            session_id="rules",
            last_question="Show revenue by region for last month",
        )
        cases = {
            "by product category": "add_dimension",
            "only include East": "add_filter",
            "also include order count": "add_metric",
            "remove region": "remove_dimension_or_metric",
            "top 5": "set_ranking",
            "by time": "add_time_dimension",
        }
        for question, expected_reason in cases.items():
            with self.subTest(question=question):
                rewrite = ProductAnalystAgent.rewrite_followup(question, memory)
                self.assertTrue(rewrite["is_followup"])
                self.assertEqual(rewrite["reason"], expected_reason)
                self.assertIn(memory.last_question, str(rewrite["question"]))

    def test_followup_is_rewritten_persisted_and_does_not_store_rows(self):
        service = self.service()
        first = service.ask(
            "List item names",
            self.options("conversation_first", session_id="analysis_1"),
        )
        second = service.ask(
            "by category",
            self.options("conversation_followup", session_id="analysis_1"),
        )
        self.assertEqual(first["session"]["session_id"], "analysis_1")
        self.assertTrue(second["session"]["is_followup"])
        self.assertIn("Prior analytical request: List item names", second["question"])
        self.assertEqual(second["session"]["original_question"], "by category")

        state = json.loads(
            Path(second["agent_team"]["state_path"]).read_text(encoding="utf-8")
        )
        analysis_ref = next(
            item for item in state["artifacts"]
            if item["artifact_type"] == "analysis_request"
        )
        analysis = json.loads(
            (Path(second["agent_team"]["state_path"]).parent / analysis_ref["path"])
            .read_text(encoding="utf-8")
        )["payload"]
        self.assertTrue(analysis["is_followup"])
        self.assertEqual(analysis["rewritten_from"], "by category")

        session_path = Path(second["session"]["path"])
        persisted = json.loads(session_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["turn_count"], 2)
        self.assertEqual(len(persisted["history"]), 2)
        self.assertNotIn("rows", json.dumps(persisted))
        self.assertEqual(persisted["last_result_schema"], ["name"])

    def test_session_persists_across_service_instances_and_is_isolated(self):
        self.service().ask(
            "List item names",
            self.options("persistence_first", session_id="shared"),
        )
        resumed = self.service().ask(
            "only include books",
            self.options("persistence_second", session_id="shared"),
        )
        isolated = self.service().ask(
            "only include books",
            self.options("isolation", session_id="other"),
        )
        self.assertTrue(resumed["session"]["is_followup"])
        self.assertFalse(isolated["session"]["is_followup"])
        self.assertEqual(isolated["question"], "only include books")

    def test_reset_session_discards_prior_context(self):
        service = self.service()
        service.ask(
            "List item names",
            self.options("reset_first", session_id="resettable"),
        )
        output = service.ask(
            "by category",
            self.options(
                "reset_second",
                session_id="resettable",
                reset_session=True,
            ),
        )
        self.assertFalse(output["session"]["is_followup"])
        persisted = json.loads(Path(output["session"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["turn_count"], 1)
        self.assertEqual(len(persisted["history"]), 1)

    def test_session_history_is_bounded_to_ten_turns(self):
        service = self.service()
        for number in range(11):
            service.ask(
                f"List item names {number}",
                self.options(f"bounded_{number}", session_id="bounded"),
            )
        session_path = (
            self.root / ".queryforge" / "sessions" / "bounded.json"
        )
        persisted = json.loads(session_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["turn_count"], 11)
        self.assertEqual(len(persisted["history"]), 10)
        self.assertEqual(persisted["history"][0]["turn_number"], 2)

    def test_no_session_keeps_single_turn_output_and_writes_no_session_file(self):
        output = self.service().ask(
            "List item names",
            self.options("single_turn"),
        )
        self.assertNotIn("session", output)
        self.assertFalse((self.root / ".queryforge" / "sessions").exists())

    def test_gateway_uses_stable_session_per_user_and_channel(self):
        adapter = GatewayAdapter(self.service())
        first = adapter.handle(user_id="user 1", channel="sales", text="List item names")
        second = adapter.handle(user_id="user 1", channel="sales", text="by category")
        other = adapter.handle(user_id="user 1", channel="support", text="by category")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["session_id"], other["session_id"])


if __name__ == "__main__":
    unittest.main()
