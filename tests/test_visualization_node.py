import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from main import build_parser
from queryforge.workflow.node.visualization_node import VisualizationNode
from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, ExecutionResult, SQLContext, SqlTask


class VisualizationWorkflowLLM:
    def generate_json(self, prompt):
        if "Select local QueryForge skills" in prompt:
            return {"skills": [], "reason": "No optional skill."}
        if "Evaluate whether the SQL and result" in prompt:
            return {
                "success": True,
                "strategy": "SUCCESS",
                "reason": "The grouped result answers the question.",
                "suggested_fix": None,
            }
        return {
            "sql": "SELECT category, SUM(amount) AS total FROM sales GROUP BY category",
            "explanation": "Aggregate sales by category.",
            "tables_used": ["sales"],
        }


class VisualizationNodeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_category_and_numeric_defaults_to_bar(self):
        result = VisualizationNode.build_visualization(
            question="Rank revenue by region",
            sql="SELECT region, revenue FROM totals",
            columns=["region", "revenue"],
            rows=[["North", 20], ["South", 10]],
        )
        self.assertEqual(result.chart_type, "bar")
        self.assertEqual(result.chart_config["mark"], "bar")
        self.assertEqual(result.chart_config["encoding"]["x"]["field"], "region")
        self.assertEqual(result.chart_config["encoding"]["y"]["field"], "revenue")

    def test_date_and_numeric_prefers_line(self):
        result = VisualizationNode.build_visualization(
            question="Show monthly revenue trend",
            sql="SELECT month, revenue FROM totals",
            columns=["month", "revenue"],
            rows=[["2026-01", 10], ["2026-02", 20]],
        )
        self.assertEqual(result.chart_type, "line")
        self.assertEqual(result.chart_config["mark"]["type"], "line")
        self.assertEqual(result.chart_config["encoding"]["x"]["type"], "temporal")

    def test_small_explicit_share_request_uses_pie(self):
        result = VisualizationNode.build_visualization(
            question="Show each category's revenue percentage",
            sql="SELECT category, revenue FROM totals",
            columns=["category", "revenue"],
            rows=[["A", 60], ["B", 40]],
        )
        self.assertEqual(result.chart_type, "pie")
        self.assertEqual(result.chart_config["mark"]["type"], "arc")
        self.assertEqual(result.chart_config["encoding"]["theta"]["field"], "revenue")

    def test_unsupported_shape_falls_back_to_table(self):
        result = VisualizationNode.build_visualization(
            question="List school contacts",
            sql="SELECT school, phone FROM schools",
            columns=["school", "phone"],
            rows=[["A", "555-0100"], ["B", "555-0101"]],
        )
        self.assertEqual(result.chart_type, "table")
        self.assertEqual(result.chart_config["format"], "table")

    def test_execute_writes_vega_lite_json(self):
        context = self._context(
            columns=["category", "total"], rows=[["A", 3], ["B", 2]]
        )
        result = VisualizationNode(self.root / "charts").execute(context)
        self.assertTrue(result.success)
        chart = context.final_output["visualization"]
        path = Path(chart["chart_path"])
        self.assertTrue(path.is_file())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["$schema"], "https://vega.github.io/schema/vega-lite/v5.json")
        self.assertEqual(saved["mark"], "bar")

    def test_file_write_failure_does_not_fail_node_or_sql_output(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("file", encoding="utf-8")
        context = self._context(columns=["category", "total"], rows=[["A", 3]])
        result = VisualizationNode(blocked).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.final_output["status"], "success")
        self.assertIsNone(context.final_output["visualization"]["chart_path"])
        self.assertIsNotNone(context.final_output["visualization"]["error"])

    def test_workflow_only_adds_visualization_when_enabled(self):
        database = self.root / "sales.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE sales (category TEXT, amount INTEGER)")
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?)", [("A", 3), ("B", 2)]
        )
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
        task = SqlTask(question="Rank sales by category", database_path=str(database))
        plain = WorkflowRunner(
            config,
            llm_factory=lambda _: VisualizationWorkflowLLM(),
            selected_skills=[],
        ).run(task)
        self.assertNotIn("visualization", plain)

        visualized = WorkflowRunner(
            config,
            llm_factory=lambda _: VisualizationWorkflowLLM(),
            selected_skills=[],
            visualize=True,
            chart_output_dir=str(self.root / "workflow-charts"),
        ).run(task)
        self.assertEqual(visualized["visualization"]["chart_type"], "bar")
        self.assertTrue(Path(visualized["visualization"]["chart_path"]).is_file())

    def test_cli_accepts_visualization_options(self):
        args = build_parser().parse_args(
            ["--visualize", "--chart-output-dir", str(self.root / "charts")]
        )
        self.assertTrue(args.visualize)
        self.assertEqual(args.chart_output_dir, str(self.root / "charts"))

    @staticmethod
    def _context(columns, rows):
        return Context(
            task=SqlTask(question="Rank totals by category", database_path="test.sqlite"),
            sql_context=SQLContext(
                sql="SELECT category, total FROM totals",
                explanation="Return totals.",
                tables_used=["totals"],
            ),
            execution_result=ExecutionResult(
                columns=columns, rows=rows, row_count=len(rows)
            ),
            final_output={"status": "success"},
        )


if __name__ == "__main__":
    unittest.main()
