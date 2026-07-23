"""Offline tests for SQL candidate scoring and bounded selection."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from queryforge.workflow.node.parallel_candidates_node import ParallelCandidatesNode
from queryforge.workflow.sql_selector import SQLSelector
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.domain.semantic import MetricMatch, SemanticMetric
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class CandidateLLM:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)

    def generate_json(self, prompt: str) -> dict:
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "none"}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        if self.responses:
            return self.responses.pop(0)
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List names.",
            "tables_used": ["items"],
        }


class ParallelCandidatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE items (name TEXT, category TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            [("alpha", "a", 10), ("beta", "b", 20), ("gamma", "a", 30)],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def context(self) -> Context:
        return Context(
            task=SqlTask(
                question="List item names",
                database_path=str(self.database),
            )
        )

    def tool(self) -> DatabaseTool:
        return DatabaseTool(SQLiteConnector(str(self.database)))

    def test_selector_rejects_unsafe_and_selects_executable_candidate(self):
        selector = SQLSelector(self.tool(), max_preview=3, preview_limit=2)
        result = selector.select(
            [
                {"sql": "DROP TABLE items"},
                {
                    "sql": "SELECT name FROM items ORDER BY name",
                    "explanation": "valid",
                    "tables_used": ["items"],
                },
            ],
            self.context(),
        )
        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["evaluations"][0]["status"], "rejected")
        self.assertFalse(result["evaluations"][0]["ast_valid"])
        self.assertEqual(result["evaluations"][1]["status"], "eligible")
        self.assertTrue(result["evaluations"][1]["execution_success"])

    def test_selector_scores_non_empty_result_above_empty_result(self):
        selector = SQLSelector(self.tool(), max_preview=2)
        result = selector.select(
            [
                {"sql": "SELECT name FROM items WHERE 1 = 0"},
                {"sql": "SELECT name FROM items"},
            ],
            self.context(),
        )
        self.assertEqual(result["selected_index"], 1)
        self.assertGreater(
            result["evaluations"][1]["score"],
            result["evaluations"][0]["score"],
        )

    def test_selector_prefers_candidate_matching_metric_expression(self):
        context = self.context()
        context.metric_matches = [
            MetricMatch(
                matched_term="revenue",
                metric=SemanticMetric(
                    name="revenue",
                    description="Total item revenue.",
                    entity="items",
                    aggregation="sum",
                    expression="SUM(items.amount)",
                ),
            )
        ]
        result = SQLSelector(self.tool(), max_preview=2).select(
            [
                {"sql": "SELECT COUNT(*) AS revenue FROM items"},
                {"sql": "SELECT SUM(amount) AS revenue FROM items"},
            ],
            context,
        )
        self.assertEqual(result["selected_index"], 1)
        self.assertGreater(
            result["evaluations"][1]["score_components"]["semantic_match"],
            result["evaluations"][0]["score_components"]["semantic_match"],
        )
        self.assertEqual(
            result["evaluations"][1]["reflect_status"],
            "not_available_pre_selection",
        )

    def test_selector_tie_is_deterministic_and_preview_budget_is_bounded(self):
        selector = SQLSelector(self.tool(), max_preview=1, preview_limit=1)
        result = selector.select(
            [
                {"sql": "SELECT name FROM items"},
                {"sql": "SELECT category FROM items"},
            ],
            self.context(),
        )
        self.assertEqual(result["selected_index"], 0)
        self.assertEqual(result["evaluations"][1]["status"], "not_previewed")

    def test_parallel_node_generates_two_and_records_selection(self):
        llm = CandidateLLM(
            [
                {
                    "sql": "SELECT missing FROM items",
                    "explanation": "bad",
                    "tables_used": ["items"],
                },
                {
                    "sql": "SELECT name FROM items ORDER BY name",
                    "explanation": "good",
                    "tables_used": ["items"],
                },
            ]
        )
        context = self.context()
        result = ParallelCandidatesNode(
            llm,
            self.tool(),
            candidate_count=2,
            max_preview=2,
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.candidate_selection["selected_index"], 1)
        self.assertEqual(
            context.sql_context.sql,
            "SELECT name FROM items ORDER BY name",
        )
        self.assertEqual(len(context.candidate_selection["evaluations"]), 2)

    def test_parallel_node_generates_candidates_concurrently(self):
        class DelayedCandidateLLM(CandidateLLM):
            def generate_json(self, prompt: str) -> dict:
                if "Generate candidate" in prompt:
                    time.sleep(0.15)
                    return {
                        "sql": "SELECT name FROM items ORDER BY name",
                        "explanation": "List names.",
                        "tables_used": ["items"],
                    }
                return super().generate_json(prompt)

        started = time.monotonic()
        context = self.context()
        result = ParallelCandidatesNode(
            DelayedCandidateLLM([]),
            self.tool(),
            candidate_count=2,
        ).execute(context)
        elapsed = time.monotonic() - started
        self.assertTrue(result.success)
        self.assertLess(elapsed, 0.27)
        self.assertEqual(context.candidate_selection["generation_mode"], "concurrent")
        self.assertTrue(
            all(
                "generation_duration_ms" in candidate
                for candidate in context.candidate_selection["candidates"]
            )
        )

    def test_selected_candidate_preserves_structured_reasoning(self):
        llm = CandidateLLM(
            [
                {
                    "sql": "SELECT name FROM items ORDER BY name",
                    "explanation": "List item names.",
                    "tables_used": ["items"],
                    "reasoning": {
                        "goal": "List item names",
                        "tables": ["items"],
                        "sorting": [{"column": "name", "direction": "ASC"}],
                        "confidence": 0.9,
                    },
                },
                {
                    "sql": "SELECT category FROM items",
                    "explanation": "List categories.",
                    "tables_used": ["items"],
                },
            ]
        )
        context = self.context()
        result = ParallelCandidatesNode(
            llm,
            self.tool(),
            candidate_count=2,
        ).execute(context)
        self.assertTrue(result.success)
        self.assertIsNotNone(context.reasoning_result)
        self.assertEqual(context.reasoning_result.goal, "List item names")
        self.assertEqual(context.reasoning_validation["status"], "valid")

    def test_service_parallel_option_persists_trace_and_default_is_one(self):
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: CandidateLLM(
                [
                    {
                        "sql": "SELECT name FROM items",
                        "explanation": "one",
                        "tables_used": ["items"],
                    },
                    {
                        "sql": "SELECT category FROM items",
                        "explanation": "two",
                        "tables_used": ["items"],
                    },
                ]
            ),
        )
        default = service.ask(
            "List item names",
            AgentOptions(
                database=str(self.database),
                skills=[],
                run_id="parallel_default",
                orchestration_state_root=str(self.root / ".queryforge" / "runs"),
            ),
        )
        self.assertIsNone(default["candidate_selection"])
        enabled = service.ask(
            "List item names",
            AgentOptions(
                database=str(self.database),
                skills=[],
                run_id="parallel_enabled",
                parallel_candidates=2,
                parallel_max_preview=2,
                orchestration_state_root=str(self.root / ".queryforge" / "runs"),
            ),
        )
        self.assertIsNotNone(enabled["candidate_selection"])
        state = json.loads(
            Path(enabled["agent_team"]["state_path"]).read_text(encoding="utf-8")
        )
        self.assertIn(
            "candidate_selection",
            [artifact["artifact_type"] for artifact in state["artifacts"]],
        )


if __name__ == "__main__":
    unittest.main()
