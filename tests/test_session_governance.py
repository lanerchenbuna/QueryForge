"""M7: conversation-memory governance must be reachable and actually used.

Stage 13's session lifecycle (retention/expiry, scoped deletion, export,
user-scoped preferences, definition-version invalidation) shipped inside
``SessionStore`` with no caller: no REST route and no CLI command reached any of
it, and nothing ever recorded the definition versions that
``SessionStore.invalidate_version`` matches against. These tests pin the wiring
end to end: service facade, REST routes, CLI commands, and the write side.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import main as cli

from queryforge.application import AgentOptions, AgentService
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, SqlTask, VectorMatch
from queryforge.domain.knowledge import (
    GlossaryEntry,
    KnowledgeSource,
    StructuredKnowledgeBase,
    content_hash,
)
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.storage.knowledge_base import KnowledgeBaseBuilder
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.orchestration.agents.entry_router import EntryRouterAgent
from queryforge.orchestration.orchestrator.orchestrator import OrchestratorAgent
from queryforge.orchestration.runtime.session_store import SessionStore
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

SESSION_MODEL = """
version: 1
name: session_fixture
description: Session governance fixture.
entities:
- name: items
  table: items
  description: One row per item.
  entity_type: fact
  primary_key: [item_id]
  grain: [item_id]
  dimensions:
  - name: category
    column: category
    description: Item category.
metrics:
- name: item_count
  description: Number of items.
  entity: items
  aggregation: count
  expression: COUNT(items.item_id)
  synonyms: [items, item count]
  allowed_dimensions: [items.category]
"""


class SessionLLM:
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
            "sql": "SELECT COUNT(item_id) AS item_count FROM items",
            "explanation": "Count the items.",
            "tables_used": ["items"],
        }


class SessionGovernanceTest(unittest.TestCase):
    """Shared fixture: a governed database, a semantic model, and a service."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE items (item_id INTEGER PRIMARY KEY, category TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, ?, ?)",
            [(1, "a", 10.0), (2, "b", 20.0)],
        )
        connection.commit()
        connection.close()
        self.semantic_model = self.root / "semantic_model.yml"
        self.semantic_model.write_text(SESSION_MODEL, encoding="utf-8")
        self.state_root = self.root / "runs"
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            semantic_model_path=str(self.semantic_model),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.state_root),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def service(self, config: Config | None = None) -> AgentService:
        return AgentService(
            config_loader=lambda **_: config or self.config,
            llm_factory=lambda _: SessionLLM(),
        )

    def store(self) -> SessionStore:
        return SessionStore(self.root / "sessions")

    def seed_session(self, session_id: str = "seeded") -> SessionStore:
        store = self.store()
        memory = store.create(session_id)
        store.save(memory)
        return store


