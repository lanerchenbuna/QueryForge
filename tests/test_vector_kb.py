import sqlite3
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.core.schemas.models import (
    Context,
    SqlTask,
    TableColumn,
    TableSchema,
    VectorMatch,
)
from queryforge.infrastructure.storage import (
    KnowledgeBaseBuilder,
    SQLHistoryStore,
    VectorDocument,
    VectorSearchResult,
    VectorStore,
    VectorStoreError,
    LanceDBVectorStore,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"


class MemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        self.documents = {}

    def add_documents(self, documents):
        docs = list(documents)
        self.documents.update({document.id: document for document in docs})
        return len(docs)

    def search(self, query, *, top_k=3, source_types=None):
        requested = set(source_types or ())
        candidates = [
            document
            for document in self.documents.values()
            if not requested or document.source_type in requested
        ]
        candidates.sort(
            key=lambda document: (
                any(token.lower() in document.text.lower() for token in query.split()),
                document.id,
            ),
            reverse=True,
        )
        return [
            VectorSearchResult(
                id=document.id,
                text=document.text,
                metadata=document.metadata,
                source_type=document.source_type,
                created_at=document.created_at,
                score=0.9,
            )
            for document in candidates[:top_k]
        ]

    def rebuild(self, documents):
        self.documents = {}
        self.add_documents(documents)
        return self.stats()

    def stats(self):
        tables = {
            "sql_history_vectors": sum(
                document.source_type != "schema_doc"
                for document in self.documents.values()
            ),
            "schema_doc_vectors": sum(
                document.source_type == "schema_doc"
                for document in self.documents.values()
            ),
        }
        return {"tables": tables, "total": sum(tables.values())}


class DeterministicEmbedding:
    def embed(self, texts):
        return [
            [
                float(len(text)),
                float(text.lower().count("school")),
                float(text.lower().count("item")),
                float(text.lower().count("name")),
            ]
            for text in texts
        ]


class VectorWorkflowLLM:
    def __init__(self) -> None:
        self.gen_prompt = ""

    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "No optional skill."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "Correct result.",
                "suggested_fix": None,
            }
        self.gen_prompt = prompt
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List names.",
            "tables_used": ["items"],
        }


class VectorKnowledgeBaseTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database_path = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database_path)
        connection.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        connection.execute("INSERT INTO items VALUES (1, 'alpha')")
        connection.commit()
        connection.close()
        self.history = SQLHistoryStore(self.root / "history.sqlite")
        self.config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database_path),
            history_db_path=str(self.root / "history.sqlite"),
            vector_kb_path=str(self.root / "lancedb"),
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_builder_covers_history_schema_and_bundled_source_types(self):
        self.history.add(
            question="List item names",
            sql="SELECT name FROM items",
            explanation="List names",
            tables_used=["items"],
            success=True,
        )
        store = MemoryVectorStore()
        stats = KnowledgeBaseBuilder(store).rebuild(
            history_store=self.history,
            schemas=[
                TableSchema(
                    table_name="items",
                    columns=[TableColumn(name="name", data_type="TEXT")],
                )
            ],
            sources=[
                SAMPLE_ROOT / "reference_sql",
                SAMPLE_ROOT / "reference_template",
                SAMPLE_ROOT / "success_story.csv",
            ],
        )
        source_types = {document.source_type for document in store.documents.values()}
        self.assertEqual(
            source_types,
            {
                "sql_history",
                "schema_doc",
                "reference_sql",
                "reference_template",
                "success_story",
            },
        )
        self.assertEqual(stats["total"], len(store.documents))

    def test_workflow_retrieves_separate_context_and_writes_success(self):
        store = MemoryVectorStore()
        store.add_documents(
            [
                VectorDocument.create(
                    id="reference:1",
                    text="Question: list names SQL: SELECT name FROM items",
                    source_type="reference_sql",
                    metadata={"sql": "SELECT name FROM items"},
                )
            ]
        )
        llm = VectorWorkflowLLM()
        output = WorkflowRunner(
            self.config,
            llm_factory=lambda _: llm,
            selected_skills=[],
            enable_vector_kb=True,
            vector_store=store,
            vector_top_k=2,
        ).run(SqlTask(question="List item names", database_path=str(self.database_path)))
        self.assertEqual(output["rows"], [["alpha"]])
        self.assertEqual(output["vector_kb"]["status"], "active")
        self.assertEqual(output["vector_kb"]["write_status"], "inserted")
        self.assertTrue(output["vector_kb"]["sql_matches"])
        self.assertTrue(output["vector_kb"]["schema_matches"])
        self.assertIn("Current SQLite schema (authoritative)", llm.gen_prompt)
        self.assertIn("similar historical SQL and reference material", llm.gen_prompt)
        self.assertIn("schema documentation", llm.gen_prompt)
        self.assertIn("history:", " ".join(store.documents))

    def test_missing_embedding_key_degrades_without_blocking_query(self):
        output = WorkflowRunner(
            self.config,
            llm_factory=lambda _: VectorWorkflowLLM(),
            selected_skills=[],
            enable_vector_kb=True,
        ).run(SqlTask(question="List item names", database_path=str(self.database_path)))
        self.assertEqual(output["rows"], [["alpha"]])
        self.assertEqual(output["vector_kb"]["status"], "degraded")
        self.assertIn("embedding API key", output["vector_kb"]["error"])

    def test_missing_lancedb_dependency_has_install_hint(self):
        from unittest.mock import patch

        with patch.dict(sys.modules, {"lancedb": None}):
            with self.assertRaisesRegex(VectorStoreError, r"pip install.*vector"):
                LanceDBVectorStore(
                    self.root / "missing-lancedb",
                    embedding_provider=DeterministicEmbedding(),
                )

    def test_gen_sql_prompt_marks_vector_context_non_authoritative(self):
        context = Context(
            task=SqlTask(question="List names", database_path="test.sqlite"),
            relevant_tables=[
                TableSchema(
                    table_name="items",
                    columns=[TableColumn(name="name", data_type="TEXT")],
                )
            ],
            vector_sql_matches=[
                VectorMatch(
                    id="one",
                    text="SQL: SELECT name FROM items",
                    source_type="reference_sql",
                    created_at="2026-07-15T00:00:00+00:00",
                    score=0.9,
                )
            ],
        )
        prompt = GenSqlNode._build_prompt(context)
        self.assertIn("SELECT name FROM items", prompt)
        self.assertIn("supporting evidence only", prompt)

    @unittest.skipUnless(importlib.util.find_spec("lancedb"), "optional lancedb not installed")
    def test_real_lancedb_add_search_rebuild_and_stats(self):
        store = LanceDBVectorStore(
            self.root / "real-lancedb",
            embedding_provider=DeterministicEmbedding(),
        )
        documents = [
            VectorDocument.create(
                id="sql:1",
                text="Question: list item names SQL: SELECT name FROM items",
                source_type="sql_history",
            ),
            VectorDocument.create(
                id="schema:items",
                text="Table: items Columns: name (TEXT)",
                source_type="schema_doc",
            ),
        ]
        self.assertEqual(store.add_documents(documents), 2)
        self.assertEqual(store.stats()["total"], 2)
        matches = store.search(
            "list item names", top_k=1, source_types=("sql_history",)
        )
        self.assertEqual(matches[0].id, "sql:1")
        stats = store.rebuild(documents[:1])
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["tables"]["schema_doc_vectors"], 0)


if __name__ == "__main__":
    unittest.main()
