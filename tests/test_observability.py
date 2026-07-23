import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from main import build_parser
from queryforge.workflow.node.base import Node
from queryforge.workflow.workflow import Workflow, WorkflowError
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.core.observability import (
    ObservedModelProvider,
    configure_logging,
    new_run_id,
    node_logging_context,
    run_logging_context,
)
from queryforge.core.schemas.models import Context, SqlTask


class ObservabilityLLM:
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


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.log_path = self.root / "logs/queryforge.log"
        configure_logging("INFO", log_path=self.log_path, console=False)

    def tearDown(self):
        configure_logging("WARNING", console=False)
        self.directory.cleanup()

    def test_logging_has_run_context_and_redacts_credentials(self):
        logger = logging.getLogger("queryforge.test")
        with run_logging_context("qf_test_run"):
            logger.info(
                "authorization=Bearer secret-token api_key=sk-secret123456"
            )
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("run_id=qf_test_run", content)
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("secret-token", content)
        self.assertNotIn("sk-secret123456", content)

    def test_model_summary_does_not_save_or_log_prompt_by_default(self):
        trace_dir = self.root / "traces"
        provider = ObservedModelProvider(
            ObservabilityLLM(),
            provider_name="qwen",
            model_name="qwen-plus",
            debug_prompts=False,
            trace_dir=trace_dir,
        )
        secret_prompt = "private business prompt that must not be logged"
        with run_logging_context("qf_model"), node_logging_context("gen_sql"):
            provider.generate_json(secret_prompt)
        self.assertFalse(trace_dir.exists())
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("provider=qwen", log)
        self.assertIn("model=qwen-plus", log)
        self.assertIn(f"prompt_chars={len(secret_prompt)}", log)
        self.assertNotIn(secret_prompt, log)

    def test_debug_prompts_writes_explicit_trace(self):
        trace_dir = self.root / "traces"
        provider = ObservedModelProvider(
            ObservabilityLLM(),
            provider_name="openai",
            model_name="offline",
            debug_prompts=True,
            trace_dir=trace_dir,
        )
        with run_logging_context("qf_trace"), node_logging_context("gen_sql"):
            provider.generate_json("full prompt for explicit debugging")
        files = list((trace_dir / "qf_trace").glob("*.json"))
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "qf_trace")
        self.assertEqual(payload["node"], "gen_sql")
        self.assertEqual(payload["prompt"], "full prompt for explicit debugging")

    def test_workflow_exposes_run_id_timings_sql_and_summary(self):
        database = self.root / "items.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('alpha')")
        connection.commit()
        connection.close()
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(database),
            history_db_path=str(self.root / "history.sqlite"),
        )
        output = WorkflowRunner(
            config,
            llm_factory=lambda _: ObservabilityLLM(),
            selected_skills=[],
            show_run_summary=True,
            run_id_factory=lambda: "qf_fixed_run",
            trace_dir=str(self.root / "traces"),
        ).run(SqlTask(question="List item names", database_path=str(database)))
        self.assertEqual(output["run_id"], "qf_fixed_run")
        self.assertEqual(output["run_summary"]["run_id"], "qf_fixed_run")
        self.assertEqual(output["run_summary"]["output_status"], "success")
        self.assertTrue(output["run_summary"]["workflow_nodes"])
        self.assertTrue(
            all(
                node["duration_ms"] is not None
                for node in output["run_summary"]["workflow_nodes"]
            )
        )
        self.assertIsNotNone(output["sql_execution_duration_ms"])
        self.assertFalse((self.root / "traces").exists())
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("sql_generated sql=SELECT name FROM items ORDER BY name", log)
        self.assertIn("row_count=1", log)
        self.assertIn("run_summary", log)

    def test_failed_node_log_identifies_node_and_duration(self):
        class FailingNode(Node):
            name = "deliberate_failure"

            def execute(self, context):
                return self.failure("expected diagnostic")

        context = Context(task=SqlTask(question="test", database_path="test.sqlite"))
        with self.assertRaises(WorkflowError):
            Workflow(context, [FailingNode()]).run()
        result = context.node_results[0]
        self.assertEqual(result.node_name, "deliberate_failure")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.started_at)
        self.assertIsNotNone(result.ended_at)
        self.assertIsNotNone(result.duration_ms)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("node=deliberate_failure", log)
        self.assertIn("success=False", log)
        self.assertIn("expected diagnostic", log)

    def test_invalid_log_level_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid LOG_LEVEL"):
            configure_logging("verbose", log_path=self.log_path, console=False)

    def test_cli_parses_observability_options(self):
        args = build_parser().parse_args(
            ["--log-level", "DEBUG", "--debug-prompts", "--show-run-summary"]
        )
        self.assertEqual(args.log_level, "DEBUG")
        self.assertTrue(args.debug_prompts)
        self.assertTrue(args.show_run_summary)

    def test_run_ids_are_unique(self):
        first = new_run_id()
        second = new_run_id()
        self.assertRegex(first, r"^qf_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