class SessionGovernanceServiceTest(SessionGovernanceTest):
    def test_lifecycle_operations_are_reachable_from_the_service(self):
        service = self.service()
        self.seed_session("lifecycle")
        self.assertEqual(service.list_sessions()["sessions"], ["lifecycle"])

        status = service.session_status("lifecycle")
        self.assertTrue(status["found"])
        self.assertEqual(status["retained_turns"], 0)
        self.assertEqual(status["preferences"], [])
        self.assertFalse(service.session_status("missing")["found"])

        exported = service.export_session("lifecycle")
        self.assertTrue(exported["found"])
        self.assertEqual(exported["session_id"], "lifecycle")

        expired = service.expire_sessions()
        self.assertEqual(expired["sessions"], 1)
        self.assertEqual(expired["expired_turns"], 0)

        deleted = service.delete_session("lifecycle")
        self.assertEqual(deleted["status"], "deleted")
        self.assertFalse(service.session_status("lifecycle")["found"])
        self.assertEqual(service.list_sessions()["sessions"], [])

    def test_preferences_are_user_scoped_and_revocable_through_the_service(self):
        service = self.service()
        self.seed_session("prefs")
        stored = service.set_session_preference(
            "prefs", user_id="user-a", name="format", value="long"
        )
        self.assertEqual(stored["preference"]["user_id"], "user-a")
        self.assertEqual(
            service.session_preferences("prefs", user_id="user-a")["count"], 1
        )
        # Another user sees nothing, and cannot revoke what is not theirs.
        self.assertEqual(
            service.session_preferences("prefs", user_id="user-b")["count"], 0
        )
        self.assertFalse(
            service.revoke_session_preference(
                "prefs", "format", user_id="user-b"
            )["revoked"]
        )
        self.assertTrue(
            service.revoke_session_preference(
                "prefs", "format", user_id="user-a"
            )["revoked"]
        )
        self.assertEqual(service.session_preferences("prefs")["count"], 0)

    def test_expiry_drops_only_turns_outside_the_retention_window(self):
        from queryforge.orchestration.schemas.session import SessionTurn

        store = self.seed_session("retained")
        memory = store.load("retained")
        memory.history = [
            SessionTurn(
                turn_number=1,
                question="old question",
                status="success",
                created_at="2020-01-01T00:00:00+00:00",
            ),
            SessionTurn(
                turn_number=2,
                question="recent question",
                status="success",
            ),
        ]
        memory.turn_count = 2
        store.save(memory)

        summary = self.service().expire_sessions(session_id="retained")
        self.assertEqual(summary["status"], "expired")
        self.assertEqual(summary["expired_turns"], 1)
        remaining = self.service().session_status("retained")
        self.assertEqual(
            [turn["question"] for turn in remaining["turns"]], ["recent question"]
        )

    def test_deleting_a_turn_range_keeps_the_rest(self):
        store = self.seed_session("partial")
        from queryforge.orchestration.schemas.session import SessionTurn

        memory = store.load("partial")
        memory.history = [
            SessionTurn(turn_number=index, question=f"q{index}", status="success")
            for index in (1, 2, 3)
        ]
        memory.turn_count = 3
        store.save(memory)

        deleted = self.service().delete_session("partial", turn_range=(2, 2))
        self.assertEqual(deleted["status"], "deleted_turns")
        self.assertEqual(deleted["deleted_turns"], 1)
        self.assertEqual(
            [turn["turn_number"] for turn in self.service().session_status("partial")["turns"]],
            [1, 3],
        )


class SessionKnowledgeVersionTest(SessionGovernanceTest):
    """The write side: a run must record the definitions it relied on."""

    def ask(self, session_id: str, run_id: str) -> dict:
        return self.service().ask(
            "How many items are there?",
            AgentOptions(
                database=str(self.database),
                skills=[],
                session_id=session_id,
                run_id=run_id,
                orchestration_state_root=str(self.state_root),
            ),
        )

    def test_a_run_records_its_metric_and_model_versions(self):
        output = self.ask("writer", "qf_writer")
        self.assertEqual(output["status"], "success")
        recorded = output["session"]["knowledge_versions"]
        self.assertTrue(
            any(reference.startswith("metric:item_count@") for reference in recorded),
            recorded,
        )
        self.assertTrue(
            any(reference.startswith("model:session_fixture@") for reference in recorded),
            recorded,
        )
        status = self.service().session_status("writer")
        self.assertEqual(status["turns"][-1]["knowledge_versions"], recorded)
        self.assertEqual(status["invalidated_turns"], 0)

    def test_invalidate_version_marks_exactly_the_turns_that_used_it(self):
        self.ask("invalidation", "qf_invalidation")
        service = self.service()
        before = service.session_status("invalidation")["turns"][-1]
        self.assertTrue(before["knowledge_versions"])

        # The metric id alone is enough: `KnowledgeVersionRef.matches` also accepts
        # the bare id, which is what an operator has at hand.
        result = service.invalidate_session_knowledge_version(
            "item_count", reason="metric formula changed"
        )
        self.assertEqual(result["turns"], 1)
        self.assertEqual(result["affected"], [{"session_id": "invalidation", "turns": 1}])

        after = service.session_status("invalidation")["turns"][-1]
        self.assertTrue(after["invalidated"])
        self.assertEqual(after["invalidated_reason"], "metric formula changed")
        # An unrelated version leaves the turn alone.
        self.assertEqual(service.invalidate_session_knowledge_version("other")["turns"], 0)

    def test_invalidation_can_be_scoped_to_one_session(self):
        self.ask("scoped-a", "qf_scoped_a")
        self.ask("scoped-b", "qf_scoped_b")
        service = self.service()
        result = service.invalidate_session_knowledge_version(
            "item_count", session_id="scoped-a"
        )
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(
            service.session_status("scoped-b")["invalidated_turns"], 0
        )


