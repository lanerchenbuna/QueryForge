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

    # ------------------------------------------------- /analyze allowlist (H4)

    def planner(self):
        from queryforge.application.analysis_planner import AnalysisPlannerService

        return AnalysisPlannerService(config_loader=lambda **_: self.config)

    def test_planning_service_applies_the_network_allowlist_before_opening_files(self):
        planner = self.planner()
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            planner.analyze(
                "How many items are there?",
                database=str(self.outside),
                entrypoint="api",
            )
        # The allowlist is consulted *before* the path is opened: a nonexistent
        # outside file is refused for being outside, not for being missing.
        missing = self.outside_root / "missing.sqlite"
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            planner.analyze(
                "How many items are there?",
                database=str(missing),
                entrypoint="api",
            )
        # The semantic model and the SQL policy are caller-supplied paths too.
        with self.assertRaisesRegex(ValueError, "semantic_model_path"):
            planner.analyze(
                "How many items are there?",
                database=str(self.database),
                semantic_model_path=str(self.outside_root / "model.yml"),
                entrypoint="api",
            )
        with self.assertRaisesRegex(ValueError, "sql_policy_path"):
            planner.analyze(
                "How many items are there?",
                database=str(self.database),
                sql_policy_path=str(self.outside_root / "policy.yml"),
                entrypoint="api",
            )

    def test_api_analysis_schema_is_held_to_the_network_allowlist(self):
        """The API request schema names its transport, so the planner refuses it."""
        from queryforge.interfaces.api.schemas import AnalyzeRequest

        request = AnalyzeRequest(
            question="How many items are there?", database=str(self.outside)
        )
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            self.planner().analyze(request.question, **request.to_kwargs())

    def test_local_analysis_without_an_entrypoint_keeps_todays_behaviour(self):
        payload = self.planner().analyze(
            "How many items are there?", database=str(self.outside)
        )
        # No entrypoint means a local caller: it reaches the planner (and asks for
        # a governed metric) instead of being refused by the transport allowlist.
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["stop_reason"], "no_governed_metric_match")

    # ------------------------------------------- /ask vs /ask/stream contract (M6)

    def stream_service(self, config: Config | None = None) -> AgentService:
        return AgentService(
            config_loader=lambda **_: config or self.config,
            llm_factory=lambda _: TransportLLM(),
        )

    def test_stream_refuses_before_starting_a_worker(self):
        """M6: the stream entry point validates synchronously, like ``ask``."""
        with self.assertRaisesRegex(ValueError, "outside the allowed transport paths"):
            self.stream_service().stream(
                "List names",
                AgentOptions(
                    database=str(self.outside),
                    skills=[],
                    entrypoint="api_stream",
                    orchestration_state_root=str(self.root / "runs"),
                ),
            )
        with self.assertRaisesRegex(ValueError, "SQLite database does not exist"):
            self.stream_service().stream(
                "List names",
                AgentOptions(
                    database=str(self.root / "missing.sqlite"),
                    skills=[],
                    entrypoint="api_stream",
                    orchestration_state_root=str(self.root / "runs"),
                ),
            )

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_stream_route_refuses_exactly_what_ask_refuses(self):
        """M6: /ask/stream answered 200 + a failed terminal event for a 400 request."""
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        restricted = replace(
            self.config, allowed_database_paths=(str(self.root),)
        )
        client = TestClient(create_app(self.stream_service(restricted)))
        cases = {
            "database outside the transport allowlist": {
                "database": str(self.outside)
            },
            "database does not exist": {
                "database": str(self.root / "missing.sqlite")
            },
            "invalid run options": {"tool_loop_max_rounds": 99},
            "unknown data domain": {"domain_id": "unknown_domain"},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                body = {"question": "List names", "skills": [], **overrides}
                ask = client.post("/ask", json=body)
                stream = client.post("/ask/stream", json=body)
                self.assertEqual(ask.status_code, 400, ask.text)
                self.assertEqual(stream.status_code, 400, stream.text)
                # Same refusal, same reason: the two routes cannot disagree.
                self.assertEqual(stream.json()["detail"], ask.json()["detail"])
                self.assertNotIn("event-stream", stream.headers.get("content-type", ""))

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_stream_route_applies_the_semantic_gate_before_streaming(self):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        gated = replace(self.config, require_semantic_model=True)
        client = TestClient(create_app(self.stream_service(gated)))
        body = {"question": "List names", "database": str(self.database), "skills": []}
        ask = client.post("/ask", json=body)
        stream = client.post("/ask/stream", json=body)
        self.assertEqual(ask.status_code, 400, ask.text)
        self.assertEqual(stream.status_code, 400, stream.text)
        self.assertIn("semantic layer is required", stream.json()["detail"])
        self.assertEqual(stream.json()["detail"], ask.json()["detail"])

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_a_valid_stream_request_still_streams(self):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        client = TestClient(create_app(self.stream_service()))
        response = client.post(
            "/ask/stream",
            json={"question": "List names", "database": str(self.database), "skills": []},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"].split(";")[0], "text/event-stream"
        )
        self.assertIn('"event_type":"run_started"', response.text)
        self.assertIn('"event_type":"final_result"', response.text)

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

    @unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
    def test_unloadable_config_fails_closed_instead_of_serving_anonymously(self):
        """M4: a broken config used to disable the API-key gate entirely."""
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        def broken_loader(**_):
            raise RuntimeError("models.yml is unreadable")

        service = AgentService(
            config_loader=broken_loader, llm_factory=lambda _: TransportLLM()
        )
        client = TestClient(create_app(service))

        # The public liveness route keeps working, so a deployment can still be
        # probed while its configuration is broken.
        self.assertEqual(client.get("/health").status_code, 200)
        # Everything else is refused: whether this deployment requires an API key
        # is unknown, so serving the route anonymously is not an option.
        for method, path, body in (
            ("get", "/skills", None),
            ("get", "/models", None),
            ("post", "/ask", {"question": "List names", "database": str(self.database)}),
            ("get", "/report/qf_missing", None),
        ):
            response = getattr(client, method)(path, json=body) if body else getattr(
                client, method
            )(path)
            self.assertEqual(response.status_code, 503, path)
            self.assertIn("configuration", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
