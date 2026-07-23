import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main as cli
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.core.schemas.models import SqlTask
from queryforge.infrastructure.storage import SQLHistoryStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "sample_data/anime_streaming"


class HistoryWorkflowLLM:
    def __init__(self) -> None:
        self.gen_prompts = []

    def generate_json(self, prompt: str):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "No optional skill needed."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The query returns the requested names.",
                "suggested_fix": None,
            }
        self.gen_prompts.append(prompt)
        return {
            "sql": "SELECT name FROM items ORDER BY name",
            "explanation": "List item names.",
            "tables_used": ["items"],
        }


class SQLHistoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.history_path = Path(self.directory.name) / "nested/history.sqlite"
        self.store = SQLHistoryStore(self.history_path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_database_is_created_and_schema_has_no_result_rows_column(self) -> None:
        self.assertTrue(self.history_path.is_file())
        connection = sqlite3.connect(self.history_path)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sql_history)")
        }
        connection.close()
        self.assertIn("row_count", columns)
        self.assertNotIn("rows", columns)
        self.assertNotIn("result", columns)

    def test_add_deduplicates_question_and_sql(self) -> None:
        first_id, first_inserted = self.store.add(
            question="List item names",
            sql="SELECT name FROM items",
            explanation="List names",
            tables_used=["items"],
            success=True,
            row_count=2,
            provider="qwen",
            model="qwen-plus",
        )
        second_id, second_inserted = self.store.add(
            question="List item names",
            sql="SELECT name FROM items",
            success=True,
        )
        self.assertTrue(first_inserted)
        self.assertFalse(second_inserted)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.store.list_entries()), 1)

    def test_search_returns_only_success_and_supports_table_filter(self) -> None:
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items",
            tables_used=["items"],
            success=True,
        )
        self.store.add(
            question="List item names with a broken query",
            sql="SELECT missing FROM items",
            tables_used=["items"],
            success=False,
            error="no such column",
        )
        matches = self.store.search(
            "Please list the item names", top_k=5, tables_used=["items"]
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].sql, "SELECT name FROM items")
        self.assertEqual(
            self.store.search("List item names", tables_used=["orders"]), []
        )

    def test_similarity_handles_chinese_text(self) -> None:
        score = self.store.similarity("查询最近30天订单", "请查询最近 30 天的订单")
        self.assertGreater(score, 0.5)

    def test_import_success_stories_and_reference_sql_with_dedup(self) -> None:
        first = self.store.import_success_stories(SAMPLE_ROOT / "success_story.csv")
        second = self.store.import_success_stories(SAMPLE_ROOT / "success_story.csv")
        references = self.store.import_reference_sql(SAMPLE_ROOT / "reference_sql")
        self.assertEqual(first.inserted, 3)
        self.assertEqual(second.duplicates, 3)
        self.assertGreaterEqual(references.inserted, 5)
        matches = self.store.search(
            "watch hours by anime format", top_k=3
        )
        self.assertTrue(matches)
        self.assertIn("watch_seconds", matches[0].sql)
        self.assertIn(matches[0].source, {"reference_sql", "success_story"})

    def test_clear_returns_deleted_count(self) -> None:
        self.store.add(question="q", sql="SELECT 1", success=True)
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(self.store.list_entries(), [])

    def test_successful_workflow_writes_then_retrieves_history(self) -> None:
        data_path = Path(self.directory.name) / "items.sqlite"
        connection = sqlite3.connect(data_path)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('a')")
        connection.commit()
        connection.close()
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(data_path),
            history_db_path=str(self.history_path),
        )
        first_llm = HistoryWorkflowLLM()
        first = WorkflowRunner(
            config, llm_factory=lambda _: first_llm, selected_skills=[]
        ).run(SqlTask(question="List item names", database_path=str(data_path)))
        self.assertEqual(first["history_write"]["status"], "inserted")

        second_llm = HistoryWorkflowLLM()
        second = WorkflowRunner(
            config, llm_factory=lambda _: second_llm, selected_skills=[]
        ).run(
            SqlTask(
                question="Please list the item names", database_path=str(data_path)
            )
        )
        self.assertTrue(second["history_matches"])
        self.assertIn("Persisted successful SQL history matches", second_llm.gen_prompts[0])
        self.assertIn("SELECT name FROM items", second_llm.gen_prompts[0])

    def test_unavailable_history_does_not_block_main_query(self) -> None:
        data_path = Path(self.directory.name) / "fallback_items.sqlite"
        connection = sqlite3.connect(data_path)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('a')")
        connection.commit()
        connection.close()
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(data_path),
            history_db_path=self.directory.name,
        )
        output = WorkflowRunner(
            config,
            llm_factory=lambda _: HistoryWorkflowLLM(),
            selected_skills=[],
        ).run(SqlTask(question="List item names", database_path=str(data_path)))
        self.assertEqual(output["rows"], [["a"]])
        self.assertEqual(output["history_write"]["status"], "failed")
        self.assertIn("history", output["history_write"]["error"].lower())

    def test_cli_import_show_and_confirmed_clear(self) -> None:
        environment = {"HISTORY_DB_PATH": str(self.history_path)}
        stdout = io.StringIO()
        argv = [
            "main.py",
            "--import-success-stories",
            str(SAMPLE_ROOT / "success_story.csv"),
            "--show-history",
        ]
        with patch.object(sys, "argv", argv), patch.dict(
            os.environ, environment, clear=False
        ), redirect_stdout(stdout):
            self.assertEqual(cli.main(), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["success_stories"]["inserted"], 3)
        self.assertEqual(len(payload["entries"]), 3)

        stderr = io.StringIO()
        with patch.object(sys, "argv", ["main.py", "--clear-history"]), redirect_stderr(stderr):
            self.assertEqual(cli.main(), 2)
        self.assertIn("--confirm-clear-history", stderr.getvalue())

        stdout = io.StringIO()
        argv = ["main.py", "--clear-history", "--confirm-clear-history"]
        with patch.object(sys, "argv", argv), patch.dict(
            os.environ, environment, clear=False
        ), redirect_stdout(stdout):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["cleared"], 3)


if __name__ == "__main__":
    unittest.main()