@unittest.skipUnless(FASTAPI_AVAILABLE, "optional FastAPI dependencies not installed")
class SessionGovernanceRouteTest(SessionGovernanceTest):
    def client(self, config: Config | None = None):
        from fastapi.testclient import TestClient

        from queryforge.interfaces.api.app import create_app

        return TestClient(create_app(self.service(config)))

    def test_the_whole_lifecycle_is_reachable_over_rest(self):
        client = self.client()
        created = client.post(
            "/sessions/lifecycle/preferences",
            json={"user_id": "user-a", "name": "format", "value": "long"},
        )
        self.assertEqual(created.status_code, 200)

        status = client.get("/sessions/lifecycle")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["found"])
        self.assertEqual(len(status.json()["preferences"]), 1)

        listed = client.get(
            "/sessions/lifecycle/preferences", params={"user_id": "user-a"}
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["count"], 1)

        revoked = client.delete(
            "/sessions/lifecycle/preferences/format", params={"user_id": "user-a"}
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertTrue(revoked.json()["revoked"])

        exported = client.get("/sessions/lifecycle/export")
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.json()["found"])

        expired = client.post("/sessions/expire", json={})
        self.assertEqual(expired.status_code, 200)
        self.assertEqual(expired.json()["sessions"], 1)

        invalidated = client.post(
            "/sessions/invalidate-version",
            json={"version_ref": "item_count", "session_id": "lifecycle"},
        )
        self.assertEqual(invalidated.status_code, 200)
        self.assertEqual(invalidated.json()["version_ref"], "item_count")

        deleted = client.delete("/sessions/lifecycle")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["status"], "deleted")
        # The session is gone, and the read routes say so instead of pretending.
        self.assertEqual(client.get("/sessions/lifecycle").status_code, 404)
        self.assertEqual(client.get("/sessions/lifecycle/export").status_code, 404)
        self.assertEqual(client.delete("/sessions/lifecycle").status_code, 404)

    def test_turn_range_deletion_requires_both_bounds(self):
        client = self.client()
        self.seed_session("ranged")
        incomplete = client.delete("/sessions/ranged", params={"turn_start": 1})
        self.assertEqual(incomplete.status_code, 400)
        complete = client.delete(
            "/sessions/ranged", params={"turn_start": 1, "turn_end": 1}
        )
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.json()["status"], "unchanged")

    def test_session_routes_require_the_transport_api_key(self):
        secured = replace(self.config, api_key="session-secret")
        client = self.client(secured)
        self.seed_session("secured")
        self.assertEqual(client.get("/sessions/secured").status_code, 401)
        self.assertEqual(client.get("/sessions/secured/export").status_code, 401)
        self.assertEqual(client.delete("/sessions/secured").status_code, 401)
        self.assertEqual(client.post("/sessions/expire", json={}).status_code, 401)
        self.assertEqual(
            client.post(
                "/sessions/secured/preferences",
                json={"user_id": "user-a", "name": "format", "value": "long"},
            ).status_code,
            401,
        )
        self.assertEqual(
            client.post(
                "/sessions/invalidate-version", json={"version_ref": "item_count"}
            ).status_code,
            401,
        )
        allowed = client.get(
            "/sessions/secured", headers={"X-API-Key": "session-secret"}
        )
        self.assertEqual(allowed.status_code, 200)

    def test_unknown_session_is_not_found_not_a_crash(self):
        client = self.client()
        self.assertEqual(client.get("/sessions/nope").status_code, 404)
        self.assertEqual(client.delete("/sessions/nope").status_code, 404)
        # Expiring an unknown session is a reported no-op, not an error.
        expired = client.post("/sessions/expire", json={"session_id": "nope"})
        self.assertEqual(expired.status_code, 200)
        self.assertEqual(expired.json()["status"], "not_found")


