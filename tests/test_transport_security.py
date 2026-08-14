"""Offline tests for transport hardening: API-key auth and path allowlists."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from queryforge.application import AgentOptions, AgentService
from queryforge.core.config import Config
from queryforge.interfaces.transport_security import (
    database_roots,
    report_roots,
    request_api_key_matches,
    validate_transport_options,
)

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


class TransportLLM:
    def generate_json(self, prompt: str) -> dict:
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


class TransportSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.executemany("INSERT INTO items VALUES (?)", [("a",), ("b",)])
        connection.commit()
        connection.close()
        # A second, unrelated directory tree that must never be reachable.
        self.outside_directory = tempfile.TemporaryDirectory()
        self.outside_root = Path(self.outside_directory.name)
        self.outside = self.outside_root / "secret.sqlite"
        connection = sqlite3.connect(self.outside)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('secret')")
        connection.commit()
        connection.close()
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / "runs"),
            report_output_dir=str(self.root / "reports"),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()
        self.outside_directory.cleanup()

    def service(self, config: Config | None = None) -> AgentService:
        return AgentService(
            config_loader=lambda **_: config or self.config,
            llm_factory=lambda _: TransportLLM(),
        )

    def test_api_key_match_is_disabled_without_configured_key(self):
        self.assertTrue(request_api_key_matches(self.config, None, None))

    def test_api_key_match_accepts_bearer_and_x_api_key_constant_time(self):
        config = replace(self.config, api_key="secret-token")
        self.assertTrue(
            request_api_key_matches(config, "Bearer secret-token", None)
        )
        self.assertTrue(request_api_key_matches(config, None, "secret-token"))
        self.assertFalse(request_api_key_matches(config, "Bearer wrong", None))
        self.assertFalse(request_api_key_matches(config, None, "wrong"))
        self.assertFalse(request_api_key_matches(config, None, None))

    def test_database_roots_use_file_parents_and_directory_entries(self):
        config = replace(
            self.config,
            allowed_database_paths=(str(self.database), str(self.root / "warehouse")),
        )
        roots = database_roots(config)
        self.assertIn(self.database.parent.resolve(), roots)
        self.assertIn((self.root / "warehouse").resolve(), roots)

    def test_network_entrypoint_rejects_database_outside_roots(self):
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            self.service().ask(
                "List names",
                AgentOptions(
                    database=str(self.outside),
                    skills=[],
                    entrypoint="api",
                    run_id="qf_network_block",
                    orchestration_state_root=str(self.root / "runs"),
                ),
            )

    def test_cli_entrypoint_remains_unrestricted(self):
        answer = self.service().ask(
            "List names",
            AgentOptions(
                database=str(self.outside),
                skills=[],
                entrypoint="cli",
                run_id="qf_cli_local",
                orchestration_state_root=str(self.root / "runs"),
            ),
        )
        self.assertEqual(answer["status"], "success")
        self.assertEqual(answer["rows"], [["secret"]])

    def test_configured_allowlist_replaces_fallback_roots(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        allowed_database = allowed / "data.sqlite"
        connection = sqlite3.connect(allowed_database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('in')")
        connection.commit()
        connection.close()
        config = replace(
            self.config,
            allowed_database_paths=(str(allowed),),
        )
        answer = self.service(config).ask(
            "List names",
            AgentOptions(
                database=str(allowed_database),
                skills=[],
                entrypoint="api",
                run_id="qf_allowlisted",
                orchestration_state_root=str(self.root / "runs"),
            ),
        )
        self.assertEqual(answer["rows"], [["in"]])
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            self.service(config).ask(
                "List names",
                AgentOptions(
                    database=str(self.outside),
                    skills=[],
                    entrypoint="api",
                    run_id="qf_allowlisted_block",
                    orchestration_state_root=str(self.root / "runs"),
                ),
            )

    def test_network_entrypoint_confines_report_output_dir(self):
        options = AgentOptions(
            database=str(self.database),
            skills=[],
            entrypoint="api",
            run_id="qf_report_block",
            orchestration_state_root=str(self.root / "runs"),
            report_output_dir=str(self.root / "elsewhere"),
        )
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            self.service().ask("List names", options)

    def test_report_path_rejects_roots_outside_allowlist(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            service.report_path(
                "qf_missing_report",
                report_output_dir=str(self.root / "elsewhere"),
            )

    def test_validate_transport_options_checks_auxiliary_paths(self):
        config = self.config
        outside_policy = self.outside_root / "policy.yml"
        with self.assertRaisesRegex(ValueError, "sql_policy_path"):
            validate_transport_options(
                config,
                AgentOptions(
                    database=str(self.database),
                    sql_policy_path=str(outside_policy),
                    entrypoint="mcp",
                ),
            )

    def test_report_roots_default_to_configured_output_dir(self):
        self.assertEqual(report_roots(self.config), ((self.root / "reports").resolve(),))

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_fastapi_requires_api_key_when_configured(self):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        secured = replace(self.config, api_key="app-secret")
        client = TestClient(create_app(self.service(secured)))
        self.assertEqual(client.get("/health").status_code, 200)
        blocked = client.post(
            "/ask", json={"question": "List names", "database": str(self.database)}
        )
        self.assertEqual(blocked.status_code, 401)
        allowed = client.post(
            "/ask",
            headers={"X-API-Key": "app-secret"},
            json={"question": "List names", "database": str(self.database)},
        )
        self.assertEqual(allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
