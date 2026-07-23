"""Offline coverage for enhanced MCP resources, prompts, tools, and sessions."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.core.config import Config
from queryforge.interfaces.mcp.server import create_mcp_server
from queryforge.application import AgentService


class MCPResourceLLM:
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


class FakeFastMCP:
    def __init__(self, name, json_response=False):
        self.name = name
        self.json_response = json_response
        self.tools = {}
        self.resources = {}
        self.prompts = {}

    def tool(self):
        return lambda function: self._register(self.tools, function.__name__, function)

    def resource(self, uri):
        return lambda function: self._register(self.resources, uri, function)

    def prompt(self, name=None):
        return lambda function: self._register(
            self.prompts, name or function.__name__, function
        )

    @staticmethod
    def _register(registry, name, function):
        registry[name] = function
        return function


class MCPEnhancedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO items VALUES (1, 'alpha')")
        connection.commit()
        connection.close()
        self.semantic = self.root / "semantic.yml"
        self.semantic.write_text(
            """version: 1
name: items
entities:
  - name: item
    table: items
    primary_key: [id]
    dimensions:
      - name: name
        column: name
metrics:
  - name: item_count
    description: Count items.
    entity: item
    aggregation: count
    expression: COUNT(items.id)
""",
            encoding="utf-8",
        )
        self.subjects = self.root / "subjects.yml"
        self.subjects.write_text(
            """subjects:
  - id: catalog
    name: Catalog
    description: Item catalog
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
            semantic_model_path=str(self.semantic),
            subject_tree_path=str(self.subjects),
            orchestration_state_root=str(self.root / ".queryforge" / "runs"),
        )
        self.service = AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: MCPResourceLLM(),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def fake_server(self):
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
            return create_mcp_server(self.service)

    def test_service_resources_reuse_schema_semantic_policy_and_sessions(self):
        self.assertEqual(self.service.list_tables(), [{
            "name": "items", "column_count": 2, "foreign_key_count": 0
        }])
        detail = self.service.describe_table("items")
        self.assertEqual(detail["table"]["table_name"], "items")
        self.assertEqual(detail["sample_rows"], [[1, "alpha"]])
        self.assertEqual(self.service.list_metrics()[0]["name"], "item_count")
        self.assertEqual(self.service.get_metric("item_count")["aggregation"], "count")
        preview = self.service.preview_sql("SELECT name FROM items", limit=1)
        self.assertEqual(preview["rows"], [["alpha"]])
        with self.assertRaises(ValueError):
            self.service.preview_sql("DROP TABLE items")
        session = self.service.new_session(session_id="mcp_session")
        self.assertEqual(session["session_id"], "mcp_session")
        self.assertEqual(self.service.reset_session("mcp_session")["status"], "reset")

    def test_mcp_registers_resources_prompts_tools_and_shared_session(self):
        server = self.fake_server()
        self.assertGreaterEqual(len(server.resources), 7)
        self.assertGreaterEqual(len(server.prompts), 4)
        self.assertGreaterEqual(len(server.tools), 11)
        self.assertIn("queryforge://tables", server.resources)
        self.assertIn("queryforge://metrics/{metric_name}", server.resources)
        self.assertIn("queryforge.analyze_data", server.prompts)
        self.assertIn("queryforge.build_report", server.prompts)
        self.assertEqual(
            server.resources["queryforge://tables"]()[0]["name"], "items"
        )
        self.assertEqual(
            server.resources["queryforge://tables/{table_name}"]("items")["sample_rows"],
            [[1, "alpha"]],
        )
        self.assertIn(
            "Analyze this data question",
            server.prompts["queryforge.analyze_data"]("List items"),
        )
        session = server.tools["new_session"]("connection_session")
        self.assertEqual(session["session_id"], "connection_session")
        result = server.tools["ask_sql"]("List item names", skills=[])
        self.assertEqual(result["session"]["session_id"], "connection_session")
        self.assertEqual(
            server.tools["reset_session"]()["session_id"], "connection_session"
        )
        review = server.tools["review_sql"]("SELECT name FROM items")
        self.assertEqual(review["status"], "success")


if __name__ == "__main__":
    unittest.main()
