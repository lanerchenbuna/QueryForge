"""Offline tests for static HTML report generation and orchestration."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.workflow.report_generator import ReportGenerator
from queryforge.orchestration.agents.report import ReportAgent
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import RoutingDecision, TaskState
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, ExecutionResult, SQLContext, SqlTask
from queryforge.application import AgentOptions, AgentService


class ReportLLM:
    def generate_json(self, prompt: str) -> dict:
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The grouped result answers the report request.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT category, SUM(amount) AS total FROM items GROUP BY category ORDER BY total DESC",
            "explanation": "Aggregate amount by category.",
            "tables_used": ["items"],
        }


class ReportArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "items.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE items (category TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [("books", 10), ("books", 20), ("music", 5)],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def context(self, rows=None, columns=None) -> Context:
        return Context(
            task=SqlTask(
                question="Build report for sales by category",
                database_path=str(self.database),
            ),
            run_id="qf_report_test",
            sql_context=SQLContext(
                sql="SELECT category, SUM(amount) AS total FROM items GROUP BY category",
                explanation="Aggregate amount by category.",
                tables_used=["items"],
            ),
            execution_result=ExecutionResult(
                columns=columns or ["category", "total"],
                rows=rows or [["books", 30], ["music", 5]],
                row_count=len(rows or [["books", 30], ["music", 5]]),
            ),
            report_output_dir=str(self.root / "reports"),
        )

    def test_generator_writes_html_manifest_charts_and_findings(self):
        artifact = ReportGenerator(self.root / "reports", max_rows=1).generate(
            self.context()
        )
        html = Path(artifact.file_path).read_text(encoding="utf-8")
        manifest = json.loads(Path(artifact.manifest_path).read_text(encoding="utf-8"))
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("Result Table", html)
        self.assertIn("Show SQL", html)
        self.assertIn("vegaEmbed", html)
        self.assertIn("<svg", html)
        self.assertIn("chart-fallback", html)
        self.assertTrue(any(section.type == "chart" for section in artifact.sections))
        self.assertGreaterEqual(len(artifact.key_findings), 3)
        self.assertTrue(manifest["sections"])
        table = next(section for section in artifact.sections if section.type == "table")
        self.assertTrue(table.content["truncated"])

    def test_generator_selects_line_chart_for_time_metric(self):
        artifact = ReportGenerator(self.root / "reports").generate(
            self.context(
                columns=["month", "total"],
                rows=[["2026-01-01", 10], ["2026-02-01", 15]],
            )
        )
        chart = next(section for section in artifact.sections if section.type == "chart")
        self.assertEqual(chart.content["chart_type"], "line")

    def test_chart_spec_escapes_script_breakout(self):
        payload = "</script><script>alert(1)</script>"
        artifact = ReportGenerator(self.root / "reports").generate(
            self.context(
                columns=["category", "total"],
                rows=[[payload, 10], ["music", 5]],
            )
        )
        html = Path(artifact.file_path).read_text(encoding="utf-8")
        self.assertIn("vegaEmbed", html)
        # The user-controlled value must never terminate the enclosing script tag.
        self.assertNotIn("</script><script>", html)
        self.assertIn("<\\/script><script>", html)
        # The regular HTML table context remains entity-escaped.
        self.assertIn("&lt;/script&gt;", html)

    def test_generator_uses_metric_cards_for_scalar_result(self):
        artifact = ReportGenerator(self.root / "reports").generate(
            self.context(columns=["total"], rows=[[35]])
        )
        metrics = next(section for section in artifact.sections if section.type == "metrics")
        self.assertEqual(metrics.content["items"][0]["label"], "total")
        self.assertFalse(any(section.type == "chart" for section in artifact.sections))

    def test_report_agent_degrades_without_blocking_when_generation_fails(self):
        context = self.context()
        state = TaskState(
            run_id=context.run_id,
            entrypoint="test",
            classification=RoutingDecision(
                task_type="build_report",
                entrypoint="test",
                confidence=1.0,
                reason="test",
                pipeline="build_report",
            ),
        )
        store = AgentTeamStateStore(self.root / "runs")
        store.initialize(state)
        with patch.object(ReportGenerator, "generate", side_effect=RuntimeError("disk full")):
            reference = ReportAgent(store).run(state, context)
        self.assertEqual(reference.status, "degraded")
        self.assertIsNone(context.final_output)

    def test_build_report_generates_downloadable_artifact(self):
        reports = self.root / "reports"
        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(self.root / "history.sqlite"),
            orchestration_state_root=str(self.root / "runs"),
            report_output_dir=str(reports),
        )
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: ReportLLM(),
        )
        output = service.ask(
            "Build report for sales by category",
            AgentOptions(
                database=str(self.database),
                skills=[],
                run_id="qf_report_integration",
                orchestration_state_root=str(self.root / "runs"),
            ),
        )
        self.assertEqual(output["status"], "success")
        self.assertTrue(Path(output["report"]["file_path"]).is_file())
        self.assertEqual(
            service.report_path("qf_report_integration"),
            Path(output["report"]["file_path"]),
        )
        state = json.loads(
            Path(output["agent_team"]["state_path"]).read_text(encoding="utf-8")
        )
        report_ref = next(
            item for item in state["artifacts"] if item["artifact_type"] == "report_artifact"
        )
        self.assertEqual(report_ref["status"], "valid")


if __name__ == "__main__":
    unittest.main()
