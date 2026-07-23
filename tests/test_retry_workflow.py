import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.workflow.workflow import WorkflowError
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.core.schemas.models import SqlTask
from queryforge.infrastructure.storage import SQLHistoryStore


class RetryLLM:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.gen_calls = 0
        self.fix_calls = 0
        self.reflect_calls = 0

    def generate_json(self, prompt: str):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "No optional skill needed."}
        if "Repair the SQLite query" in prompt:
            self.fix_calls += 1
            column = (
                "name"
                if self.mode in {"execution_fix", "reflection_fix"}
                else f"missing_{self.fix_calls}"
            )
            suffix = " LIMIT 1" if self.mode == "reflection_fix" else ""
            return {
                "fixed_sql": f"SELECT {column} FROM items{suffix}",
                "explanation": "Repair the selected column.",
                "tables_used": ["items"],
            }
        if "Evaluate whether the SQL and result" in prompt:
            self.reflect_calls += 1
            if self.mode == "reflection_fix" and self.reflect_calls == 1:
                return {
                    "success": False,
                    "strategy": "FIX_SQL",
                    "reason": "Limit the result to one requested example.",
                    "suggested_fix": "Add LIMIT 1.",
                }
            if self.mode == "regenerate" and self.reflect_calls == 1:
                return {
                    "success": False,
                    "strategy": "REGENERATE",
                    "reason": "Return names instead of a count.",
                    "suggested_fix": "Select the name column.",
                }
            if self.mode == "review":
                return {
                    "success": False,
                    "strategy": "NEED_USER_REVIEW",
                    "reason": "The requested business meaning is ambiguous.",
                    "suggested_fix": None,
                }
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The query now answers the question.",
                "suggested_fix": None,
            }

        self.gen_calls += 1
        if self.mode in {"execution_fix", "exhaust"}:
            sql = "SELECT missing_0 FROM items"
        elif self.mode == "regenerate" and self.gen_calls == 1:
            sql = "SELECT COUNT(*) AS item_count FROM items"
        else:
            sql = "SELECT name FROM items"
        return {
            "sql": sql,
            "explanation": "Generated test query.",
            "tables_used": ["items"],
        }


class RetryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "items.sqlite"
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
            history_db_path=str(Path(self.directory.name) / "history.sqlite"),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_mode(self, mode: str, **kwargs):
        llm = RetryLLM(mode)
        runner = WorkflowRunner(
            self.config,
            llm_factory=lambda _: llm,
            selected_skills=[],
            **kwargs,
        )
        output = runner.run(
            SqlTask(question="List item names", database_path=str(self.database))
        )
        return llm, output

    def test_execution_error_is_fixed_and_replanned(self) -> None:
        presented = []
        llm, output = self.run_mode(
            "execution_fix",
            plan_mode=True,
            auto_approve_plan=True,
            plan_presenter=presented.append,
        )
        self.assertEqual(output["rows"], [["a"], ["b"]])
        self.assertEqual(output["retry_count"], 1)
        self.assertEqual([item["status"] for item in output["sql_attempt_history"]], ["failed", "success"])
        self.assertEqual(len(output["fix_attempts"]), 1)
        self.assertEqual(len(presented), 2)
        self.assertEqual(llm.fix_calls, 1)
        stored = SQLHistoryStore(self.config.history_db_path).list_entries()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].sql, "SELECT name FROM items")

    def test_reflection_can_request_local_fix(self) -> None:
        _, output = self.run_mode("reflection_fix")
        self.assertEqual(output["rows"], [["a"]])
        self.assertEqual(output["retry_count"], 1)
        self.assertEqual(
            [item["reflection_strategy"] for item in output["sql_attempt_history"]],
            ["FIX_SQL", "SUCCESS"],
        )

    def test_reflection_can_regenerate_sql(self) -> None:
        llm, output = self.run_mode("regenerate")
        self.assertEqual(output["rows"], [["a"], ["b"]])
        self.assertEqual(output["retry_count"], 1)
        self.assertEqual(llm.gen_calls, 2)
        self.assertEqual(len(output["fix_attempts"]), 0)

    def test_retry_limit_preserves_all_failed_attempts(self) -> None:
        llm = RetryLLM("exhaust")
        runner = WorkflowRunner(
            self.config,
            llm_factory=lambda _: llm,
            selected_skills=[],
            max_retries=2,
        )
        with self.assertRaises(WorkflowError) as captured:
            runner.run(
                SqlTask(question="List item names", database_path=str(self.database))
            )
        error = captured.exception
        self.assertEqual(error.node_name, "retry_limit")
        self.assertIn("Maximum SQL retries (2) exhausted", str(error))
        self.assertEqual(error.context.retry_count, 2)
        self.assertEqual(len(error.context.sql_attempt_history), 3)
        self.assertEqual(len(error.context.execution_errors), 3)
        self.assertEqual(len(error.context.fix_attempts), 2)
        self.assertEqual(
            SQLHistoryStore(self.config.history_db_path).list_entries(), []
        )

    def test_need_user_review_stops_with_clear_reason(self) -> None:
        llm = RetryLLM("review")
        runner = WorkflowRunner(
            self.config,
            llm_factory=lambda _: llm,
            selected_skills=[],
        )
        with self.assertRaisesRegex(WorkflowError, "Human review required"):
            runner.run(
                SqlTask(question="List item names", database_path=str(self.database))
            )


if __name__ == "__main__":
    unittest.main()
