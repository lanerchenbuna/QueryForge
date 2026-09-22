"""H5: the vector-KB rebuild must index the *governed* schema.

The CLI built its ``DatabaseTool`` without a SQL policy, so withheld columns
(``dim_user.email``, PII) were described and written into the retrieval index as
schema documents. These tests drive the real ``--rebuild-vector-kb`` command with
the vector store and the KB builder replaced by recording doubles, and inspect
the schemas the builder actually received.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main as cli

from queryforge.core.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANIME = PROJECT_ROOT / "sample_data" / "anime_streaming"
ANIME_DATABASE = ANIME / "anime_streaming.sqlite"
ANIME_POLICY = ANIME / "sql_policy.yml"
ANIME_SOURCE = ANIME / "reference_sql" / "watch_hours_by_format.sql"


class RecordingVectorStore:
    """Minimal stand-in for ``LanceDBVectorStore`` (no LanceDB, no embeddings)."""

    def __init__(self, path, embedding_provider=None) -> None:
        self.path = Path(path).expanduser()

    def stats(self) -> dict:
        return {"documents": 0}


class RecordingEmbeddingProvider:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args


class RecordingKnowledgeBaseBuilder:
    """Captures the schemas the CLI hands to the retrieval index builder."""

    last: "RecordingKnowledgeBaseBuilder | None" = None

    def __init__(self, vector_store, manifest_path=None) -> None:
        self.vector_store = vector_store
        self.manifest_path = manifest_path
        self.schemas = None
        self.sources = None
        self.knowledge = None
        RecordingKnowledgeBaseBuilder.last = self

    def rebuild(self, *, history_store, schemas, sources, knowledge=None) -> dict:
        self.schemas = list(schemas)
        self.sources = list(sources)
        # Recorded so a test can assert the governed path is actually reachable
        # from the CLI. Previously `knowledge` was never passed, so
        # build_governed_documents (verification tiers, conflict detection,
        # holdout isolation) only ever ran in tests.
        self.knowledge = knowledge
        return {"documents": len(self.schemas), "sources": len(self.sources)}

    @property
    def document_text(self) -> str:
        return json.dumps(
            [schema.model_dump(mode="json") for schema in self.schemas or []],
            ensure_ascii=False,
        )


class RecordingHistoryStore:
    def __init__(self, *args, **kwargs) -> None:
        self.database_path = Path(args[0]).expanduser() if args else None


class CliKnowledgeBaseGovernanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        RecordingKnowledgeBaseBuilder.last = None

    def tearDown(self) -> None:
        self.directory.cleanup()

    def config(self, *, sql_policy_path: str | None) -> Config:
        return Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(ANIME_DATABASE),
            history_db_path=str(self.root / "history.sqlite"),
            vector_kb_path=str(self.root / "kb"),
            sql_policy_path=sql_policy_path,
        )

    def run_rebuild(self, config: Config, argv: list[str]) -> tuple[int, RecordingKnowledgeBaseBuilder]:
        """Run the CLI rebuild with every external dependency doubled."""

        with patch.object(cli, "load_config", lambda **_: config), patch.object(
            cli, "LanceDBVectorStore", RecordingVectorStore
        ), patch.object(
            cli, "OpenAIEmbeddingProvider", RecordingEmbeddingProvider
        ), patch.object(
            cli, "KnowledgeBaseBuilder", RecordingKnowledgeBaseBuilder
        ), patch.object(
            cli, "SQLHistoryStore", RecordingHistoryStore
        ), patch.object(sys, "argv", ["queryforge", *argv]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = cli.main()
        builder = RecordingKnowledgeBaseBuilder.last
        self.assertIsNotNone(builder, f"the rebuild never ran: {stderr.getvalue()}")
        return exit_code, builder

    def test_rebuild_indexes_the_configured_policy_schema(self):
        config = self.config(sql_policy_path=str(ANIME_POLICY))
        exit_code, builder = self.run_rebuild(
            config,
            [
                "--rebuild-vector-kb",
                "--database",
                str(ANIME_DATABASE),
                "--kb-source",
                str(ANIME_SOURCE),
            ],
        )
        self.assertEqual(exit_code, 0)
        documents = builder.document_text
        # The policy withholds PII from dim_user; a policy-free tool described it,
        # which is exactly how it ended up in the retrieval index.
        self.assertNotIn("email", documents)
        # The governed columns of the same table are still indexed, so this is a
        # filtered schema rather than a missing table.
        self.assertIn("user_handle", documents)
        self.assertIn("dim_user", documents)
        self.assertEqual(builder.sources, [str(ANIME_SOURCE)])

    def test_rebuild_accepts_an_explicit_sql_policy_flag(self):
        config = self.config(sql_policy_path=None)
        # Without the flag and without a configured policy the rebuild is
        # ungoverned by definition; the explicit flag is what governs it.
        exit_code, builder = self.run_rebuild(
            config,
            [
                "--rebuild-vector-kb",
                "--database",
                str(ANIME_DATABASE),
                "--kb-source",
                str(ANIME_SOURCE),
                "--sql-policy",
                str(ANIME_POLICY),
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertNotIn("email", builder.document_text)
        self.assertIn("user_handle", builder.document_text)

    # ---------------------------------------------------------- governed knowledge (E-05)

    def test_rebuild_without_knowledge_leaves_the_governed_path_unused(self):
        """Documents the gap this flag closes: with no knowledge source the CLI
        never reaches build_governed_documents."""
        config = self.config(sql_policy_path=str(ANIME_POLICY))
        exit_code, builder = self.run_rebuild(
            config,
            [
                "--rebuild-vector-kb",
                "--database",
                str(ANIME_DATABASE),
                "--kb-source",
                str(ANIME_SOURCE),
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertIsNone(builder.knowledge)

    def test_rebuild_passes_a_structured_knowledge_base_to_the_builder(self):
        """``--kb-knowledge`` makes the governed path reachable from the CLI.

        Before this flag there was no way to supply structured knowledge, so the
        three-tier verification, conflict detection and holdout isolation in
        domain/knowledge/governance.py could only ever run in tests.
        """
        config = self.config(sql_policy_path=str(ANIME_POLICY))
        with tempfile.TemporaryDirectory() as directory:
            knowledge_path = Path(directory) / "knowledge.json"
            knowledge_path.write_text(
                json.dumps(
                    {
                        "metrics": {
                            "watch_hours::v1": {
                                "metric_id": "watch_hours",
                                "name": "Watch Hours",
                                "expression": "SUM(fact_watch_session.watch_seconds) / 3600.0",
                                "aggregation": "sum",
                                "entity": "watch_session",
                                "version": "v1",
                            }
                        },
                        "glossary": {},
                        "sources": {},
                        "documents": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            exit_code, builder = self.run_rebuild(
                config,
                [
                    "--rebuild-vector-kb",
                    "--database",
                    str(ANIME_DATABASE),
                    "--kb-source",
                    str(ANIME_SOURCE),
                    "--kb-knowledge",
                    str(knowledge_path),
                ],
            )
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(builder.knowledge)
        self.assertIn("watch_hours::v1", builder.knowledge.metrics)

    def test_kb_knowledge_requires_rebuild_and_an_existing_file(self):
        config = self.config(sql_policy_path=str(ANIME_POLICY))
        # Without --rebuild-vector-kb the flag must be rejected as a usage error.
        with patch.object(cli, "load_config", lambda **_: config), patch.object(
            sys, "argv", ["queryforge", "--kb-knowledge", "somewhere.json"]
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = cli.main()
        self.assertEqual(exit_code, 2)
        self.assertIn("--kb-knowledge requires --rebuild-vector-kb", stderr.getvalue())

        # A missing file is also a usage error, not a crash.
        exit_code, _builder = self.run_rebuild(
            config,
            [
                "--rebuild-vector-kb",
                "--database",
                str(ANIME_DATABASE),
                "--kb-knowledge",
                "/nonexistent/knowledge.json",
            ],
        )
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
