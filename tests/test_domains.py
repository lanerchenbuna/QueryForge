"""Offline tests for typed data domains and domain-scoped storage."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.application import AgentOptions, AgentService
from queryforge.core.config import (
    DEFAULT_DOMAIN_REGISTRY_PATH,
    PROJECT_ROOT,
    Config,
    load_config,
)
from queryforge.core.schemas.models import SQLContext, TableColumn, TableSchema
from queryforge.domain import (
    DomainContext,
    DomainError,
    DomainRegistry,
    DomainResolver,
)
from queryforge.infrastructure.storage import (
    KnowledgeBaseBuilder,
    SQLHistoryError,
    SQLHistoryStore,
)


class DomainLLM:
    """Deterministic provider: skill selection, reflection, and one valid SELECT."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, prompt: str):
        self.calls += 1
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


class ForbiddenLLM:
    """Fails the test if any model work happens before domain resolution."""

    def generate_json(self, prompt: str):
        raise AssertionError("no model call is allowed for an unresolvable domain")


class DomainFixture(unittest.TestCase):
    """Shared temporary databases, registry path, and context builders."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.registry_path = self.root / "domains/registry.json"
        self.database = self._make_database("items.sqlite", [("a",), ("b",)])
        self.other_database = self._make_database("other_items.sqlite", [("z",)])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _make_database(self, name: str, rows: list[tuple[str]]) -> Path:
        path = self.root / name
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.executemany("INSERT INTO items VALUES (?)", rows)
        connection.commit()
        connection.close()
        return path

    def context(
        self, domain_id: str = "retail", database: Path | None = None
    ) -> DomainContext:
        return DomainContext(
            domain_id=domain_id,
            source_id="src-1",
            data_version="v1",
            schema_fingerprint="fp-1",
            semantic_version="semantic-1",
            policy_version="policy-1",
            database_path=str(database or self.database),
        )

    def config(self, **overrides) -> Config:
        values = {
            "llm_provider": "openai",
            "llm_api_key": None,
            "llm_model": "offline",
            "llm_base_url": None,
            "database_path": str(self.other_database),
            "history_db_path": str(self.root / "history.sqlite"),
            "domain_registry_path": str(self.registry_path),
        }
        values.update(overrides)
        return Config(**values)

    def service(self, llm=None) -> AgentService:
        return AgentService(
            config_loader=lambda **_: self.config,
            llm_factory=lambda _: llm or DomainLLM(),
        )


class DomainResolverTest(DomainFixture):
    def test_publish_then_resolve_roundtrip_writes_registry_atomically(self) -> None:
        resolver = DomainResolver(self.registry_path)
        self.assertEqual(resolver.list_domains(), [])
        context = self.context()
        self.assertTrue(issubclass(DomainError, ValueError))

        resolver.publish(context)

        self.assertTrue(self.registry_path.is_file())
        temporary = self.registry_path.with_name(self.registry_path.name + ".tmp")
        self.assertFalse(temporary.exists())
        reloaded = DomainResolver(self.registry_path).resolve("retail")
        self.assertEqual(reloaded.model_dump(), context.model_dump())
        public = reloaded.to_public_dict()
        self.assertEqual(public["domain_id"], "retail")
        self.assertEqual(public["source_id"], "src-1")
        self.assertEqual(public["data_version"], "v1")
        self.assertEqual(public["schema_fingerprint"], "fp-1")
        self.assertEqual(public["semantic_version"], "semantic-1")
        self.assertEqual(public["policy_version"], "policy-1")
        self.assertEqual(public["database_path"], str(self.database))
        self.assertEqual(public["status"], "published")
        self.assertEqual(json.loads(json.dumps(public))["domain_id"], "retail")
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], "1.0")
        self.assertEqual(payload["domains"]["retail"]["status"], "published")

    def test_missing_registry_file_is_an_empty_registry(self) -> None:
        resolver = DomainResolver(self.registry_path)
        self.assertFalse(self.registry_path.exists())
        self.assertEqual(resolver.list_domains(), [])
        self.assertEqual(resolver.registry.domains, {})
        self.assertIsInstance(resolver.registry, DomainRegistry)

    def test_relative_registry_path_resolves_against_project_root(self) -> None:
        resolver = DomainResolver(DEFAULT_DOMAIN_REGISTRY_PATH)
        self.assertEqual(
            resolver.registry_path, (PROJECT_ROOT / DEFAULT_DOMAIN_REGISTRY_PATH)
        )
        self.assertTrue(DomainResolver("~/domains.json").registry_path.is_absolute())

    def test_invalid_registry_content_reports_the_path(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(DomainError, "Invalid data domain registry") as ctx:
            DomainResolver(self.registry_path)
        self.assertIn(str(self.registry_path), str(ctx.exception))

        self.registry_path.write_text(
            json.dumps({"domains": {"x": {}}}), encoding="utf-8"
        )
        with self.assertRaisesRegex(DomainError, "Invalid data domain registry"):
            DomainResolver(self.registry_path)

    def test_unknown_and_revoked_domains_raise_domain_error(self) -> None:
        resolver = DomainResolver(self.registry_path)
        with self.assertRaisesRegex(DomainError, "unknown data domain"):
            resolver.resolve("missing")
        with self.assertRaisesRegex(DomainError, "unknown data domain"):
            resolver.resolve("")

        resolver.publish(self.context())
        self.assertEqual(resolver.resolve("retail").domain_id, "retail")
        self.assertIsNone(resolver.resolve_optional(None))
        self.assertIsNone(resolver.resolve_optional("  "))
        self.assertEqual(resolver.resolve_optional("retail").domain_id, "retail")

        with self.assertRaisesRegex(DomainError, "unknown data domain"):
            resolver.revoke("missing")
        resolver.revoke("retail")
        with self.assertRaisesRegex(DomainError, "revoked"):
            resolver.resolve("retail")
        self.assertEqual(resolver.list_domains(), ["retail"])

    def test_validate_paths_rejects_missing_database_and_optional_files(self) -> None:
        missing = self.root / "absent.sqlite"
        context = DomainContext(
            domain_id="ghost",
            data_version="v1",
            schema_fingerprint="fp-ghost",
            database_path=str(missing),
        )
        with self.assertRaisesRegex(DomainError, "database does not exist"):
            context.validate_paths()
        with self.assertRaisesRegex(DomainError, "database does not exist"):
            DomainResolver(self.registry_path).publish(context)

        for field, message in (
            ("semantic_model_path", "semantic model does not exist"),
            ("sql_policy_path", "SQL policy does not exist"),
        ):
            scoped = self.context().model_copy(update={field: str(missing)})
            with self.assertRaisesRegex(DomainError, message):
                scoped.validate_paths()

        self.context().validate_paths()

    def test_from_config_uses_the_configured_registry_path(self) -> None:
        config = self.config()
        resolver = DomainResolver.from_config(config)
        self.assertEqual(resolver.registry_path, self.registry_path)
        resolver.publish(self.context())
        self.assertEqual(
            DomainResolver.from_config(config).resolve("retail").data_version, "v1"
        )
        self.assertEqual(
            DomainResolver(config.domain_registry_path).list_domains(), ["retail"]
        )

    def test_list_domains_is_sorted_and_keeps_revoked_entries(self) -> None:
        resolver = DomainResolver(self.registry_path)
        resolver.publish(self.context(domain_id="zulu"))
        resolver.publish(self.context(domain_id="alpha"))
        resolver.publish(self.context(domain_id="midway"))
        self.assertEqual(resolver.list_domains(), ["alpha", "midway", "zulu"])
        resolver.revoke("alpha")
        self.assertEqual(resolver.list_domains(), ["alpha", "midway", "zulu"])
        resolver.publish(self.context(domain_id="zulu", database=self.other_database))
        self.assertEqual(
            resolver.resolve("zulu").database_path, str(self.other_database)
        )


class DomainConfigTest(unittest.TestCase):
    def test_domain_registry_path_defaults_and_reads_the_environment(self) -> None:
        self.assertEqual(
            Config(
                llm_provider="openai",
                llm_api_key=None,
                llm_model="offline",
                llm_base_url=None,
                database_path="items.sqlite",
            ).domain_registry_path,
            DEFAULT_DOMAIN_REGISTRY_PATH,
        )
        self.assertEqual(
            DEFAULT_DOMAIN_REGISTRY_PATH, ".queryforge/domains/registry.json"
        )

        with patch("queryforge.core.config.load_dotenv"), patch.dict(
            os.environ, {"LLM_PROVIDER": "openai"}, clear=True
        ):
            self.assertEqual(
                load_config().domain_registry_path, DEFAULT_DOMAIN_REGISTRY_PATH
            )
        with patch("queryforge.core.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "openai",
                "DOMAIN_REGISTRY_PATH": "custom/domains.json",
            },
            clear=True,
        ):
            self.assertEqual(load_config().domain_registry_path, "custom/domains.json")


class AgentServiceDomainTest(DomainFixture):
    def setUp(self) -> None:
        super().setUp()
        self.registry = DomainResolver(self.registry_path)
        self.registry.publish(self.context())
        self.config = self.config()
        self.history_path = Path(self.config.history_db_path)

    def test_domain_id_resolves_controlled_paths_and_scopes_history(self) -> None:
        answer = self.service().ask(
            "List item names",
            AgentOptions(domain_id="retail", skills=[], run_id="qf_domain"),
        )

        # The config default database is `other_database`; these rows prove the
        # run used the domain-resolved database path instead.
        self.assertEqual(answer["rows"], [["a"], ["b"]])
        self.assertEqual(answer["domain"]["domain_id"], "retail")
        self.assertEqual(answer["domain"]["data_version"], "v1")
        self.assertEqual(answer["domain"]["schema_fingerprint"], "fp-1")
        self.assertEqual(answer["domain"]["resolved_by"], "registry")
        self.assertEqual(answer["domain"]["status"], "published")

        store = SQLHistoryStore(self.history_path)
        entries = store.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].metadata["domain_id"], "retail")
        self.assertEqual(entries[0].metadata["data_version"], "v1")
        scoped = store.search("List item names", domain_id="retail", data_version="v1")
        self.assertEqual([match.sql for match in scoped], [entries[0].sql])
        self.assertEqual(store.list_domains(), ["retail"])

    def test_domain_run_publishes_a_governed_retrieval_scope(self) -> None:
        """The production path must hand the domain scope to retrieval (step 13).

        Without this wiring the retrieval chain stays unfiltered and another
        domain's definitions can compete for this run's context window.
        """
        from queryforge.workflow.node import schema_linking_node as module

        captured: dict = {}
        original = module.SchemaLinkingNode.execute

        def spy(node, context):
            captured.setdefault(
                "scopes", []
            ).append(dict(context.task_context.get("retrieval_scope") or {}))
            return original(node, context)

        with patch.object(module.SchemaLinkingNode, "execute", spy):
            self.service().ask(
                "List item names",
                AgentOptions(domain_id="retail", skills=[], run_id="qf_scope"),
            )
        self.assertTrue(captured["scopes"])
        scope = captured["scopes"][0]
        self.assertEqual(scope["domain_id"], "retail")
        self.assertEqual(scope["data_version"], "v1")
        self.assertEqual(scope["version"], "semantic-1")

        # A run with no domain publishes nothing: a single-database deployment
        # keeps the previous, unfiltered behaviour instead of filtering on "".
        captured.clear()
        with patch.object(module.SchemaLinkingNode, "execute", spy):
            self.service().ask(
                "List item names",
                AgentOptions(skills=[], run_id="qf_scope_local"),
            )
        self.assertTrue(captured["scopes"])
        self.assertEqual(captured["scopes"][0], {})

    def test_unknown_domain_fails_before_any_model_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown data domain"):
            self.service(llm=ForbiddenLLM()).ask(
                "List item names",
                AgentOptions(domain_id="ghost", skills=[], run_id="qf_ghost"),
            )

    def test_revoked_domain_fails_before_any_model_call(self) -> None:
        self.registry.revoke("retail")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.service(llm=ForbiddenLLM()).ask(
                "List item names",
                AgentOptions(domain_id="retail", skills=[], run_id="qf_revoked"),
            )

    def test_blank_domain_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "domain_id must be non-blank"):
            AgentOptions(domain_id="   ").validate_for_config(self.config)
        with self.assertRaisesRegex(ValueError, "domain_id must be non-blank"):
            self.service(llm=ForbiddenLLM()).ask(
                "List item names", AgentOptions(domain_id="   ", skills=[])
            )

    def test_local_cli_without_domain_id_keeps_existing_behavior(self) -> None:
        answer = self.service().ask(
            "List item names",
            AgentOptions(
                database=str(self.database), skills=[], run_id="qf_cli_legacy"
            ),
        )

        self.assertEqual(answer["rows"], [["a"], ["b"]])
        self.assertNotIn("domain", answer)
        entries = SQLHistoryStore(self.history_path).list_entries()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].metadata["domain_id"])
        store = SQLHistoryStore(self.history_path)
        self.assertEqual(
            [match.sql for match in store.search("List item names", domain_id=None)],
            [entries[0].sql],
        )
        self.assertEqual(store.search("List item names", domain_id="retail"), [])
        self.assertEqual(store.list_domains(), [])

    def test_network_entrypoint_rejects_path_conflict_while_cli_prefers_explicit(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with explicit path"):
            self.service(llm=ForbiddenLLM()).ask(
                "List item names",
                AgentOptions(
                    domain_id="retail",
                    database=str(self.other_database),
                    skills=[],
                    entrypoint="api",
                    run_id="qf_network_conflict",
                ),
            )

        cli_answer = self.service().ask(
            "List item names",
            AgentOptions(
                domain_id="retail",
                database=str(self.other_database),
                skills=[],
                entrypoint="cli",
                run_id="qf_cli_conflict",
            ),
        )
        self.assertEqual(cli_answer["rows"], [["z"]])
        self.assertEqual(cli_answer["domain"]["domain_id"], "retail")

    def test_network_entrypoint_accepts_matching_paths(self) -> None:
        answer = self.service().ask(
            "List item names",
            AgentOptions(
                domain_id="retail",
                database=str(self.database),
                skills=[],
                entrypoint="api",
                run_id="qf_network_match",
            ),
        )
        self.assertEqual(answer["rows"], [["a"], ["b"]])
        self.assertEqual(answer["domain"]["resolved_by"], "registry")


class HistoryDomainScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLHistoryStore(Path(self.directory.name) / "history.sqlite")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _seed(self) -> None:
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items",
            explanation="Domain a.",
            tables_used=["items"],
            success=True,
            metadata={"domain_id": "a", "data_version": "v1"},
        )
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items WHERE name IS NOT NULL",
            explanation="Legacy unscoped row.",
            tables_used=["items"],
            success=True,
            metadata={"domain_id": None},
        )
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items ORDER BY name",
            explanation="Domain b.",
            tables_used=["items"],
            success=True,
            metadata={"domain_id": "b", "data_version": "v1"},
        )

    def test_scoped_search_excludes_legacy_and_other_domains(self) -> None:
        self._seed()

        scoped = self.store.search(
            "List item names", top_k=10, domain_id="a", data_version="v1"
        )
        self.assertEqual([match.sql for match in scoped], ["SELECT name FROM items"])
        self.assertEqual(
            [
                match.sql
                for match in self.store.search(
                    "List item names", top_k=10, domain_id="b"
                )
            ],
            ["SELECT name FROM items ORDER BY name"],
        )
        # The domain filter is applied before the top-k cut.
        self.assertEqual(
            [
                match.sql
                for match in self.store.search(
                    "List item names", top_k=1, domain_id="a", data_version="v1"
                )
            ],
            ["SELECT name FROM items"],
        )
        # A version mismatch must not fall back to another version's SQL.
        self.assertEqual(
            self.store.search(
                "List item names", top_k=10, domain_id="a", data_version="v2"
            ),
            [],
        )
        # Legacy rows (metadata domain_id is None) are never returned when scoped.
        self.assertNotIn(
            "SELECT name FROM items WHERE name IS NOT NULL",
            [match.sql for match in scoped],
        )
        self.assertEqual(self.store.list_domains(), ["a", "b"])

    def test_unscoped_search_still_returns_every_row_including_legacy(self) -> None:
        self._seed()
        matches = self.store.search("List item names", top_k=10)
        self.assertEqual(len(matches), 3)
        self.assertIn(
            "SELECT name FROM items WHERE name IS NOT NULL",
            [match.sql for match in matches],
        )

    def test_blank_scope_is_rejected_instead_of_widening(self) -> None:
        self._seed()
        for blank in ("", "   "):
            with self.assertRaisesRegex(SQLHistoryError, "domain_id"):
                self.store.search("List item names", domain_id=blank)

    def test_list_domains_ignores_missing_and_malformed_metadata(self) -> None:
        self.store.add(question="q1", sql="SELECT 1", success=True)
        self.store.add(
            question="q2",
            sql="SELECT 2",
            success=True,
            metadata={"domain_id": "zulu"},
        )
        self.store.add(
            question="q3",
            sql="SELECT 3",
            success=True,
            metadata={"domain_id": ""},
        )
        connection = sqlite3.connect(self.store.database_path)
        connection.execute(
            "UPDATE sql_history SET metadata = ? WHERE question = ?", ("not json", "q1")
        )
        connection.commit()
        connection.close()
        self.assertEqual(self.store.list_domains(), ["zulu"])
        # A row with undecodable metadata is excluded from scoped results but
        # still usable by the unscoped (legacy) path.
        scoped = [
            match.sql
            for match in self.store.search("q1", top_k=5, domain_id="zulu")
        ]
        self.assertNotIn("SELECT 1", scoped)
        unscoped = [match.sql for match in self.store.search("q1", top_k=5)]
        self.assertIn("SELECT 1", unscoped)


class KnowledgeBaseDomainMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sql_context = SQLContext(
            sql="SELECT name FROM items",
            explanation="List names.",
            tables_used=["items"],
        )

    def test_successful_query_document_carries_domain_scope_when_provided(self) -> None:
        scoped = KnowledgeBaseBuilder.successful_query_document(
            question="List item names",
            sql_context=self.sql_context,
            history_id=7,
            domain_id="retail",
            data_version="v1",
        )
        self.assertEqual(scoped.metadata["domain_id"], "retail")
        self.assertEqual(scoped.metadata["data_version"], "v1")

        legacy = KnowledgeBaseBuilder.successful_query_document(
            question="List item names", sql_context=self.sql_context, history_id=8
        )
        self.assertNotIn("domain_id", legacy.metadata)
        self.assertNotIn("data_version", legacy.metadata)

    def test_schema_documents_carry_domain_scope_when_provided(self) -> None:
        schemas = [
            TableSchema(
                table_name="items",
                columns=[TableColumn(name="name", data_type="TEXT")],
            )
        ]
        scoped = KnowledgeBaseBuilder.schema_documents(
            schemas, domain_id="retail", data_version="v1"
        )[0]
        self.assertEqual(scoped.metadata["domain_id"], "retail")
        self.assertEqual(scoped.metadata["data_version"], "v1")
        self.assertEqual(scoped.id, "schema:items")

        legacy = KnowledgeBaseBuilder.schema_documents(schemas)[0]
        self.assertNotIn("domain_id", legacy.metadata)
        self.assertNotIn("data_version", legacy.metadata)

    def test_history_rebuild_keeps_recorded_domain_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLHistoryStore(Path(directory) / "history.sqlite")
            store.add(
                question="List item names",
                sql="SELECT name FROM items",
                success=True,
                metadata={"domain_id": "retail", "data_version": "v1"},
            )
            store.add(
                question="Count legacy rows",
                sql="SELECT COUNT(*) FROM items",
                success=True,
            )
            documents = {
                document.id: document
                for document in KnowledgeBaseBuilder.history_documents(store)
            }
        scoped = next(
            document
            for document in documents.values()
            if document.metadata["sql"] == "SELECT name FROM items"
        )
        self.assertEqual(scoped.metadata["domain_id"], "retail")
        self.assertEqual(scoped.metadata["data_version"], "v1")
        legacy = next(
            document
            for document in documents.values()
            if document.metadata["sql"] == "SELECT COUNT(*) FROM items"
        )
        self.assertNotIn("domain_id", legacy.metadata)


if __name__ == "__main__":
    unittest.main()
