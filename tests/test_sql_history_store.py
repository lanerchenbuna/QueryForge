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
from queryforge.domain.knowledge import VerificationLevel
from queryforge.infrastructure.storage import SQLHistoryStore
from queryforge.infrastructure.storage.sql_history_store import DEFAULT_SEARCH_WINDOW


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

    def test_successful_workflow_writes_then_retrieves_reviewed_history(self) -> None:
        """H2: a written row becomes a few-shot example only after human review.

        The previously asserted behaviour (a just-executed row reaching the
        generation prompt) is the bug: a successful execution proves the query
        ran, not that it answered the business question.
        """
        data_path, config = self._workflow_fixture("items.sqlite")
        first = WorkflowRunner(
            config, llm_factory=lambda _: HistoryWorkflowLLM(), selected_skills=[]
        ).run(SqlTask(question="List item names", database_path=str(data_path)))
        self.assertEqual(first["history_write"]["status"], "inserted")
        entry_id = first["history_write"]["entry_id"]

        unreviewed_llm = HistoryWorkflowLLM()
        unreviewed = WorkflowRunner(
            config, llm_factory=lambda _: unreviewed_llm, selected_skills=[]
        ).run(
            SqlTask(
                question="Please list the item names", database_path=str(data_path)
            )
        )
        self.assertEqual(unreviewed["history_matches"], [])
        self.assertNotIn("SELECT name FROM items", unreviewed_llm.gen_prompts[0])
        unreviewed_evidence = unreviewed["task_evidence"]["history_retrieval"]
        self.assertTrue(unreviewed_evidence["trusted_only"])
        self.assertEqual(unreviewed_evidence["returned"], [])

        self.assertIsNotNone(self.store.mark_reviewed(entry_id, "business-owner"))

        reviewed_llm = HistoryWorkflowLLM()
        reviewed = WorkflowRunner(
            config, llm_factory=lambda _: reviewed_llm, selected_skills=[]
        ).run(
            SqlTask(
                question="Please list the item names", database_path=str(data_path)
            )
        )
        self.assertTrue(reviewed["history_matches"])
        self.assertIn("Persisted successful SQL history matches", reviewed_llm.gen_prompts[0])
        self.assertIn("SELECT name FROM items", reviewed_llm.gen_prompts[0])
        reviewed_evidence = reviewed["task_evidence"]["history_retrieval"]
        self.assertEqual(
            reviewed_evidence["returned"][0]["verification_level"],
            VerificationLevel.human_reviewed.value,
        )
        self.assertEqual(
            reviewed_evidence["returned"][0]["review_status"], "reviewed"
        )

    def test_workflow_history_reader_applies_the_run_domain_scope(self) -> None:
        """H2: the writer stamps a domain, so the reader must query by one.

        ``OutputNode`` stamps ``domain_id``/``data_version`` on every history row
        (and ``SQLHistoryStore`` implements scoped search), but the production
        reader called ``search(question, top_k=...)`` with no scope, so another
        domain's SQL was injected verbatim into the generation prompt.
        """
        data_path, config = self._workflow_fixture("scoped_items.sqlite")
        finance_id, _ = self.store.add(
            question="List item names",
            sql="SELECT name FROM items /* finance definition */",
            tables_used=["items"],
            success=True,
            metadata={"domain_id": "finance", "data_version": "2026-01"},
            domain_id="finance",
            data_version="2026-01",
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        in_domain_id, _ = self.store.add(
            question="List item names",
            sql="SELECT name FROM items /* anime definition */",
            tables_used=["items"],
            success=True,
            metadata={"domain_id": "anime_streaming", "data_version": "2026-01"},
            domain_id="anime_streaming",
            data_version="2026-01",
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        llm = HistoryWorkflowLLM()
        output = WorkflowRunner(
            config,
            llm_factory=lambda _: llm,
            selected_skills=[],
            history_domain_id="anime_streaming",
            history_data_version="2026-01",
        ).run(
            SqlTask(
                question="Please list the item names", database_path=str(data_path)
            )
        )
        self.assertEqual(
            [match["id"] for match in output["history_matches"]], [in_domain_id]
        )
        self.assertNotIn(finance_id, [match["id"] for match in output["history_matches"]])
        self.assertIn("anime definition", llm.gen_prompts[0])
        self.assertNotIn("finance definition", llm.gen_prompts[0])
        evidence = output["task_evidence"]["history_retrieval"]
        self.assertEqual(
            evidence["scope"],
            {"domain_id": "anime_streaming", "data_version": "2026-01"},
        )
        self.assertTrue(evidence["trusted_only"])
        self.assertEqual(
            [item["id"] for item in evidence["returned"]], [in_domain_id]
        )
        self.assertEqual(evidence["returned"][0]["domain_id"], "anime_streaming")

    def test_curated_rows_are_not_evicted_by_search_volume(self) -> None:
        """M8: a curated row must not fall out of the candidate window.

        The window was ``WHERE success = 1 ORDER BY id DESC LIMIT 2000`` with
        similarity scored in Python afterwards, so material imported before enough
        runs were recorded could never be found again: unrelated recent rows were
        returned instead and injected as "history matches".
        """
        curated_id, inserted = self.store.add(
            question="List item names for the curated example",
            sql="SELECT name FROM items WHERE curated = 1",
            tables_used=["items"],
            success=True,
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        self.assertTrue(inserted)
        for index in range(2010):
            self.store.add(
                question=f"Noise question {index} about device counts",
                sql=f"SELECT COUNT(*) FROM devices_{index}",
                success=True,
            )
        matches = self.store.search("List item names for the curated example", top_k=1)
        self.assertEqual([match.id for match in matches], [curated_id])
        self.assertEqual(matches[0].similarity, 1.0)

    def test_the_candidate_window_is_explicit_and_prioritises_curated_rows(self) -> None:
        """M8: the window is a configured bound, not an invisible constant."""
        store = SQLHistoryStore(self.history_path, search_window=2)
        curated_id, _ = store.add(
            question="List item names",
            sql="SELECT name FROM items WHERE curated = 1",
            tables_used=["items"],
            success=True,
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        store.add(
            question="Noise one about devices",
            sql="SELECT 1 FROM devices",
            success=True,
        )
        store.add(
            question="Noise two about devices",
            sql="SELECT 2 FROM devices",
            success=True,
        )
        result = store.search_with_evidence("List item names", top_k=1)
        self.assertEqual([match.id for match in result.matches], [curated_id])
        self.assertEqual(result.evidence["candidate_window"]["limit"], 2)
        self.assertEqual(result.evidence["candidate_window"]["scanned"], 2)

    def test_search_reports_the_window_scope_and_match_governance(self) -> None:
        """M8/H2: the reader reports what it scanned and what it filtered by."""
        in_scope_id, _ = self.store.add(
            question="List item names",
            sql="SELECT name FROM items",
            success=True,
            metadata={"domain_id": "anime_streaming", "data_version": "v1"},
            domain_id="anime_streaming",
            data_version="v1",
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items WHERE 1",
            success=True,
            metadata={"domain_id": "finance"},
            domain_id="finance",
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items WHERE 2",
            success=True,
            metadata={"domain_id": "anime_streaming", "data_version": "v2"},
            domain_id="anime_streaming",
            data_version="v2",
            verification_level=VerificationLevel.human_reviewed,
            review_status="reviewed",
        )
        self.store.add(
            question="List item names",
            sql="SELECT name FROM items WHERE 3",
            success=True,
            metadata={"domain_id": "anime_streaming", "data_version": "v1"},
            domain_id="anime_streaming",
            data_version="v1",
            verification_level=VerificationLevel.execution_success,
            review_status="draft",
        )
        result = self.store.search_with_evidence(
            "List item names",
            top_k=5,
            domain_id="anime_streaming",
            data_version="v1",
            trusted_only=True,
        )
        self.assertEqual([match.id for match in result.matches], [in_scope_id])
        evidence = result.evidence
        self.assertEqual(
            evidence["scope"],
            {"domain_id": "anime_streaming", "data_version": "v1"},
        )
        self.assertTrue(evidence["trusted_only"])
        self.assertEqual(evidence["candidate_window"]["limit"], DEFAULT_SEARCH_WINDOW)
        self.assertEqual(evidence["candidate_window"]["scanned"], 4)
        self.assertEqual(
            evidence["candidate_window"]["order"],
            ["reviewed", "curated_source", "id_desc"],
        )
        self.assertEqual(
            evidence["counts"],
            {
                "scanned": 4,
                "in_scope": 2,
                "trusted": 1,
                "matching_tables": 1,
                "similar": 1,
            },
        )
        self.assertEqual(evidence["status"], "active")
        self.assertEqual(
            evidence["returned"],
            [
                {
                    "id": in_scope_id,
                    "similarity": 1.0,
                    "domain_id": "anime_streaming",
                    "data_version": "v1",
                    "verification_level": "human_reviewed",
                    "review_status": "reviewed",
                    "source": "query",
                }
            ],
        )
        # The unscoped view of the same store still returns every successful row,
        # so the scope narrows the reader rather than the stored material.
        self.assertEqual(len(self.store.search("List item names", top_k=5)), 4)

    def _workflow_fixture(self, database_name: str):
        data_path = Path(self.directory.name) / database_name
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
        return data_path, config

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
