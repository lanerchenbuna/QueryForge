"""Offline tests for structured reasoning summaries and SQL consistency checks."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, ReasoningResult, SqlTask
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class ReasoningLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def generate_json(self, prompt: str) -> dict:
        self.calls += 1
        return self.payload


class StructuredReasoningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT, category TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha', 'a')")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def context(self) -> Context:
        context = Context(
            task=SqlTask(
                question="List item names by category",
                database_path=str(self.database),
            )
        )
        context.relevant_tables = [
            DatabaseTool(SQLiteConnector(str(self.database))).describe_table("items")
        ]
        return context

    def test_reasoning_is_parsed_and_consistent_without_extra_call(self):
        llm = ReasoningLLM(
            {
                "sql": "SELECT category, name FROM items GROUP BY category, name ORDER BY name",
                "explanation": "List names by category.",
                "tables_used": ["items"],
                "reasoning": {
                    "goal": "List item names by category",
                    "grain": "item and category",
                    "tables": ["items"],
                    "dimensions": ["category"],
                    "sorting": [{"column": "name", "direction": "ASC"}],
                    "assumptions": ["The item table contains the requested names."],
                    "risks": [],
                    "confidence": 0.9,
                    "strategy": "direct_generation",
                },
            }
        )
        context = self.context()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(llm.calls, 1)
        self.assertIsInstance(context.reasoning_result, ReasoningResult)
        self.assertEqual(context.reasoning_result.goal, "List item names by category")
        self.assertEqual(context.reasoning_validation["status"], "valid")

    def test_reasoning_sql_mismatch_is_warning_not_blocking(self):
        llm = ReasoningLLM(
            {
                "sql": "SELECT category, name FROM items WHERE category = 'a' ORDER BY name",
                "explanation": "List names.",
                "tables_used": ["items"],
                "reasoning": {
                    "goal": "List item names",
                    "grain": "item",
                    "tables": ["items"],
                    "dimensions": [],
                    "filters": [],
                    "sorting": [],
                    "confidence": 0.7,
                    "strategy": "direct_generation",
                },
            }
        )
        context = self.context()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.reasoning_validation["status"], "warning")
        self.assertTrue(context.reasoning_validation["warnings"])

    def test_missing_reasoning_is_backward_compatible(self):
        llm = ReasoningLLM(
            {
                "sql": "SELECT name FROM items",
                "explanation": "List names.",
                "tables_used": ["items"],
            }
        )
        context = self.context()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertIsNone(context.reasoning_result)
        self.assertIsNone(context.reasoning_validation)

    def test_invalid_reasoning_does_not_break_sql_generation(self):
        llm = ReasoningLLM(
            {
                "sql": "SELECT name FROM items",
                "explanation": "List names.",
                "tables_used": ["items"],
                "reasoning": {"confidence": "not-a-number"},
            }
        )
        context = self.context()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.reasoning_validation["status"], "warning")


if __name__ == "__main__":
    unittest.main()
