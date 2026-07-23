import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import main as cli

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from starlette.exceptions import StarletteDeprecationWarning

    warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)
    from fastapi.testclient import TestClient
else:  # pragma: no cover - exercised by a clean base-only installation
    TestClient = None

from queryforge.interfaces.api.app import APIUnavailableError, create_app
from queryforge import __version__
from queryforge.core.config import Config
from queryforge.interfaces.gateway import GatewayAdapter
from queryforge.interfaces.mcp.server import MCPUnavailableError, create_mcp_server
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.storage import SQLHistoryStore


class ServiceLLM:
    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "No optional skill."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The result answers the question.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List names.",
            "tables_used": ["items"],
        }


class StubService:
    def __init__(self):
        self.ask_calls = []
        self.plan_calls = []

    def health(self):
        return {"status": "ok", "service": "QueryForge"}

    def list_models(self):
        return [{"name": "qwen", "model": "qwen-plus", "active": True}]

    def list_skills(self):
        return [{"name": "sql_best_practices", "enabled": True}]

    def ask(self, question, options=None):
        self.ask_calls.append((question, options))
        return {
            "status": "success",
            "run_id": "qf_stub",
            "question": question,
            "sql": "SELECT name FROM items",
            "explanation": "List names.",
            "columns": ["name"],
            "rows": [["alpha"], ["beta"]],
            "row_count": 2,
        }

    def plan(self, question, options=None):
        self.plan_calls.append((question, options))
        return {
            "status": "planned",
            "run_id": "qf_plan",
            "plan": {"sql": "SELECT name FROM items", "executed": False},
        }

    def get_history(self, limit=20):
        return {"entries": [], "limit": limit}


class ServiceAPIGatewayMCPTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.executemany("INSERT INTO items VALUES (?)", [("a",), ("b",)])
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

    def tearDown(self):
        self.directory.cleanup()

    def config_loader(self, **kwargs):
        return self.config

    def test_agent_service_ask_uses_workflow_and_plan_does_not_execute(self):
        service = AgentService(
            config_loader=self.config_loader,
            llm_factory=lambda _: ServiceLLM(),
        )
        answer = service.ask(
            "List item names",
            AgentOptions(database=str(self.database), skills=[], run_id="qf_service"),
        )
        self.assertEqual(answer["rows"], [["a"], ["b"]])
        self.assertEqual(answer["run_id"], "qf_service")
        self.assertEqual(answer["sql_security"]["name"], "default_sql_security")
        self.assertTrue(answer["sql_security"]["decisions"][0]["allowed"])
        self.assertEqual(answer["sql_security"]["decisions"][0]["rule"], "allow")
        history_count = len(SQLHistoryStore(self.config.history_db_path).list_entries())

        plan = service.plan(
            "List item names",
            AgentOptions(database=str(self.database), skills=[], run_id="qf_plan"),
        )
        self.assertEqual(plan["status"], "planned")
        self.assertFalse(plan["plan"]["executed"])
        self.assertIsNone(plan["plan"]["approved"])
        self.assertNotIn("rows", plan)
        self.assertEqual(
            len(SQLHistoryStore(self.config.history_db_path).list_entries()),
            history_count,
        )

    def test_service_requires_semantic_layer_with_explicit_diagnostic_escape_hatch(self):
        strict = replace(self.config, require_semantic_model=True)
        service = AgentService(
            config_loader=lambda **_: strict,
            llm_factory=lambda _: ServiceLLM(),
        )
        with self.assertRaisesRegex(ValueError, "semantic layer is required"):
            service.ask(
                "List item names",
                AgentOptions(database=str(self.database), skills=[]),
            )
        answer = service.ask(
            "List item names",
            AgentOptions(
                database=str(self.database),
                skills=[],
                allow_schema_only=True,
            ),
        )
        self.assertEqual(answer["rows"], [["a"], ["b"]])

    def test_cli_passes_sql_policy_to_shared_agent_service(self):
        output = {
            "status": "success",
            "run_id": "qf_cli_policy",
            "model_provider": "openai",
            "model": "offline",
            "rows": [],
        }
        with patch(
            "sys.argv",
            [
                "queryforge",
                "--question",
                "List names",
                "--sql-policy",
                "analyst.yml",
            ],
        ), patch.object(cli.AgentService, "ask", return_value=output) as ask, redirect_stdout(
            StringIO()
        ), redirect_stderr(StringIO()):
            code = cli.main()
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_args.args[1].sql_policy_path, "analyst.yml")

    def test_service_health_models_skills_and_history(self):
        service = AgentService(config_loader=self.config_loader)
        self.assertEqual(service.health()["status"], "ok")
        self.assertTrue(any(model["name"] == "qwen" for model in service.list_models()))
        self.assertTrue(
            any(skill["name"] == "sql_best_practices" for skill in service.list_skills())
        )
        self.assertEqual(service.get_history(5)["entries"], [])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_fastapi_routes_delegate_to_service(self):
        service = StubService()
        client = TestClient(create_app(service))
        self.assertEqual(client.get("/health").json()["status"], "ok")
        self.assertEqual(client.get("/models").json()[0]["name"], "qwen")
        self.assertEqual(
            client.get("/skills").json()[0]["name"], "sql_best_practices"
        )

        response = client.post(
            "/ask",
            json={
                "question": "List names",
                "database": "items.sqlite",
                "semantic_model_path": "items.semantic.yml",
                "allow_schema_only": True,
                "subject_tree_enabled": True,
                "subject_tree_path": "items.subjects.yml",
                "subject": "inventory",
                "sql_policy_path": "items.policy.yml",
                "model_provider": "qwen",
                "model": "qwen-plus",
                "skills": ["sql_best_practices"],
                "visualize": True,
                "report": True,
                "report_output_dir": "reports",
                "session_id": "api_session",
                "reset_session": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [["alpha"], ["beta"]])
        options = service.ask_calls[-1][1]
        self.assertEqual(options.model_provider, "qwen")
        self.assertEqual(options.semantic_model_path, "items.semantic.yml")
        self.assertTrue(options.allow_schema_only)
        self.assertTrue(options.subject_tree_enabled)
        self.assertEqual(options.subject_tree_path, "items.subjects.yml")
        self.assertEqual(options.subject, "inventory")
        self.assertEqual(options.sql_policy_path, "items.policy.yml")
        self.assertTrue(options.visualize)
        self.assertTrue(options.report)
        self.assertEqual(options.report_output_dir, "reports")
        self.assertEqual(options.session_id, "api_session")
        self.assertTrue(options.reset_session)

        plan = client.post("/plan", json={"question": "List names"})
        self.assertEqual(plan.status_code, 200)
        self.assertEqual(plan.json()["status"], "planned")

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_fastapi_openapi_uses_package_version(self):
        client = TestClient(create_app(StubService()))
        self.assertEqual(client.get("/openapi.json").json()["info"]["version"], __version__)

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_real_api_returns_named_security_error_before_execution(self):
        policy = self.root / "strict.yml"
        policy.write_text(
            """version: 1
name: api_strict
allowed_tables: [items]
allowed_columns:
  items: [name]
require_limit: true
max_limit: 1
""",
            encoding="utf-8",
        )
        service = AgentService(
            config_loader=self.config_loader,
            llm_factory=lambda _: ServiceLLM(),
        )
        response = TestClient(create_app(service)).post(
            "/ask",
            json={
                "question": "List item names",
                "database": str(self.database),
                "sql_policy_path": str(policy),
                "skills": [],
            },
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertIn("SQL_SECURITY_ERROR", detail)
        self.assertIn("rule=unbounded_result", detail)
        self.assertRegex(detail, r"run_id=qf_[0-9a-f]{32}")

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_real_agent_service_through_api_returns_sql_rows_and_plan(self):
        service = AgentService(
            config_loader=self.config_loader,
            llm_factory=lambda _: ServiceLLM(),
        )
        client = TestClient(create_app(service))
        ask = client.post(
            "/ask",
            json={
                "question": "List item names",
                "database": str(self.database),
                "skills": [],
            },
        )
        self.assertEqual(ask.status_code, 200)
        self.assertEqual(ask.json()["sql"], "SELECT name FROM items ORDER BY name")
        self.assertEqual(ask.json()["rows"], [["a"], ["b"]])
        self.assertRegex(ask.json()["run_id"], r"^qf_[0-9a-f]{32}$")

        plan = client.post(
            "/plan",
            json={
                "question": "List item names",
                "database": str(self.database),
                "skills": [],
            },
        )
        self.assertEqual(plan.status_code, 200)
        self.assertEqual(plan.json()["status"], "planned")
        self.assertFalse(plan.json()["plan"]["executed"])
        self.assertRegex(plan.json()["run_id"], r"^qf_[0-9a-f]{32}$")

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_gateway_route_and_adapter_return_preview(self):
        service = StubService()
        adapter_output = GatewayAdapter(service, preview_rows=1).handle(
            user_id="u-1", channel="demo", text="List names"
        )
        self.assertEqual(adapter_output["rows_preview"], [["alpha"]])
        self.assertIn("Returned 2 row(s)", adapter_output["text"])

        client = TestClient(create_app(service))
        response = client.post(
            "/gateway/webhook",
            json={"user_id": "u-1", "channel": "slack-demo", "text": "List names"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sql"], "SELECT name FROM items")
        self.assertEqual(response.json()["row_count"], 2)

    def test_mcp_missing_sdk_has_clear_install_hint(self):
        with patch.dict(sys.modules, {"mcp": None}):
            with self.assertRaisesRegex(MCPUnavailableError, "requirements-mcp.txt"):
                create_mcp_server(StubService())

    def test_fastapi_missing_dependency_has_clear_install_hint(self):
        with patch.dict(sys.modules, {"fastapi": None}):
            with self.assertRaisesRegex(APIUnavailableError, "requirements-server.txt"):
                create_app(StubService())

    def test_mcp_registers_subject_tools_over_same_service(self):
        class FakeFastMCP:
            def __init__(self, name, json_response=False):
                self.name = name
                self.json_response = json_response
                self.tools = {}
                self.resources = {}
                self.prompts = {}

            def tool(self):
                def decorator(function):
                    self.tools[function.__name__] = function
                    return function

                return decorator

            def resource(self, uri):
                def decorator(function):
                    self.resources[uri] = function
                    return function

                return decorator

            def prompt(self, name=None):
                def decorator(function):
                    self.prompts[name or function.__name__] = function
                    return function

                return decorator

        mcp_module = types.ModuleType("mcp")
        mcp_module.__path__ = []
        server_module = types.ModuleType("mcp.server")
        server_module.__path__ = []
        fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        fastmcp_module.FastMCP = FakeFastMCP
        service = StubService()
        with patch.dict(
            sys.modules,
            {
                "mcp": mcp_module,
                "mcp.server": server_module,
                "mcp.server.fastmcp": fastmcp_module,
            },
        ):
            server = create_mcp_server(service)
        self.assertEqual(
            set(server.tools),
            {
                "ask_sql",
                "list_models",
                "list_skills",
                "list_subjects",
                "get_history",
                "list_tables",
                "describe_table",
                "list_metrics",
                "preview_sql",
                "review_sql",
                "new_session",
                "reset_session",
            },
        )
        result = server.tools["ask_sql"](
            "List names",
            sql_policy_path="items.policy.yml",
            subject_tree_enabled=True,
            subject_tree_path="items.subjects.yml",
            subject="inventory",
            session_id="mcp_session",
            reset_session=True,
        )
        self.assertEqual(result["sql"], "SELECT name FROM items")
        self.assertEqual(
            service.ask_calls[-1][1].sql_policy_path, "items.policy.yml"
        )
        self.assertTrue(service.ask_calls[-1][1].subject_tree_enabled)
        self.assertEqual(
            service.ask_calls[-1][1].subject_tree_path, "items.subjects.yml"
        )
        self.assertEqual(service.ask_calls[-1][1].subject, "inventory")
        self.assertEqual(service.ask_calls[-1][1].session_id, "mcp_session")
        self.assertTrue(service.ask_calls[-1][1].reset_session)
        self.assertEqual(server.tools["get_history"](3)["limit"], 3)


if __name__ == "__main__":
    unittest.main()