class SessionGovernanceCliTest(SessionGovernanceTest):
    """The same capabilities from the operator surface."""

    def run_cli(self, *argv: str) -> tuple[int, dict | None, str]:
        with patch.object(sys, "argv", ["queryforge", *argv]), patch.dict(
            os.environ, {"ORCHESTRATION_STATE_ROOT": str(self.state_root)}
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main()
        payload = None
        if stdout.getvalue().strip():
            payload = json.loads(stdout.getvalue())
        return code, payload, stderr.getvalue()

    def seed_with_turns(self, session_id: str = "cli-session") -> None:
        from queryforge.orchestration.schemas.session import (
            KnowledgeVersionRef,
            SessionTurn,
            UserPreference,
        )

        store = self.store()
        memory = store.create(session_id)
        memory.user_id = "user-a"
        memory.turn_count = 2
        memory.history = [
            SessionTurn(
                turn_number=1,
                question="how many items?",
                status="success",
                created_at="2020-01-01T00:00:00+00:00",
                knowledge_versions=[
                    KnowledgeVersionRef(kind="metric", id="item_count", version="aaaa")
                ],
            ),
            SessionTurn(
                turn_number=2,
                question="and by category?",
                status="success",
                knowledge_versions=[
                    KnowledgeVersionRef(kind="metric", id="item_count", version="bbbb")
                ],
            ),
        ]
        # Persist the seeded turns first: `set_preference` loads the stored session
        # and writes it back with the preference appended, so the order matters.
        store.save(memory)
        store.set_preference(
            session_id,
            UserPreference(user_id="user-a", name="format", value="long"),
        )

    def test_listing_status_and_export(self):
        self.seed_with_turns()
        code, payload, _ = self.run_cli("--sessions")
        self.assertEqual(code, 0)
        self.assertEqual(payload["sessions"], ["cli-session"])

        code, status, _ = self.run_cli("--session-status", "cli-session")
        self.assertEqual(code, 0)
        self.assertEqual(status["turn_count"], 2)
        self.assertEqual(len(status["preferences"]), 1)
        self.assertEqual(status["invalidated_turns"], 0)

        code, exported, _ = self.run_cli("--session-export", "cli-session")
        self.assertEqual(code, 0)
        self.assertTrue(exported["found"])
        self.assertEqual(exported["turn_count"], 2)
        # Result rows are never stored, so the export cannot contain them.
        self.assertNotIn("rows", json.dumps(exported))

    def test_expire_delete_and_preference_commands(self):
        self.seed_with_turns()
        code, expired, _ = self.run_cli("--session-expire")
        self.assertEqual(code, 0)
        self.assertEqual(expired["expired_turns"], 1)

        code, revoked, _ = self.run_cli(
            "--session-revoke-preference",
            "format",
            "--session-id",
            "cli-session",
            "--user-id",
            "user-a",
        )
        self.assertEqual(code, 0)
        self.assertTrue(revoked["revoked"])

        code, stored, _ = self.run_cli(
            "--session-set-preference",
            "grain",
            "monthly",
            "--session-id",
            "cli-session",
            "--user-id",
            "user-a",
        )
        self.assertEqual(code, 0)
        self.assertEqual(stored["preference"]["name"], "grain")

        code, deleted, _ = self.run_cli("--session-delete", "cli-session")
        self.assertEqual(code, 0)
        self.assertEqual(deleted["status"], "deleted")

    def test_deleting_a_turn_range(self):
        self.seed_with_turns()
        code, deleted, _ = self.run_cli(
            "--session-delete", "cli-session", "--session-turn-range", "2-2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(deleted["deleted_turns"], 1)
        _, status, _ = self.run_cli("--session-status", "cli-session")
        self.assertEqual([turn["turn_number"] for turn in status["turns"]], [1])

    def test_invalidating_a_superseded_definition_version(self):
        self.seed_with_turns()
        code, invalidated, _ = self.run_cli(
            "--invalidate-knowledge-version",
            "item_count",
            "--invalidate-reason",
            "metric formula changed",
        )
        self.assertEqual(code, 0)
        self.assertEqual(invalidated["turns"], 2)
        _, status, _ = self.run_cli("--session-status", "cli-session")
        self.assertEqual(status["invalidated_turns"], 2)
        self.assertEqual(
            {turn["invalidated_reason"] for turn in status["turns"]},
            {"metric formula changed"},
        )

    def test_companion_flags_without_their_action_are_usage_errors(self):
        for argv, expected in (
            (("--session-turn-range", "1-2"), "--session-turn-range requires"),
            (("--session-expire-before", "2020-01-01T00:00:00+00:00"), "--session-expire-before requires"),
            (("--session-revoke-preference", "format"), "--session-revoke-preference requires --session-id"),
            (("--invalidate-reason", "why"), "--invalidate-reason requires"),
        ):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(expected, stderr)

    def test_a_malformed_turn_range_is_a_usage_error_not_a_silent_noop(self):
        self.seed_with_turns()
        code, _, stderr = self.run_cli(
            "--session-delete", "cli-session", "--session-turn-range", "two-four"
        )
        self.assertEqual(code, 1)
        self.assertIn("START-END", stderr)
        # Nothing was deleted by the failed command.
        _, status, _ = self.run_cli("--session-status", "cli-session")
        self.assertEqual(len(status["turns"]), 2)


class _HookDrivenRunner:
    """Runner stand-in that drives the orchestrator's hooks with a prepared Context.

    The production runner builds its context from the database and the model
    pipeline; these tests need the same hook contract without a model call, so the
    runner hands the orchestrator the workflow context the test built and returns
    one completed result. Both legs of the session write path stay real: the
    orchestrator records the turn and ``AgentService`` then annotates it.
    """

    def __init__(self, config, *, context_factory, **kwargs) -> None:
        self.analysis_hook = kwargs["analysis_hook"]
        self.run_id_factory = kwargs["run_id_factory"]
        self.context_factory = context_factory

    def run(self, task) -> dict:
        context = self.context_factory(task)
        self.analysis_hook(context)
        return {
            "status": "success",
            "run_id": self.run_id_factory(),
            "question": task.question,
            "sql": SessionVersionWriterTest.SQL,
            "explanation": "Count the items.",
            "columns": ["item_count"],
            "rows": [[2]],
            "row_count": 1,
        }


class SessionVersionWriterTest(SessionGovernanceTest):
    """The writer side of step 17's open item 八.2.

    Stage 13 could invalidate the turns that used a superseded definition, but only
    ``AgentService`` recorded the versions, and only *after*
    ``OrchestratorAgent._record_session_turn`` had written the turn. A session
    written by any other entry point therefore recorded nothing, and the
    ``glossary``/``knowledge`` reference kinds had no producer in the product, so
    ``invalidate_version("glossary:...")`` could never match. These tests drive the
    orchestrator alone first, then the whole service path, and pin that each kind
    of reference marks exactly the turns that used it.
    """

    QUESTION = "How many items are there?"
    SQL = "SELECT COUNT(item_id) AS item_count FROM items"

    # --------------------------------------------------------------- fixtures
    def workflow_context(
        self,
        *,
        question: str | None = None,
        matches: tuple = (),
        semantic_model: bool = True,
    ) -> Context:
        """A real workflow context: loaded semantic model plus retrieved knowledge."""
        question = question or self.QUESTION
        context = Context(
            task=SqlTask(question=question, database_path=str(self.database))
        )
        if semantic_model:
            with SQLiteConnector(str(self.database)) as connector:
                tool = DatabaseTool(connector)
                schemas = [tool.describe_table(name) for name in tool.list_tables()]
            context.semantic_model = SemanticModelLoader.load_and_validate(
                str(self.semantic_model), schemas, question
            )
        context.vector_schema_matches = list(matches)
        return context

    @staticmethod
    def retrieved_knowledge(
        *,
        term: str = "item count",
        definition: str = "A counted row of the items table.",
        document: str = "Rows are counted once.",
    ) -> list[VectorMatch]:
        """Retrieval matches built by the real governance → document → vector path.

        The documents come from ``StructuredKnowledgeBase.to_documents``, i.e. what
        an indexed governed knowledge base hands the retrieval node, so the test
        never hand-writes the metadata the product reads back.
        """
        knowledge = StructuredKnowledgeBase()
        knowledge.add_glossary(
            GlossaryEntry(term=term, definition=definition, owner="ops")
        )
        knowledge.add_source(
            KnowledgeSource(
                id="counting_policy",
                kind="document",
                name="Counting policy",
                content_hash=content_hash(document),
            ),
            text=document,
        )
        documents = KnowledgeBaseBuilder.build_governed_documents(
            knowledge, skip_tainted=False
        )
        return [
            VectorMatch(
                id=entry.id,
                text=entry.text,
                metadata=dict(entry.metadata),
                source_type=entry.source_type,
                created_at=entry.created_at,
                score=1.0,
            )
            for entry in documents
            if entry.source_type in {"glossary", "knowledge_document"}
        ]

    def run_orchestrator(
        self, session_id: str, run_id: str, *, context: Context
    ) -> tuple[SessionStore, dict]:
        """Record one turn through the orchestrator only — no service annotation."""
        question = context.task.question
        store = self.store()
        memory = store.load_or_create(session_id)
        orchestrator = OrchestratorAgent(AgentTeamStateStore(self.state_root))

        def workflow(analysis_hook, candidate_hook, completion_hook) -> dict:
            analysis_hook(context)
            return {
                "status": "success",
                "run_id": run_id,
                "question": question,
                "sql": self.SQL,
                "columns": ["item_count"],
            }

        output = orchestrator.run(
            run_id=run_id,
            decision=EntryRouterAgent().route(question, "cli"),
            workflow=workflow,
            session_memory=memory,
            session_store=store,
            original_question=question,
        )
        return store, output

    def service_with_contexts(self, *contexts: Context) -> AgentService:
        """A service whose runner hands the orchestrator the prepared contexts."""
        remaining = list(contexts)

        def context_factory(task):
            if remaining:
                return remaining.pop(0)
            return self.workflow_context(
                question=task.question, matches=self.retrieved_knowledge()
            )

        return AgentService(
            config_loader=lambda **_: self.config,
            runner_factory=lambda config, **kwargs: _HookDrivenRunner(
                config, context_factory=context_factory, **kwargs
            ),
            llm_factory=lambda _: SessionLLM(),
        )

    def references(self, session_id: str) -> list[str]:
        """Every version reference the persisted turns of one session carry."""
        memory = self.store().load(session_id)
        return [
            reference.reference()
            for turn in memory.history
            for reference in turn.knowledge_versions
        ]

    def reference_of_kind(self, session_id: str, kind: str):
        """The single reference of one kind on a session's last turn."""
        memory = self.store().load(session_id)
        matched = [
            reference
            for reference in memory.history[-1].knowledge_versions
            if reference.kind == kind
        ]
        self.assertEqual(len(matched), 1, [item.reference() for item in matched])
        return matched[0]

    # ------------------------------------------------------------ the writer
    def test_the_orchestrator_records_the_versions_a_run_used_by_itself(self):
        """No service annotation: the writer records what the run relied on."""
        self.run_orchestrator("writer", "qf_writer", context=self.workflow_context())
        references = self.references("writer")
        digest = sha256(self.semantic_model.read_bytes()).hexdigest()[:12]
        self.assertEqual(
            set(references),
            {f"model:session_fixture@{digest}", f"metric:item_count@{digest}"},
        )
        # This run retrieved no governed knowledge, so it records none of it.
        self.assertEqual(
            [
                reference
                for reference in references
                if reference.startswith(("glossary:", "knowledge:"))
            ],
            [],
        )

    def test_each_kind_of_reference_marks_exactly_the_turns_that_used_it(self):
        for version_ref, session_id in (
            ("metric:item_count", "metric_scope"),
            ("model:session_fixture", "model_scope"),
            ("glossary:item count", "glossary_scope"),
        ):
            with self.subTest(version_ref=version_ref):
                self.run_orchestrator(
                    session_id,
                    f"qf_{session_id}_used",
                    context=self.workflow_context(matches=self.retrieved_knowledge()),
                )
                # A second turn in the same session used no definition at all.
                self.run_orchestrator(
                    session_id,
                    f"qf_{session_id}_empty",
                    context=self.workflow_context(matches=(), semantic_model=False),
                )
                service = self.service()
                result = service.invalidate_session_knowledge_version(
                    version_ref, session_id=session_id
                )
                self.assertEqual(result["turns"], 1)
                turns = service.session_status(session_id)["turns"]
                self.assertEqual([turn["turn_number"] for turn in turns], [1, 2])
                self.assertEqual(
                    [turn["invalidated"] for turn in turns], [True, False]
                )
                recorded = turns[0]["knowledge_versions"]
                self.assertTrue(
                    any(
                        reference.startswith(f"{version_ref}@")
                        for reference in recorded
                    ),
                    recorded,
                )

    def test_invalidation_leaves_another_sessions_turns_untouched(self):
        for session_id in ("scoped-a", "scoped-b"):
            self.run_orchestrator(
                session_id,
                f"qf_{session_id.replace('-', '_')}",
                context=self.workflow_context(matches=self.retrieved_knowledge()),
            )
        service = self.service()
        scoped = service.invalidate_session_knowledge_version(
            "glossary:item count", session_id="scoped-a"
        )
        self.assertEqual(scoped["affected"], [{"session_id": "scoped-a", "turns": 1}])
        self.assertEqual(service.session_status("scoped-a")["invalidated_turns"], 1)
        self.assertEqual(service.session_status("scoped-b")["invalidated_turns"], 0)
        # The unscoped form then reaches exactly the one turn left.
        self.assertEqual(
            service.invalidate_session_knowledge_version("glossary:item count")[
                "turns"
            ],
            1,
        )
        self.assertEqual(service.session_status("scoped-b")["invalidated_turns"], 1)

    def test_a_turn_that_used_nothing_records_nothing(self):
        """The control: an empty turn records nothing and weights nothing."""
        # A session whose only turn used no definition at all.
        self.run_orchestrator(
            "empty_usage",
            "qf_empty_usage",
            context=self.workflow_context(matches=(), semantic_model=False),
        )
        self.assertEqual(self.references("empty_usage"), [])
        service = self.service()
        for version_ref in (
            "metric:item_count",
            "model:session_fixture",
            "glossary:item count",
        ):
            with self.subTest(version_ref=version_ref):
                self.assertEqual(
                    service.invalidate_session_knowledge_version(
                        version_ref, session_id="empty_usage"
                    )["turns"],
                    0,
                )

        # In a session that also ran a loaded turn, only the loaded turn marks.
        for version_ref, session_id in (
            ("metric:item_count", "empty_metric"),
            ("model:session_fixture", "empty_model"),
            ("glossary:item count", "empty_glossary"),
        ):
            with self.subTest(loaded=version_ref):
                self.run_orchestrator(
                    session_id,
                    f"qf_{session_id}_empty",
                    context=self.workflow_context(matches=(), semantic_model=False),
                )
                self.run_orchestrator(
                    session_id,
                    f"qf_{session_id}_loaded",
                    context=self.workflow_context(matches=self.retrieved_knowledge()),
                )
                turns = service.session_status(session_id)["turns"]
                self.assertEqual(turns[0]["knowledge_versions"], [])
                self.assertTrue(turns[1]["knowledge_versions"])
                self.assertEqual(
                    service.invalidate_session_knowledge_version(
                        version_ref, session_id=session_id
                    )["turns"],
                    1,
                )
                turns = service.session_status(session_id)["turns"]
                self.assertEqual(
                    [turn["invalidated"] for turn in turns], [False, True]
                )

    # ------------------------------------------------------ version digests
    def test_a_changed_glossary_definition_supersedes_the_recorded_digest(self):
        definition = "A counted row of the items table."
        self.run_orchestrator(
            "glossary_before",
            "qf_glossary_before",
            context=self.workflow_context(
                matches=self.retrieved_knowledge(definition=definition)
            ),
        )
        before = self.reference_of_kind("glossary_before", "glossary")
        # An independent run over the same definition records the same version: the
        # digest is content-derived, not a per-run artefact.
        self.run_orchestrator(
            "glossary_repeat",
            "qf_glossary_repeat",
            context=self.workflow_context(
                matches=self.retrieved_knowledge(definition=definition)
            ),
        )
        self.assertEqual(
            self.reference_of_kind("glossary_repeat", "glossary").version,
            before.version,
        )

        edited = "A counted row, excluding cancelled rows."
        self.run_orchestrator(
            "glossary_after",
            "qf_glossary_after",
            context=self.workflow_context(
                matches=self.retrieved_knowledge(definition=edited)
            ),
        )
        after = self.reference_of_kind("glossary_after", "glossary")
        self.assertNotEqual(before.version, after.version)

        service = self.service()
        # The superseded version still names the turn that used it ...
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                before.reference(), session_id="glossary_before"
            )["turns"],
            1,
        )
        # ... and no longer matches a turn that ran against the edited definition.
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                before.reference(), session_id="glossary_after"
            )["turns"],
            0,
        )
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                after.reference(), session_id="glossary_after"
            )["turns"],
            1,
        )

    def test_a_changed_semantic_model_supersedes_the_recorded_digest(self):
        self.run_orchestrator(
            "model_before", "qf_model_before", context=self.workflow_context()
        )
        before = self.reference_of_kind("model_before", "model")
        self.semantic_model.write_text(
            SESSION_MODEL.replace("Number of items.", "Number of rows."),
            encoding="utf-8",
        )
        self.run_orchestrator(
            "model_after", "qf_model_after", context=self.workflow_context()
        )
        after = self.reference_of_kind("model_after", "model")
        self.assertNotEqual(before.version, after.version)
        # The metric carries the model's digest, so the superseded model version
        # identifies the whole definition set the earlier turn ran against.
        self.assertEqual(
            set(self.references("model_before")),
            {before.reference(), f"metric:item_count@{before.version}"},
        )

        service = self.service()
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                before.reference(), session_id="model_before"
            )["turns"],
            1,
        )
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                before.reference(), session_id="model_after"
            )["turns"],
            0,
        )
        self.assertEqual(
            service.invalidate_session_knowledge_version(
                after.reference(), session_id="model_after"
            )["turns"],
            1,
        )

    # ------------------------------------------------- both legs of the write
    def test_the_service_compensation_neither_duplicates_nor_drops_references(self):
        """Orchestrator and service write the same turn: one reference each, one turn."""
        service = self.service_with_contexts(
            self.workflow_context(matches=self.retrieved_knowledge())
        )
        options = dict(
            database=str(self.database),
            skills=[],
            session_id="both_legs",
            orchestration_state_root=str(self.state_root),
        )
        first = service.ask(self.QUESTION, AgentOptions(run_id="qf_both_legs", **options))
        self.assertEqual(first["status"], "success")

        references = self.references("both_legs")
        self.assertEqual(len(references), len(set(references)), references)
        for prefix in (
            "model:session_fixture@",
            "metric:item_count@",
            "glossary:item count@",
            "knowledge:counting_policy@",
        ):
            self.assertTrue(
                any(reference.startswith(prefix) for reference in references),
                references,
            )
        memory = self.store().load("both_legs")
        self.assertEqual(memory.turn_count, 1)
        self.assertEqual(len(memory.history), 1)

        # A second request appends one more turn — the compensation never appends
        # a second turn for the run it annotates, and never a duplicate reference.
        service.ask(
            self.QUESTION, AgentOptions(run_id="qf_both_legs_second", **options)
        )
        memory = self.store().load("both_legs")
        self.assertEqual(memory.turn_count, 2)
        self.assertEqual(len(memory.history), 2)
        for turn in memory.history:
            rendered = [reference.reference() for reference in turn.knowledge_versions]
            self.assertEqual(len(rendered), len(set(rendered)), rendered)
            self.assertTrue(
                any(
                    reference.startswith(("glossary:", "knowledge:"))
                    for reference in rendered
                ),
                rendered,
            )


if __name__ == "__main__":
    unittest.main()
