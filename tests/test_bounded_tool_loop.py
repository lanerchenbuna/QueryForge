"""Offline tests for the bounded, read-only Tool Loop."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from queryforge.workflow.node.tool_loop_node import ToolLoopNode
from queryforge.core.config import Config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import Context, SqlTask
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


class ToolLoopLLM:
    def __init__(self, actions: list[dict]) -> None:
        self.actions = list(actions)
        self.calls = 0

    def generate_json(self, prompt: str) -> dict:
        self.calls += 1
        if self.actions:
            return self.actions.pop(0)
        return {
            "action": "final_answer",
            "params": {
                "sql": "SELECT name FROM items ORDER BY name",
                "explanation": "List item names.",
                "tables_used": ["items"],
            },
        }


class BoundedToolLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT, category TEXT)")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [("alpha", "a"), ("beta", "b"), ("gamma", "a")],
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

    def database_tool(self) -> DatabaseTool:
        connector = SQLiteConnector(str(self.database))
        return DatabaseTool(connector)

    def test_four_read_only_tools_and_final_answer(self):
        llm = ToolLoopLLM(
            [
                {"action": "list_tables", "params": {}},
                {"action": "describe_table", "params": {"table_name": "items"}},
                {
                    "action": "preview_distinct_values",
                    "params": {"table_name": "items", "column_name": "category", "limit": 20},
                },
                {
                    "action": "execute_sql_preview",
                    "params": {"sql": "SELECT name FROM items", "limit": 20},
                },
                {
                    "action": "final_answer",
                    "params": {
                        "sql": "SELECT name FROM items ORDER BY name",
                        "explanation": "List item names.",
                        "tables_used": ["items"],
                    },
                },
            ]
        )
        context = self.context()
        result = ToolLoopNode(
            llm,
            self.database_tool(),
            max_rounds=5,
            timeout_seconds=30,
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.tool_loop_status, "completed")
        self.assertEqual(context.tool_loop_exit_reason, "final_answer")
        self.assertEqual(len(context.tool_loop_history), 5)
        self.assertEqual(
            context.sql_context.sql,
            "SELECT name FROM items ORDER BY name",
        )
        self.assertEqual(
            context.tool_loop_history[3]["observation"]["row_count"],
            3,
        )

    def test_max_rounds_stops_without_final_answer(self):
        llm = ToolLoopLLM(
            [
                {"action": "list_tables", "params": {}},
                {"action": "describe_table", "params": {"table_name": "items"}},
                {
                    "action": "preview_distinct_values",
                    "params": {"table_name": "items", "column_name": "category"},
                },
                {
                    "action": "execute_sql_preview",
                    "params": {"sql": "SELECT name FROM items"},
                },
            ]
        )
        context = self.context()
        result = ToolLoopNode(
            llm,
            self.database_tool(),
            max_rounds=3,
        ).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.tool_loop_status, "max_rounds")
        self.assertEqual(len(context.tool_loop_history), 3)
        self.assertIsNone(context.sql_context)

    def test_preview_rows_are_bounded_to_one_hundred(self):
        llm = ToolLoopLLM(
            [
                {
                    "action": "execute_sql_preview",
                    "params": {"sql": "SELECT name FROM items", "limit": 1000},
                },
                {"action": "final_answer", "params": {}},
            ]
        )
        context = self.context()
        result = ToolLoopNode(
            llm,
            self.database_tool(),
            max_rounds=2,
            preview_limit=100,
        ).execute(context)
        self.assertTrue(result.success)
        observation = context.tool_loop_history[0]["observation"]
        self.assertLessEqual(len(observation["rows"]), 100)
        self.assertLessEqual(observation["row_count"], 100)

    def test_invalid_action_and_timeout_are_bounded(self):
        invalid_context = self.context()
        invalid = ToolLoopNode(
            ToolLoopLLM([{"action": "drop_table", "params": {}}]),
            self.database_tool(),
        ).execute(invalid_context)
        self.assertTrue(invalid.success)
        self.assertEqual(invalid_context.tool_loop_status, "error")
        self.assertEqual(invalid_context.tool_loop_exit_reason, "invalid_action")

        timeout_context = self.context()
        timeout = ToolLoopNode(
            ToolLoopLLM([{"action": "list_tables", "params": {}}] * 5),
            self.database_tool(),
            timeout_seconds=0.000001,
        ).execute(timeout_context)
        self.assertTrue(timeout.success)
        self.assertEqual(timeout_context.tool_loop_status, "timeout")

    def test_preview_uses_policy_and_rejects_unsafe_sql(self):
        tool = self.database_tool()
        with self.assertRaises(UnsafeSQLError):
            tool.execute_sql_preview("DROP TABLE items")
        with self.assertRaises(UnsafeSQLError):
            tool.preview_distinct_values("items", "missing", 20)

    def test_service_default_is_unchanged_and_enabled_persists_trace(self):
        class ServiceLLM(ToolLoopLLM):
            def generate_json(self, prompt: str) -> dict:
                if "Choose one bounded read-only action" in prompt:
                    return super().generate_json(prompt)
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

        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )
        default_llm = ServiceLLM([])
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: default_llm,
        )
        default = service.ask(
            "List item names",
            AgentOptions(database=str(self.database), skills=[], run_id="default"),
        )
        self.assertEqual(default["tool_loop"]["status"], "disabled")
        self.assertEqual(default["tool_loop"]["rounds"], 0)

        enabled_llm = ServiceLLM(
            [
                {"action": "list_tables", "params": {}},
                {"action": "final_answer", "params": {}},
            ]
        )
        enabled_service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: enabled_llm,
        )
        enabled = enabled_service.ask(
            "List item names",
            AgentOptions(
                database=str(self.database),
                skills=[],
                run_id="enabled",
                tool_loop_enabled=True,
                tool_loop_max_rounds=2,
                orchestration_state_root=str(self.root / ".queryforge" / "runs"),
            ),
        )
        self.assertEqual(enabled["tool_loop"]["status"], "completed")
        self.assertEqual(enabled["tool_loop"]["rounds"], 2)
        state = json.loads(
            Path(enabled["agent_team"]["state_path"]).read_text(encoding="utf-8")
        )
        self.assertIn(
            "tool_loop_trace",
            [artifact["artifact_type"] for artifact in state["artifacts"]],
        )


if __name__ == "__main__":
    unittest.main()
